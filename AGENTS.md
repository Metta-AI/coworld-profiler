# AGENTS.md

Guidance for coding agents working in coworld-profiler.

## What this is

A Coworld whose game and players exist only to measure hosted episode timing.
Read `README.md` first, then `docs/designs/2026-09-09-profiler-design.md` for
what is measured and why, `docs/measurement-reference.md` for what each
result field means, and `docs/player-protocol.md` for the wire protocol.

## Invariants

- The game and players emit no telemetry. Everything leaves the cluster as
  `results.json`, replay bytes, the per-slot player zip, or stdout JSON lines.
- Never log payloads, tokens, the player websocket URL, or presigned URLs.
- Durations use `time.monotonic_ns()`; wall clock is only for alignment.
- A message never carries its own send timestamp; timing records ship in the
  next action or in `flush_reply`.
- `scores` are all `0.0`. This is not a game to win.
- `coworld_manifest_template.json` schemas are generated from
  `profiler/config.py` and `profiler/game/results.py`. Edit the models, then
  run `uv run python tools/render_manifest.py`. The test suite checks sync.
- Keep `ping_timeout=None` in every player.

## Commands

```bash
uv sync --group dev
uv run pytest                       # unit + real-process end-to-end tests
uv run ruff check . && uv run ruff format --check .
uv run python tools/render_manifest.py
# From a venv that has the coworld CLI (the metta checkout):
uv run coworld build --project . --version X.Y.Z
uv run coworld run-episode dist/coworld_manifest.json [--variant ID]
uv run coworld certify dist/coworld_manifest.json
uv run coworld upload-coworld dist/coworld_manifest.json
```

## Layout

See README.md. The engine (`profiler/game/episode.py`) is transport-agnostic
so it can be tested without sockets; the server (`profiler/game/server.py`)
only adapts FastAPI websockets to it.
