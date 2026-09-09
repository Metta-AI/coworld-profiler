"""Fetch every episode of a set of experience requests and print a cross-variant table.

    uv run --project ~/coding/metta python tools/summarize_experiments.py tmp/xreqs.tsv [--out tmp/hosted] [--markdown]

`xreqs.tsv` is what tools/request_experiments.py prints: `xreq_id<TAB>variant<TAB>...`.
Episodes are downloaded with tools/fetch_episode.py's logic, then one row per
episode is printed, followed by per-variant medians. Milliseconds throughout.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from coworld.api_client import CoworldApiClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_episode import DEFAULT_SERVER, fetch  # noqa: E402

COLUMNS = [
    ("rtt p50", lambda r: r["websocket_application_rtt"]["p50_s"]),
    ("rtt p99", lambda r: r["websocket_application_rtt"]["p99_s"]),
    ("rtt max", lambda r: r["websocket_application_rtt"]["max_s"]),
    ("resid p50", lambda r: r["websocket_transport_residual"]["p50_s"]),
    ("resid p99", lambda r: r["websocket_transport_residual"]["p99_s"]),
    ("tick lag p99", lambda r: r["game_tick_lag"]["p99_s"]),
    ("loop lag p99", lambda r: r["game_event_loop_lag"]["p99_s"]),
    ("gc max", lambda r: r["game_gc_pause"]["max_s"]),
    ("late %", lambda r: None if r["action_late_fraction"] is None else r["action_late_fraction"] * 100 / 1000),
    ("ready max", lambda r: r["player_connect_ready_max_s"]),
    ("dns max", lambda r: max((s["player_connect_dns_s"] or 0) for s in r["slots"])),
    ("upgrade max", lambda r: max((s["player_connect_upgrade_s"] or 0) for s in r["slots"])),
    ("artifact max", lambda r: max((s["player_artifact_publish_s"] or 0) for s in r["slots"])),
    ("throttled", lambda r: r["game_cpu_throttled_s"]),
]


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value * 1000:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xreqs", type=Path)
    parser.add_argument("--out", type=Path, default=Path("tmp/hosted"))
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()

    client = CoworldApiClient.from_login(server_url=args.server)
    rows: list[tuple[str, str, dict]] = []
    for line in args.xreqs.read_text().splitlines():
        if not line.strip():
            continue
        xreq_id, variant = line.split("\t")[:2]
        for episode in client.list_experience_request_episodes(xreq_id):
            if episode.status != "completed":
                print(f"{variant} {episode.id}: {episode.status} {episode.error_type or ''}", file=sys.stderr)
                continue
            results_path = args.out / episode.id / "results.json"
            if not results_path.exists():
                fetch(client, episode.id, args.out)
            if not results_path.exists():
                continue
            rows.append((variant, episode.id, json.loads(results_path.read_text())))

    header = ["variant", "episode", "slots"] + [name for name, _ in COLUMNS]
    sep = " | " if args.markdown else "  "
    print(sep.join(header))
    if args.markdown:
        print("|".join("---" for _ in header))
    for variant, episode_id, results in rows:
        values = [variant, episode_id[:13], str(results["player_slot_count"])] + [_cell(fn(results)) for _, fn in COLUMNS]
        print(sep.join(values))
    print()
    print("per-variant medians (ms)")
    print(sep.join(["variant", "n", "slots"] + [name for name, _ in COLUMNS]))
    if args.markdown:
        print("|".join("---" for _ in range(len(COLUMNS) + 3)))
    for variant in dict.fromkeys(v for v, _, _ in rows):
        group = [r for v, _, r in rows if v == variant]
        cells = []
        for _, fn in COLUMNS:
            values = [fn(r) for r in group if fn(r) is not None]
            cells.append(_cell(statistics.median(values)) if values else "-")
        print(sep.join([variant, str(len(group)), str(group[0]["player_slot_count"])] + cells))


if __name__ == "__main__":
    main()
