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

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SITE = os.environ.get("DD_SITE", "datadoghq.com")


def search(query: str, *, limit: int, window: str = "now-48h") -> list[dict]:
    body = {"data": {"type": "search_request", "attributes": {"filter": {"query": query, "from": window, "to": "now"}, "page": {"limit": limit}}}}
    request = urllib.request.Request(
        f"https://api.{SITE}/api/v2/spans/events/search",
        data=json.dumps(body).encode(),
        headers={"DD-API-KEY": os.environ["DD_API_KEY"], "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"], "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)["data"]
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise
            wait = float(error.headers.get("x-ratelimit-reset") or error.headers.get("Retry-After") or 2 ** attempt)
            print(f"rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(min(wait, 60) + 1)
    raise RuntimeError("Datadog rate limit did not clear after 8 attempts")


def main(jobs_path: str, out_dir: str) -> None:
    jobs = json.loads(Path(jobs_path).read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}
    for row in jobs:
        job_id = row["job_id"]
        target = out / f"{job_id}.json"
        if target.exists():
            index[job_id] = target.name
            continue
        roots = search(f"@job.id:{job_id} operation_name:job.lifecycle", limit=1)
        if not roots:
            print(f"{job_id}: no root span", file=sys.stderr)
            continue
        trace_id = roots[0]["attributes"]["trace_id"]
        spans = search(f"trace_id:{trace_id}", limit=200)
        compact = [
            {
                "operation_name": s["attributes"]["operation_name"],
                "resource_name": s["attributes"].get("resource_name"),
                "start": s["attributes"]["start_timestamp"],
                "end": s["attributes"]["end_timestamp"],
                "duration_s": s["attributes"]["custom"]["duration"] / 1e9,
                "parent_id": s["attributes"]["parent_id"],
                "span_id": s["attributes"]["span_id"],
                "custom": {k: v for k, v in s["attributes"]["custom"].items() if k in ("pod", "node", "image", "player", "replay", "step_count", "coworld", "job", "episode_request")},
            }
            for s in spans
        ]
        target.write_text(json.dumps({"job_id": job_id, "trace_id": trace_id, "ereq": row["ereq"], "round": row["round"], "spans": compact}, indent=1))
        index[job_id] = target.name
        print(f"{job_id}: {len(compact)} spans")
        time.sleep(1.0)
    (out / "index.json").write_text(json.dumps(index, indent=1))
    print(f"{len(index)} of {len(jobs)} jobs saved to {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
