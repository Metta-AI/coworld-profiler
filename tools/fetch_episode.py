"""Download everything a hosted profiler episode produced into a local directory.

    uv run --project ~/coding/metta python tools/fetch_episode.py ereq_... [ereq_...] [--out tmp/hosted]

Needs the coworld CLI's login (run `uv run coworld` commands from the metta
checkout once so the token exists). Writes, per episode request:

    results.json            game-written results (requires coworld ownership or team account)
    replay                  gzip JSONL replay from the public replay URL
    episode.json            the episode request row
    policy_artifact_<slot>.zip   each slot's zip, when the caller owns that policy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.request import urlopen

from coworld.api_client import CoworldApiClient

DEFAULT_SERVER = "https://softmax.com/api"


def fetch(client: CoworldApiClient, episode_request_id: str, out_dir: Path) -> None:
    target = out_dir / episode_request_id
    target.mkdir(parents=True, exist_ok=True)
    row = client.get_episode_request(episode_request_id)
    (target / "episode.json").write_text(row.model_dump_json(indent=1))
    try:
        (target / "results.json").write_bytes(client.get_episode_request_artifact_bytes(episode_request_id, "results"))
    except Exception as error:  # noqa: BLE001 - access is ownership-scoped; report and continue
        print(f"{episode_request_id}: results not readable ({type(error).__name__})", file=sys.stderr)
    if row.replay_url:
        with urlopen(row.replay_url, timeout=60) as response:
            (target / "replay").write_bytes(response.read())
    for entry in client.list_episode_request_policy_artifacts(episode_request_id):
        if not entry.has_artifact:
            continue
        data = client.get_episode_request_policy_artifact(episode_request_id, entry.policy_version_id, entry.position)
        (target / f"policy_artifact_{entry.position}.zip").write_bytes(data)
    print(f"{episode_request_id}: {row.status} -> {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_request_ids", nargs="+")
    parser.add_argument("--out", type=Path, default=Path("tmp/hosted"))
    parser.add_argument("--server", default=DEFAULT_SERVER)
    args = parser.parse_args()
    client = CoworldApiClient.from_login(server_url=args.server)
    for episode_request_id in args.episode_request_ids:
        fetch(client, episode_request_id, args.out)


if __name__ == "__main__":
    main()
