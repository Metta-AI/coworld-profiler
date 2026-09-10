# coworld-profiler

A Coworld that exists only to measure. It runs as an ordinary hosted episode
on the Softmax Observatory and records the timing the platform does not
instrument: websocket round trips between the game pod and each player pod,
decomposed into game encode, send, player decode, think, encode, send, and a
transport residual; per-slot connect stages (DNS, TCP, upgrade); startup
milestones inside both containers; tick scheduling lag; action staleness;
event-loop lag; cgroup CPU throttling; GC pauses; and artifact write times.

Design and the mapping to the platform's existing spans:
[docs/designs/2026-09-09-profiler-design.md](docs/designs/2026-09-09-profiler-design.md).
Wire protocol: [docs/player-protocol.md](docs/player-protocol.md).
What each number means and how to read it:
[docs/measurement-reference.md](docs/measurement-reference.md).
The research report on five rounds of hosted measurements (100 episodes):
[docs/reports/hosted-timing-2026-09-09.html](docs/reports/hosted-timing-2026-09-09.html)
(commentable HTML) and its markdown twin
[docs/reports/hosted-timing-2026-09-09.md](docs/reports/hosted-timing-2026-09-09.md).
The earlier two-round write-up is
[docs/results/2026-09-09-hosted-findings.md](docs/results/2026-09-09-hosted-findings.md).

## Layout

```
profiler/game/server.py      FastAPI game container (routes, bootstrap stamps, finalize)
profiler/game/episode.py     tick loop, per-slot queues, turn correlation
profiler/game/results.py     aggregates -> results.json, replay record order
profiler/player/player.py    the instrumented player (--policy echo|busy|slow-start)
profiler/player/connection.py DNS / TCP / upgrade timed separately
profiler/player/artifact.py  per-slot zip publication
profiler/{clocks,recorder,resources,payload,protocol,config,io}.py
tools/analyze_replay.py      print a summary from a replay, results file, or player zip
tools/fetch_episode.py       download a hosted episode's results, replay, and player zips
tools/request_experiments.py create hosted experience requests per variant
tools/summarize_experiments.py cross-variant table from hosted episodes
tools/render_manifest.py     regenerate manifest schemas from the pydantic models
tests/                       pytest
```

## Run locally

```bash
uv sync --group dev
uv run pytest
# The coworld CLI lives in the metta checkout's venv; run it from there or via --project.
uv run --project ~/coding/metta coworld build --project . --version 0.1.0
uv run --project ~/coding/metta coworld run-episode dist/coworld_manifest.json
uv run --project ~/coding/metta coworld run-episode dist/coworld_manifest.json --variant fanout-8p
```

`run-episode` leaves `results.json`, the gzip JSONL replay, per-slot
`policy_artifact_<slot>.zip`, and container logs in its artifact workspace.
`uv run python tools/analyze_replay.py <replay-or-results>` prints the
headline numbers.

## Hosted

```bash
uv run --project ~/coding/metta coworld certify dist/coworld_manifest.json
uv run --project ~/coding/metta coworld upload-coworld dist/coworld_manifest.json
# Image ref is game.runnable.image in dist/coworld_manifest.json. Each --run value is one argv token.
uv run --project ~/coding/metta coworld upload-policy IMAGE --name profiler-echo \
  --run python --run -m --run profiler.player.player --run --policy --run echo
uv run --project ~/coding/metta python tools/request_experiments.py --coworld cow_... --echo <uuid> --busy <uuid>
uv run --project ~/coding/metta python tools/summarize_experiments.py tmp/xreqs.tsv
```

`results.json` is readable only by Softmax team accounts. Every player's
artifact zip also contains `game_summary.json`, the same aggregate, so a
policy owner can read the whole picture from the policy-artifact route.

## What it deliberately does not do

It emits no telemetry from the cluster. Everything crosses the boundary as
artifacts the platform already collects. It does not change the runner and
it does not add spans; the design doc lists which existing gaps it explains
and which it can only bound.

## Comparing hosted traces

`tools/fetch_spans.py JOBS_JSON OUTPUT_DIR` finds each job's lifecycle trace
and follows Datadog's pagination through the currently indexed spans in the
requested time window. This does not prove that every original span was retained.
Use `--refresh` to refetch existing files after late spans arrive, and
`--from 2026-09-09T00:00:00Z --to 2026-09-10T00:00:00Z` for an older window.
Timeouts or partial-result warnings fail the run before replacing that job's saved trace.
The run stops on the first error. Completed files remain indexed after an error. Refresh still revisits every
listed job; use a filtered jobs file to resume a refresh. Fix ambiguous queries
before retrying. Refresh merges by span ID, preserving
older spans outside the query window. Missing roots retain previous files with a
warning. Trace files and the index are replaced atomically.
A completed fetch does not prove that Datadog has finished indexing late spans.

The tool uses the existing standard-library HTTP client and Datadog's
[search pagination contract](https://docs.datadoghq.com/api/latest/spans/search-spans/).
No additional SDK is needed for this single endpoint. Run it through the token
broker with a session ID and the `datadog.read` scope; never save API keys.

`tools/reconcile_spans.py SPANS_DIR RESULTS_DIR...` preserves groups by operation,
Pod role and identity, player slot, and resource. UID is preferred, followed by
Pod name or span ID when older records lack a Pod identifier. Missing slots
remain unknown. The distribution table aggregates across recorded Pods. Repeated names never silently choose the
last container or player. Ambiguous single-span summaries are omitted and counted. Container and player records
remain in the resource distributions; other ambiguous operations appear only in the
omission counts. Game-pod
image-pull totals sum container durations; overlapping pulls are not elapsed job
time. Explicit viewer waits and historical derived gaps are reported separately; neither
measures the game's first turn. The separate player-startup interval measures the
worker's status observation, not a player handshake.

The resource table supports `container.run`, `player.pod_create`, and
`player.startup_observed` from companion Metta PRs
[#22417](https://github.com/Metta-AI/metta/pull/22417) and
[#22406](https://github.com/Metta-AI/metta/pull/22406). These are implemented
changes awaiting merge and deployment, not fields present in historical traces.
Older saved traces do not contain those records. The aggregate startup gate
(`player.startup_wait`) and each player's observed startup are different intervals.
An empty trace response is reported and leaves the previous snapshot untouched.

Startup summaries separate legacy worker-entry-to-health from explicit
container-start-to-health intervals. Position-derived cleanup gaps are calculated
only for explicit timing records; old reconstructed positions are not evidence
of contiguous work. The upload-to-running-stage-end gap crosses worker and database
clocks, so clock offsets can affect it. Timestamp markers have a count table, not duration averages.
Container attempts and player startup outcomes are separate distribution groups.
Nested spans overlap: worker bootstrap contains setup, and viewer wait contains
player startup wait. Do not add these rows to estimate elapsed time.
