"""Join the worker's Datadog lifecycle spans to the profiler's inside view, per episode.

    uv run python tools/reconcile_spans.py tmp/spans tmp/hosted-round1 tmp/hosted tmp/hosted-r3to5

For every job with both a span file and a results.json, derive the worker's
phases from the trace. Explicit viewer intervals are used when available;
historical traces use the gap after player launch. Neither is the first turn. Put
them beside the game's own stamps. Prints markdown tables in milliseconds.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def ms(v: float | None, digits: int = 0) -> str:
    return "-" if v is None else f"{v * 1000:,.{digits}f}"


def med(vals: list[float | None]) -> float | None:
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def q(vals: list[float | None], pct: float) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    return vals[min(len(vals) - 1, int(round(pct / 100 * (len(vals) - 1))))] if vals else None


def worker_view(trace: dict) -> dict:
    groups: dict[tuple, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    seen = set()
    for span in trace["spans"]:
        if span["span_id"] in seen:
            continue
        seen.add(span["span_id"])
        pod = span["custom"].get("pod") or {}
        player = span["custom"].get("player") or {}
        identity = (
            span["operation_name"],
            pod.get("role"),
            pod.get("uid") or pod.get("name") or span["span_id"],
            player.get("slot"),
            span["resource_name"],
        )
        groups.setdefault(identity, []).append(span)
        # Historical game-only traces have no role tag. Player spans never enter
        # the game-pod summary, and repeated names are never resolved by order.
        if pod.get("role") != "player":
            key = span["operation_name"] + (":" + span["resource_name"] if span["operation_name"] == "job.stage" else "")
            by_name.setdefault(key, []).append(span)
    single = {key: spans[0] for key, spans in by_name.items() if len(spans) == 1}
    view: dict = {"span_groups": groups, "ambiguous_names": sorted(key for key, spans in by_name.items() if len(spans) > 1 and key != "image.pull")}
    for key in (
        "job.stage:pending",
        "job.stage:dispatched",
        "job.stage:running",
        "game.bootstrap",
        "player.launch",
        "episode.loop",
        "episode.finalize",
        "pod.create",
        "node.allocate",
        "container.start",
        "worker.bootstrap",
        "player.startup_wait",
        "worker.viewer_wait",
    ):
        span = single.get(key)
        view[key] = span["duration_s"] if span else None
    pulls = by_name.get("image.pull", [])
    view["image.pull"] = sum(span["duration_s"] for span in pulls) if pulls else None
    view["image_pull_count"] = len(pulls)
    launch, loop = single.get("player.launch"), single.get("episode.loop")
    viewer = single.get("worker.viewer_wait")
    view["first_step_s"] = (ts(loop["start"]) - ts(launch["end"])) if not viewer and launch and loop else None
    finalize, running = single.get("episode.finalize"), single.get("job.stage:running")
    view["post_loop_gap_s"] = (ts(finalize["start"]) - ts(loop["end"])) if finalize and loop else None
    view["running_tail_s"] = (ts(running["end"]) - ts(finalize["end"])) if finalize and running else None
    hits = [(pull["custom"].get("image") or {}).get("cache_hit") for pull in pulls]
    view["image_cache_hits"] = hits
    node = single.get("node.allocate")
    view["node_age_s"] = ((node["custom"].get("node") or {}).get("age_at_bind_s")) if node else None
    root = single.get("job.lifecycle")
    view["lifecycle_s"] = root["duration_s"] if root else None
    return view


def main(spans_dir: str, *result_dirs: str) -> None:
    results: dict[str, tuple[str, dict, dict]] = {}
    for root in result_dirs:
        for d in Path(root).glob("ereq_*"):
            episode_path, results_path = d / "episode.json", d / "results.json"
            if episode_path.exists() and results_path.exists():
                episode = json.loads(episode_path.read_text())
                results[episode["job_id"]] = (d.name, episode, json.loads(results_path.read_text()))
    rows = []
    for path in sorted(Path(spans_dir).glob("*.json")):
        if path.name == "index.json":
            continue
        trace = json.loads(path.read_text())
        if trace["job_id"] not in results:
            continue
        ereq, episode, r = results[trace["job_id"]]
        w = worker_view(trace)
        slots_ready = [s["player_connect_ready_s"] for s in r["slots"] if s.get("player_connect_ready_s") is not None]
        rows.append(
            {
                "ereq": ereq[:13],
                "round": trace["round"],
                "slots": r["player_slot_count"],
                "steps": r["step_count"],
                "mode": r["mode"],
                **w,
                "g_listen_to_health": r["game_listening_to_first_health_s"],
                "g_listen_to_global": r["game_listening_to_first_global_s"],
                "g_boot_inside": (r["game_process_birth_to_first_mark_s"] or 0)
                + (r["game_bootstrap_import_s"] or 0)
                + (r["game_bootstrap_config_read_s"] or 0)
                + (r["game_bootstrap_config_decode_s"] or 0)
                + (r["game_bootstrap_payload_build_s"] or 0)
                + (r["game_bootstrap_server_start_s"] or 0),
                "g_last_ready": max(slots_ready) if slots_ready else None,
                "g_first_ready": min(slots_ready) if slots_ready else None,
                "g_loop": r["episode_loop_measurement_s"],
                "g_replay_prepare": r["replay_prepare_s"],
                "connected": sum(1 for s in r["slots"] if s["connected"]),
            }
        )
    print(f"joined episodes: {len(rows)}")
    dropped = Counter(key for row in rows for key in row["ambiguous_names"])
    print(f"ambiguous single-span summaries omitted, by operation: {dict(dropped)}")
    clean = [x for x in rows if x["round"] != "round1" and x["steps"] != 20]

    print("\n## Worker phases from Datadog spans, all joined episodes (ms)\n")
    print("| span or derived interval | median | p90 | min | max | n |")
    print("|---|---|---|---|---|---|")
    for key, label in [
        ("job.stage:pending", "stage pending"),
        ("job.stage:dispatched", "stage dispatched (pod create to worker start)"),
        ("pod.create", "pod.create (game pod)"),
        ("node.allocate", "node.allocate (game pod)"),
        ("image.pull", "sum of container pull durations (overlaps possible)"),
        ("container.start", "container.start (game pod)"),
        ("worker.bootstrap", "worker process bootstrap"),
        ("job.stage:running", "stage running"),
        ("game.bootstrap", "game.bootstrap (see timing.boundary for source semantics)"),
        ("player.launch", "player.launch (player_launch_s)"),
        ("first_step_s", "historical launch-to-loop gap (legacy traces only)"),
        ("worker.viewer_wait", "explicit viewer wait (timestamped traces only)"),
        ("player.startup_wait", "player processes started or failed: worker observation"),
        ("episode.loop", "episode.loop (gameplay_s)"),
        ("post_loop_gap_s", "gap between episode.loop end and episode.finalize start"),
        ("episode.finalize", "episode.finalize (artifact_upload_s)"),
        ("running_tail_s", "running stage after episode.finalize ends"),
        ("lifecycle_s", "whole job"),
    ]:
        vals = [x[key] for x in rows if x.get(key) is not None]
        print(
            f"| {label} | {ms(med(vals))} | {ms(q(vals, 90))} | {ms(min(vals)) if vals else '-'} | {ms(max(vals)) if vals else '-'} | {len(vals)} |"
        )
    hits = Counter(hit if hit is not None else "unknown" for row in rows for hit in row["image_cache_hits"])
    pull_jobs = sum(row["image_pull_count"] > 0 for row in rows)
    print(f"\nimage.pull emitted for {pull_jobs} jobs; per-container cache_hit values: {dict(hits)}")

    print("\n## Container and player distributions (ms; grouped by role, resource and slot)\n")
    print("| operation | role | resource | slot | median | p90 | records |")
    print("|---|---|---|---|---|---|---|")
    resources: dict[tuple[str, str, str, str], list[float]] = {}
    for row in rows:
        for (operation, role, _uid, slot, resource), spans in row["span_groups"].items():
            if operation not in {
                "pod.create",
                "player.pod_create",
                "image.pull",
                "container.start",
                "container.run",
                "container.started",
                "container.finished",
                "node.allocate",
                "player.startup_observed",
            }:
                continue
            key = (operation, role or "unknown", resource or "-", str(slot) if slot is not None else "-")
            resources.setdefault(key, []).extend(span["duration_s"] for span in spans)
    for (operation, role, resource, slot), values in sorted(resources.items()):
        print(f"| {operation} | {role} | {resource} | {slot} | {ms(med(values))} | {ms(q(values, 90))} | {len(values)} |")

    print("\n## Inside versus outside, per episode (ms), clean episodes\n")
    print(
        "| episode | slots | worker game_boot_s | game: process start to listen | "
        "game: listen to first /healthz | worker first_step_s | game: first /global "
        "to last slot ready | worker gameplay_s | game: measured loop | difference "
        "gameplay minus loop |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for x in sorted(clean, key=lambda x: (x["slots"], x["ereq"])):
        g2r = (x["g_last_ready"] - x["g_listen_to_global"]) if x["g_last_ready"] is not None and x["g_listen_to_global"] is not None else None
        diff = (x["episode.loop"] - x["g_loop"]) if x["episode.loop"] is not None and x["g_loop"] is not None else None
        print(
            f"| {x['ereq']} | {x['slots']} | {ms(x['game.bootstrap'])} | {ms(x['g_boot_inside'])} "
            f"| {ms(x['g_listen_to_health'])} | {ms(x['first_step_s'])} | {ms(g2r)} | "
            f"{ms(x['episode.loop'])} | {ms(x['g_loop'])} | {ms(diff)} |"
        )

    print("\n## Reconciliation medians, clean episodes (ms)\n")
    fs = [x["first_step_s"] for x in clean if x["first_step_s"] is not None]
    g2r_all = [(x["g_last_ready"] - x["g_listen_to_global"]) for x in clean if x["g_last_ready"] is not None and x["g_listen_to_global"] is not None]
    pairs = [
        (x["first_step_s"], x["g_last_ready"] - x["g_listen_to_global"])
        for x in clean
        if x["first_step_s"] is not None and x["g_last_ready"] is not None and x["g_listen_to_global"] is not None
    ]
    deltas = [b - a for a, b in pairs]
    print(f"- worker first_step_s: median {ms(med(fs))}, p90 {ms(q(fs, 90))}, max {ms(max(fs) if fs else None)} (n={len(fs)})")
    print(
        f"- game: first /global connect to last slot ready: median {ms(med(g2r_all))}, "
        f"p90 {ms(q(g2r_all, 90))}, max {ms(max(g2r_all) if g2r_all else None)}"
    )
    print(
        f"- per-episode difference (game interval minus worker first_step_s): median "
        f"{ms(med(deltas))}, p10 {ms(q(deltas, 10))}, p90 {ms(q(deltas, 90))}"
    )
    by_slots: dict[int, list[float]] = {}
    for x in clean:
        if x["first_step_s"] is not None:
            by_slots.setdefault(x["slots"], []).append(x["first_step_s"])
    print("- worker first_step_s by slot count: " + ", ".join(f"{n} slots {ms(med(v))} (n={len(v)})" for n, v in sorted(by_slots.items())))
    gb = [
        (x["game.bootstrap"], x["g_boot_inside"], x["g_listen_to_health"])
        for x in clean
        if x["game.bootstrap"] is not None and x["g_listen_to_health"] is not None
    ]
    print(
        f"- worker game_boot_s median {ms(med([a for a, _, _ in gb]))} versus game "
        f"process start to listen median {ms(med([b for _, b, _ in gb]))} plus listen "
        f"to first /healthz median {ms(med([c for _, _, c in gb]))}"
    )
    gl = [(x["episode.loop"], x["g_loop"]) for x in clean if x["episode.loop"] is not None and x["g_loop"] is not None]
    print(
        f"- worker gameplay_s minus game measured loop: median {ms(med([a - b for a, b in gl]))}, "
        f"p10 {ms(q([a - b for a, b in gl], 10))}, p90 {ms(q([a - b for a, b in gl], 90))} "
        f"(n={len(gl)})"
    )
    fin = [(x["episode.finalize"], x["g_replay_prepare"]) for x in clean if x["episode.finalize"] is not None]
    print(
        f"- worker artifact_upload_s median {ms(med([a for a, _ in fin]))}; game replay build median {ms(med([b for _, b in fin if b is not None]))}"
    )
    pl = [x["player.launch"] for x in clean if x["player.launch"] is not None]
    print(f"- worker player_launch_s median {ms(med(pl))}, max {ms(max(pl) if pl else None)}")
    disp = [x["job.stage:dispatched"] for x in rows if x["job.stage:dispatched"] is not None]
    pc = [x["pod.create"] for x in rows if x["pod.create"] is not None]
    cs = [x["container.start"] for x in rows if x["container.start"] is not None]
    print(f"- game pod dispatch: stage dispatched median {ms(med(disp))}, pod.create median {ms(med(pc))}, container.start median {ms(med(cs))}")
    r1 = [x for x in rows if x["round"] == "round1" and x["steps"] != 20]
    if r1:
        print(
            f"- round 1 (busy pod crashed): worker first_step_s median {ms(med([x['first_step_s'] for x in r1 if x['first_step_s'] is not None]))}, "
            f"gameplay_s median {ms(med([x['episode.loop'] for x in r1 if x['episode.loop'] is not None]))}, "
            f"game measured loop median {ms(med([x['g_loop'] for x in r1 if x['g_loop'] is not None]))}"
        )


if __name__ == "__main__":
    main(sys.argv[1], *sys.argv[2:])
