"""Aggregate every downloaded hosted episode into the tables the report needs.

    uv run python tools/aggregate_rounds.py tmp/hosted-round1:round1 tmp/hosted:round2 tmp/hosted-r3:round3 ...

Each argument is `<dir>:<label>`. Every `<dir>/ereq_*/results.json` is one
episode; the variant is inferred from the results (mode, slot count, cells,
step_seconds). Prints markdown tables. Milliseconds unless stated.
"""

from __future__ import annotations

import gzip
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path


def variant_of(r: dict) -> str:
    if len(r["cells"]) > 1:
        return "payload-sweep"
    if r["mode"] == "blocking":
        return "blocking-turns"
    if r["step_count"] == 10000:
        return "long-10000-ticks"
    if r["step_count"] == 20:
        return "smoke-fixture"
    if r["player_slot_count"] > 2:
        return f"fanout-{r['player_slot_count']}p"
    if abs(r["step_seconds"] - 1 / 24) < 1e-6:
        return "tick-2p-24fps"
    if r["step_seconds"] == 0.05:
        return "tick-2p-50ms"
    return "baseline-2p-20ms"


def ms(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v * 1000:.{digits}f}"


def med(vals: list[float | None]) -> float | None:
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def q(vals: list[float | None], pct: float) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(pct / 100 * (len(vals) - 1))))]


def main(args: list[str]) -> None:
    episodes: list[tuple[str, str, dict, Path]] = []
    for arg in args:
        directory, label = arg.split(":")
        for path in sorted(Path(directory).glob("ereq_*/results.json")):
            r = json.loads(path.read_text())
            episodes.append((label, variant_of(r), r, path.parent))
    print(f"episodes: {len(episodes)}  by round: {dict(sorted((k, sum(1 for e in episodes if e[0] == k)) for k in {e[0] for e in episodes}))}")
    clean = [e for e in episodes if e[0] != "round1" and e[1] != "smoke-fixture"]
    print(f"clean (busy slot present, non-smoke): {len(clean)}")

    # --- per-variant table over clean episodes
    print("\n## Per-variant medians over clean episodes (ms)\n")
    print(
        "| variant | episodes | slots | RTT p50 pooled | residual p50 | residual p99 | residual max | game tick lag p99 | game loop lag p99 | GC max | late % | ready max (s) |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for _, variant, r, _ in clean:
        by_variant[variant].append(r)
    order = [
        "baseline-2p-20ms",
        "tick-2p-24fps",
        "tick-2p-50ms",
        "fanout-4p",
        "fanout-8p",
        "fanout-16p",
        "blocking-turns",
        "long-10000-ticks",
        "payload-sweep",
    ]
    for variant in order:
        group = by_variant.get(variant, [])
        if not group:
            continue
        g = lambda f: med([f(r) for r in group])  # noqa: E731
        print(
            f"| {variant} | {len(group)} | {group[0]['player_slot_count']} | {ms(g(lambda r: r['websocket_application_rtt']['p50_s']))} | "
            f"{ms(g(lambda r: r['websocket_transport_residual']['p50_s']))} | {ms(g(lambda r: r['websocket_transport_residual']['p99_s']))} | "
            f"{ms(g(lambda r: r['websocket_transport_residual']['max_s']))} | {ms(g(lambda r: r['game_tick_lag']['p99_s']))} | "
            f"{ms(g(lambda r: r['game_event_loop_lag']['p99_s']))} | {ms(g(lambda r: r['game_gc_pause']['max_s']))} | "
            f"{100 * (g(lambda r: r['action_late_fraction']) or 0):.3f} | {(g(lambda r: r['player_connect_ready_max_s']) or 0):.1f} |"
        )

    # --- echo vs busy pooled slot stats over clean non-sweep episodes
    print("\n## Echo versus busy slots, clean non-sweep episodes (ms)\n")
    slots = [(r, s) for _, v, r, _ in clean if v != "payload-sweep" for s in r["slots"] if s["connected"]]
    print(
        "| policy | slot-episodes | RTT p50 | RTT p99 | processing p50 | think CPU p50 | residual p50 | residual p99 | game send await p50 | player send await p50 | ping p50 | late actions | negative residuals | missing reports |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for policy in ("echo", "busy"):
        ss = [s for _, s in slots if s["policy"] == policy]
        f = lambda k, p: ms(med([s[k][p] for s in ss]))  # noqa: E731
        late = sum(s["late_discarded_count"] + s["unanswered_count"] + s["timeout_count"] for s in ss)
        obs = sum(s["observation_count"] for s in ss)
        print(
            f"| {policy} | {len(ss)} | {f('websocket_application_rtt', 'p50_s')} | {f('websocket_application_rtt', 'p99_s')} | {f('player_processing_duration', 'p50_s')} | "
            f"{f('player_think_cpu', 'p50_s')} | {f('websocket_transport_residual', 'p50_s')} | {f('websocket_transport_residual', 'p99_s')} | "
            f"{f('game_websocket_send_await', 'p50_s')} | {f('player_websocket_send_await', 'p50_s')} | {f('ping_rtt', 'p50_s')} | "
            f"{late} of {obs} ({100 * late / obs:.3f} %) | {sum(s['negative_residual_count'] for s in ss)} | {sum(s['timing_report_missing_count'] for s in ss)} |"
        )
    # distribution of per-slot residual p50 across echo slots
    echo_res = [s["websocket_transport_residual"]["p50_s"] for _, s in slots if s["policy"] == "echo"]
    print(
        f"\nEcho slot residual p50 across slot-episodes: min {ms(min(echo_res))}, p10 {ms(q(echo_res, 10))}, median {ms(med(echo_res))}, p90 {ms(q(echo_res, 90))}, max {ms(max(echo_res))} (n={len(echo_res)})"
    )
    echo_rtt = [s["websocket_application_rtt"]["p99_s"] for _, s in slots if s["policy"] == "echo"]
    print(f"Echo slot RTT p99 across slot-episodes: median {ms(med(echo_rtt))}, p90 {ms(q(echo_rtt, 90))}, max {ms(max(echo_rtt))}")

    # --- payload cells
    print("\n## Payload sweep cells, clean episodes (ms), first pass then second (reverse) pass\n")
    sweeps = [r for _, v, r, _ in clean if v == "payload-sweep"]
    print("| cell | bytes | RTT p50 | RTT p99 | residual p50 | residual p99 | game encode p50 | samples |")
    print("|---|---|---|---|---|---|---|---|")
    cell_ids = [c["cell_id"] for c in sweeps[0]["cells"]]
    for cid in cell_ids:
        cells = [c for r in sweeps for c in r["cells"] if c["cell_id"] == cid]
        g = lambda f: med([f(c) for c in cells])  # noqa: E731
        print(
            f"| {cid} | {cells[0]['observation_bytes']:,} | {ms(g(lambda c: c['websocket_application_rtt']['p50_s']))} | {ms(g(lambda c: c['websocket_application_rtt']['p99_s']))} | "
            f"{ms(g(lambda c: c['websocket_transport_residual']['p50_s']))} | {ms(g(lambda c: c['websocket_transport_residual']['p99_s']))} | "
            f"{ms(g(lambda c: c['game_encode']['p50_s']))} | {sum(c['websocket_application_rtt']['count'] for c in cells)} |"
        )
    print(
        f"\nPayload-sweep episodes: n={len(sweeps)}, late fraction median {100 * (med([r['action_late_fraction'] for r in sweeps]) or 0):.1f} %, tick lag p99 median {ms(med([r['game_tick_lag']['p99_s'] for r in sweeps]), 0)} ms, observations dropped before send median {med([r['observation_dropped_count'] for r in sweeps])}"
    )

    # --- startup over all episodes (round1 included: echo slots are valid)
    print("\n## Startup and bootstrap over all hosted episodes (ms)\n")
    everything = [r for _, _, r, _ in episodes]
    print("| interval | median | p90 | min | max | n |")
    print("|---|---|---|---|---|---|")
    for key, label in [
        ("game_process_birth_to_first_mark_s", "game: process start to first Python line"),
        ("game_bootstrap_import_s", "game: imports"),
        ("game_bootstrap_config_read_s", "game: config read"),
        ("game_bootstrap_payload_build_s", "game: payload build"),
        ("game_bootstrap_server_start_s", "game: server start"),
        ("game_listening_to_first_health_s", "game listening -> first /healthz hit"),
        ("game_listening_to_first_global_s", "game listening -> first /global viewer"),
        ("player_connect_ready_min_s", "game listening -> first slot ready"),
        ("player_connect_ready_max_s", "game listening -> last slot ready"),
        ("replay_prepare_s", "game: replay build"),
        ("replay_publish_s", "game: replay write"),
    ]:
        vals = [r[key] for r in everything if r.get(key) is not None]
        print(f"| {label} | {ms(med(vals))} | {ms(q(vals, 90))} | {ms(min(vals))} | {ms(max(vals))} | {len(vals)} |")
    allslots = [s for r in everything for s in r["slots"] if s["connected"]]
    for key, label in [
        ("player_connect_dns_s", "player: DNS resolve"),
        ("player_connect_tcp_s", "player: TCP connect"),
        ("player_connect_upgrade_s", "player: websocket upgrade"),
        ("player_connect_ready_s", "player: game listening -> this slot ready"),
        ("player_artifact_publish_s", "player: artifact zip PUT"),
    ]:
        vals = [s[key] for s in allslots if s.get(key) is not None]
        print(f"| {label} | {ms(med(vals))} | {ms(q(vals, 90))} | {ms(min(vals))} | {ms(max(vals))} | {len(vals)} |")
    births, imports = [], []
    quota = None
    for _, _, r, d in episodes:
        replay = d / "replay"
        if not replay.exists():
            continue
        for rec in (json.loads(l) for l in gzip.decompress(replay.read_bytes()).decode().splitlines() if l):
            if rec["record_type"] == "player_startup" and rec.get("birth_to_first_mark_ns") is not None:
                births.append(rec["birth_to_first_mark_ns"] / 1e9)
                imports.append(rec["imports_ns"] / 1e9)
            if rec["record_type"] == "resource" and quota is None:
                quota = (rec.get("cpu_quota_usec"), rec.get("cpu_period_usec"))
    print(
        f"| player: process start to first Python line | {ms(med(births))} | {ms(q(births, 90))} | {ms(min(births))} | {ms(max(births))} | {len(births)} |"
    )
    print(f"| player: imports | {ms(med(imports))} | {ms(q(imports, 90))} | {ms(min(imports))} | {ms(max(imports))} | {len(imports)} |")
    print(
        f"\ngame cgroup cpu quota/period: {quota}; throttled seconds max over all episodes: {max((r['game_cpu_throttled_s'] or 0) for r in everything)}"
    )

    # --- clocks
    offs = [s["clock_offset_ns"] / 1e6 for s in allslots if s["clock_offset_ns"] is not None]
    widths = [(s["clock_offset_upper_ns"] - s["clock_offset_lower_ns"]) / 1e6 for s in allslots if s["clock_offset_upper_ns"] is not None]
    print(
        f"clock offset player-minus-game (ms): median {st.median(offs):.3f}, min {min(offs):.3f}, max {max(offs):.3f}; bound width median {st.median(widths):.2f} max {max(widths):.2f}; n={len(offs)}"
    )

    # --- fan-out per-slot spread
    print("\n## Fan-out: per-slot echo RTT p50 spread within an episode (ms)\n")
    print("| slots | episodes | min slot p50 | median slot p50 | max slot p50 | game send await p99 max |")
    print("|---|---|---|---|---|---|")
    for n in (2, 4, 8, 16):
        group = [r for _, v, r, _ in clean if v != "payload-sweep" and v != "blocking-turns" and r["player_slot_count"] == n]
        if not group:
            continue
        mins, meds, maxs, sends = [], [], [], []
        for r in group:
            vals = [
                s["websocket_application_rtt"]["p50_s"]
                for s in r["slots"]
                if s["policy"] == "echo" and s["websocket_application_rtt"]["p50_s"] is not None
            ]
            mins.append(min(vals))
            meds.append(st.median(vals))
            maxs.append(max(vals))
            sends.append(max(s["game_websocket_send_await"]["p99_s"] or 0 for s in r["slots"]))
        print(f"| {n} | {len(group)} | {ms(med(mins))} | {ms(med(meds))} | {ms(med(maxs))} | {ms(max(sends))} |")

    # --- long runs and blocking
    longs = [r for _, v, r, _ in clean if v == "long-10000-ticks"]
    if longs:
        print(
            f"\nlong-10000: n={len(longs)}, loop seconds median {med([r['episode_loop_measurement_s'] for r in longs]):.1f}, GC collections median {med([r['game_gc_pause']['count'] for r in longs])}, GC max median {ms(med([r['game_gc_pause']['max_s'] for r in longs]))}, replay bytes median {med([r['replay_size_bytes'] for r in longs]):,.0f}, replay build median {ms(med([r['replay_prepare_s'] for r in longs]), 0)} ms, late median {100 * (med([r['action_late_fraction'] for r in longs]) or 0):.3f} %"
        )
    blocks = [r for _, v, r, _ in clean if v == "blocking-turns"]
    if blocks:
        e = med([s["player_turn_duration"]["p50_s"] for r in blocks for s in r["slots"] if s["policy"] == "echo"])
        b = med([s["player_turn_duration"]["p50_s"] for r in blocks for s in r["slots"] if s["policy"] == "busy"])
        print(
            f"blocking: n={len(blocks)}, per-decision turn p50 echo {ms(e)} busy {ms(b)}, p99 pooled median {ms(med([r['player_turn_duration']['p99_s'] for r in blocks]))}, loop seconds median {med([r['episode_loop_measurement_s'] for r in blocks]):.1f}, timeouts {sum(s['timeout_count'] for r in blocks for s in r['slots'])}"
        )

    # --- round-to-round stability for baseline
    print("\n## Baseline stability across rounds (residual p50 per episode, ms)\n")
    for label in sorted({e[0] for e in clean}):
        vals = [r["websocket_transport_residual"]["p50_s"] for lab, v, r, _ in clean if lab == label and v == "baseline-2p-20ms"]
        if vals:
            print(f"- {label}: {', '.join(ms(v) for v in vals)}")
    # episode ids
    print("\n## Episode request ids by round\n")
    for label in sorted({e[0] for e in episodes}):
        ids = [d.name for lab, _, _, d in episodes if lab == label]
        print(f"- {label} ({len(ids)}): {', '.join(i[:13] for i in ids)}")


if __name__ == "__main__":
    main(sys.argv[1:])
