"""Regenerate the manifest template's config_schema and results_schema from the pydantic models.

    uv run python tools/render_manifest.py          # rewrite coworld_manifest_template.json in place
    uv run python tools/render_manifest.py --check  # exit 1 if the template is stale (used by tests)

The models are the source of truth; the template carries copies because the
platform validates configs and results against the manifest, not our code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from profiler.config import GameConfig
from profiler.game.results import Results

TEMPLATE = Path(__file__).resolve().parents[1] / "coworld_manifest_template.json"


def rendered() -> dict:
    template = json.loads(TEMPLATE.read_text())
    config_schema = GameConfig.model_json_schema()
    config_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    results_schema = Results.model_json_schema()
    results_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    template["game"]["config_schema"] = config_schema
    template["game"]["results_schema"] = results_schema
    return template


def main(argv: list[str]) -> int:
    document = rendered()
    text = json.dumps(document, indent=2) + "\n"
    if "--check" in argv:
        if TEMPLATE.read_text() != text:
            print("coworld_manifest_template.json is stale; run tools/render_manifest.py", file=sys.stderr)
            return 1
        return 0
    TEMPLATE.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
