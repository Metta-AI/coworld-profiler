"""Fetch the Datadog lifecycle trace for each hosted episode's job and save the spans.

    python3 ~/coding/metta/scripts/token_broker_client.py exec --scope datadog.read \
        --reason "..." -- python3 tools/fetch_spans.py tmp/jobs.json tmp/spans

Reads DD_API_KEY and DD_APP_KEY from the environment (the token broker injects
them). For every job in the input list it finds the `job.lifecycle` root by
`@job.id`, then fetches every span in that trace. Output: one JSON file per
job under the output directory, plus `index.json` mapping job id to file.
Stdlib only, no Datadog SDK.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

SITE = os.environ.get("DD_SITE", "datadoghq.com")


def _search_page(body: dict) -> dict:
    request = urllib.request.Request(
        f"https://api.{SITE}/api/v2/spans/events/search",
        data=json.dumps(body).encode(),
        headers={"DD-API-KEY": os.environ["DD_API_KEY"], "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"], "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise
            wait = float(error.headers.get("x-ratelimit-reset") or error.headers.get("Retry-After") or 2**attempt)
            print(f"rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(min(wait, 60) + 1)
    raise RuntimeError("Datadog rate limit did not clear after 8 attempts")


def search(query: str, *, limit: int, window: str = "now-48h", until: str | None = None) -> list[dict]:
    # Use the API cursor with fixed bounds so later pages cover the same window.
    end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else datetime.now(UTC)
    start = (end - timedelta(hours=48)).isoformat() if window == "now-48h" else datetime.fromisoformat(window.replace("Z", "+00:00")).isoformat()
    body = {
        "data": {
            "type": "search_request",
            "attributes": {
                "filter": {"query": query, "from": start, "to": end.isoformat()},
                "sort": "timestamp",
                "page": {"limit": limit},
            },
        }
    }
    spans = {}
    cursors = set()
    for _ in range(100):
        page = _search_page(body)
        metadata = page.get("meta", {})
        if metadata.get("status") == "timeout" or metadata.get("warnings"):
            raise RuntimeError("Datadog returned partial span results; saved trace was not replaced")
        for span in page["data"]:
            spans[(span["attributes"]["trace_id"], span["attributes"]["span_id"])] = span
        cursor = (metadata.get("page") or {}).get("after")
        if not cursor:
            return list(spans.values())
        if cursor in cursors:
            raise RuntimeError("Datadog repeated a pagination cursor")
        cursors.add(cursor)
        body["data"]["attributes"]["page"]["cursor"] = cursor
    raise RuntimeError("Span search exceeded 100 pages; narrow the time window")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=1))
    temporary.replace(path)


def main(jobs_path: str, out_dir: str, *, refresh: bool = False, window: str = "now-48h", until: str | None = None) -> None:
    end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else datetime.now(UTC)
    start = end - timedelta(hours=48) if window == "now-48h" else datetime.fromisoformat(window.replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Search timestamps must include a timezone, for example 2026-09-09T00:00:00Z")
    if start >= end:
        raise ValueError("Search start must precede search end")
    window, until = start.isoformat(), end.isoformat()
    jobs = json.loads(Path(jobs_path).read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    index = {job_id: name for job_id, name in index.items() if (out / name).is_file()}
    index.update({row["job_id"]: f"{row['job_id']}.json" for row in jobs if (out / f"{row['job_id']}.json").exists()})
    write_json(out / "index.json", index)
    for row in jobs:
        job_id = row["job_id"]
        target = out / f"{job_id}.json"
        if target.exists() and not refresh:
            index[job_id] = target.name
            continue
        roots = search(f"@job.id:{job_id} operation_name:job.lifecycle", limit=100, window=window, until=until)
        if not roots:
            retained = "; retained the previous snapshot" if target.exists() else ""
            print(f"{job_id}: no root span{retained}", file=sys.stderr)
            time.sleep(1.0)
            continue
        trace_ids = {root["attributes"]["trace_id"] for root in roots}
        if len(trace_ids) != 1:
            raise ValueError(f"{job_id}: multiple lifecycle traces; select an explicit time window")
        trace_id = trace_ids.pop()
        spans = search(f"trace_id:{trace_id}", limit=200, window=window, until=until)
        if not spans:
            print(f"{job_id}: empty trace response; no snapshot written", file=sys.stderr)
            time.sleep(1.0)
            continue
        compact = [
            {
                "operation_name": s["attributes"]["operation_name"],
                "resource_name": s["attributes"].get("resource_name"),
                "start": s["attributes"]["start_timestamp"],
                "end": s["attributes"]["end_timestamp"],
                "duration_s": s["attributes"]["custom"]["duration"] / 1e9,
                "parent_id": s["attributes"]["parent_id"],
                "span_id": s["attributes"]["span_id"],
                "custom": {
                    k: v
                    for k, v in s["attributes"]["custom"].items()
                    if k
                    in (
                        "pod",
                        "container",
                        "dispatch",
                        "node",
                        "image",
                        "player",
                        "player_file",
                        "replay",
                        "step_count",
                        "coworld",
                        "job",
                        "episode_request",
                        "timing",
                        "spec",
                    )
                },
            }
            for s in spans
        ]
        if target.exists():
            previous = json.loads(target.read_text())
            if previous["trace_id"] != trace_id:
                raise ValueError(f"{job_id}: saved trace ID differs; use a fresh output directory")
            merged = {span["span_id"]: span for span in previous["spans"]}
            merged.update({span["span_id"]: span for span in compact})
            compact = list(merged.values())
        write_json(target, {"job_id": job_id, "trace_id": trace_id, "ereq": row["ereq"], "round": row["round"], "spans": compact})
        index[job_id] = target.name
        write_json(out / "index.json", index)
        print(f"{job_id}: {len(compact)} spans")
        time.sleep(1.0)
    print(f"{sum(row['job_id'] in index for row in jobs)} of {len(jobs)} requested jobs available; {len(index)} total in {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs_path")
    parser.add_argument("out_dir")
    parser.add_argument("--refresh", action="store_true", help="Refetch saved traces to include late spans")
    parser.add_argument(
        "--from", dest="window", default="now-48h", help="Search start as an ISO timestamp with timezone; omitted means 48 hours before end"
    )
    parser.add_argument("--to", dest="until", help="Search end as an ISO timestamp with timezone; defaults to now")
    args = parser.parse_args()
    main(args.jobs_path, args.out_dir, refresh=args.refresh, window=args.window, until=args.until)
