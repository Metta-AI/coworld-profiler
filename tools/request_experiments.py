"""Create hosted experience requests for profiler variants.

    uv run --project ~/coding/metta python tools/request_experiments.py \
        --coworld cow_... --echo <policy_version_uuid> --busy <policy_version_uuid> \
        [--variant baseline-2p-20ms --variant fanout-8p ...] [--episodes 2]

Each variant's roster is filled to the variant's slot count. Slot 0 is the
busy policy when one is given (so every episode has one slot with real think
time), the rest are echo. Prints the created request ids, one per line, so
they can be passed to tools/fetch_episode.py later.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from coworld.api_client import CoworldApiClient

DEFAULT_SERVER = "https://softmax.com/api"
TEMPLATE = Path(__file__).resolve().parents[1] / "coworld_manifest_template.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coworld", required=True)
    parser.add_argument("--echo", required=True, help="policy version UUID of profiler-echo")
    parser.add_argument("--busy", help="policy version UUID of profiler-busy-5ms (slot 0 when given)")
    parser.add_argument("--variant", action="append", dest="variants")
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--notes", default="coworld-profiler timing experiment")
    args = parser.parse_args()

    variants = {variant["id"]: variant for variant in json.loads(TEMPLATE.read_text())["variants"]}
    selected = args.variants or list(variants)
    client = CoworldApiClient.from_login(server_url=args.server)
    for variant_id in selected:
        slot_count = len(variants[variant_id]["game_config"]["players"])
        roster = []
        for slot in range(slot_count):
            policy_ref = args.busy if (slot == 0 and args.busy) else args.echo
            roster.append({"player": {"policy_ref": policy_ref}, "slot": slot})
        body = {
            "coworld_id": args.coworld,
            "variant_id": variant_id,
            "roster": roster,
            "num_episodes": args.episodes,
            "notes": f"{args.notes}: {variant_id}",
        }
        created = client.create_experience_request(body)
        print(f"{created.id}\t{variant_id}\t{slot_count} slots x {args.episodes}")


if __name__ == "__main__":
    main()
