"""Print the headline numbers from a results.json, a replay (gzip JSONL), or a player artifact zip.

uv run python tools/analyze_replay.py path/to/results.json
uv run python tools/analyze_replay.py path/to/replay
uv run python tools/analyze_replay.py path/to/policy_artifact_0.zip
"""

from __future__ import annotations

import gzip
import json
import sys
import zipfile
from pathlib import Path
from typing import Any


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value * 1000:.3f}"


def _dist(name: str, dist: dict[str, Any]) -> str:
    return f"{name:<34} n={dist['count']:<6} p50 {_ms(dist['p50_s']):>9}  p90 {_ms(dist['p90_s']):>9}  p99 {_ms(dist['p99_s']):>9}  max {_ms(dist['max_s']):>9}  ms"


def print_results(results: dict[str, Any]) -> None:
    print(
        f"run {results['run_id']}  mode {results['mode']}  slots {results['player_slot_count']}  step {results['step_seconds']} s  measured ticks {results['step_count']}  complete {results['measurement_complete']}"
    )
    print()
    print(
        "bootstrap (ms)  imports",
        _ms(results["game_bootstrap_import_s"]),
        " config read",
        _ms(results["game_bootstrap_config_read_s"]),
        " decode",
        _ms(results["game_bootstrap_config_decode_s"]),
        " payload build",
        _ms(results["game_bootstrap_payload_build_s"]),
        " server start",
        _ms(results["game_bootstrap_server_start_s"]),
    )
    print(
        "process birth to first mark",
        _ms(results["game_process_birth_to_first_mark_s"]),
        " listening to first health",
        _ms(results["game_listening_to_first_health_s"]),
        " to first global",
        _ms(results["game_listening_to_first_global_s"]),
    )
    print("slot ready after listening: min", _ms(results["player_connect_ready_min_s"]), " max", _ms(results["player_connect_ready_max_s"]), " ms")
    print()
    for name in (
        "websocket_application_rtt",
        "websocket_transport_residual",
        "player_turn_duration",
        "game_step_duration",
        "game_tick_lag",
        "game_event_loop_lag",
        "game_gc_pause",
    ):
        print(_dist(name, results[name]))
    print()
    print(
        f"late action fraction {results['action_late_fraction']}  unanswered {results['action_unanswered_count']}  dropped before send {results['observation_dropped_count']}  missing timing reports {results['timing_report_missing_count']}"
    )
    print(
        f"cpu throttled {results['game_cpu_throttled_s']} s  memory peak {results['game_memory_peak_bytes']}  replay {results['replay_size_bytes']} bytes prepare {_ms(results['replay_prepare_s'])} publish {_ms(results['replay_publish_s'])} ms"
    )
    print()
    print(
        f"{'slot':<5}{'policy':<11}{'dns':>8}{'tcp':>8}{'upgrade':>9}{'ready':>9}{'rtt p50':>9}{'rtt p99':>9}{'proc p50':>9}{'resid p50':>10}{'ping p50':>9}{'applied':>8}{'late':>6}{'offset ms':>10}{'artifact':>10}"
    )
    for slot in results["slots"]:
        late = slot["late_discarded_count"] + slot["unanswered_count"] + slot["timeout_count"]
        offset = "-" if slot["clock_offset_ns"] is None else f"{slot['clock_offset_ns'] / 1e6:.3f}"
        artifact = _ms(slot["player_artifact_publish_s"]) if slot["player_artifact_error"] is None else "error"
        print(
            f"{slot['player_slot']:<5}{str(slot['policy']):<11}{_ms(slot['player_connect_dns_s']):>8}{_ms(slot['player_connect_tcp_s']):>8}{_ms(slot['player_connect_upgrade_s']):>9}{_ms(slot['player_connect_ready_s']):>9}{_ms(slot['websocket_application_rtt']['p50_s']):>9}{_ms(slot['websocket_application_rtt']['p99_s']):>9}{_ms(slot['player_processing_duration']['p50_s']):>9}{_ms(slot['websocket_transport_residual']['p50_s']):>10}{_ms(slot['ping_rtt']['p50_s']):>9}{slot['applied_count']:>8}{late:>6}{offset:>10}{artifact:>10}"
        )
    if results.get("cells"):
        print()
        for cell in results["cells"]:
            print(
                f"cell {cell['cell_id']:<12} {cell['observation_bytes']:>9} bytes  "
                + _dist("rtt", cell["websocket_application_rtt"])
                + "   encode p50 "
                + _ms(cell["game_encode"]["p50_s"])
            )


def main(path_text: str) -> None:
    path = Path(path_text)
    raw = path.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            summary = json.loads(archive.read("summary.json"))
            print("player artifact:", path.name, "files:", ", ".join(names))
            print(json.dumps(summary, indent=1))
            if "game_summary.json" in names:
                game_summary = json.loads(archive.read("game_summary.json"))
                results = game_summary["results"]
                results["slots"] = game_summary["slots"]
                print()
                print_results(results)
        return
    if raw[:2] == b"\x1f\x8b":
        records = [json.loads(line) for line in gzip.decompress(raw).decode().splitlines() if line]
        summary = next(record for record in records if record["record_type"] == "summary")
        counts: dict[str, int] = {}
        for record in records:
            counts[record["record_type"]] = counts.get(record["record_type"], 0) + 1
        print("replay records:", counts)
        print_results(summary)
        return
    print_results(json.loads(raw))


if __name__ == "__main__":
    main(sys.argv[1])
