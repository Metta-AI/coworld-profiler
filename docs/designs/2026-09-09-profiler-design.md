# coworld-profiler design

Status: living document. Created 2026-09-09. Owner: James Boggs.

## Problem

Hosted Coworld episodes on the Observatory record five coarse worker phases
(`game_boot_s`, `player_launch_s`, `first_step_s`, `gameplay_s`,
`artifact_upload_s`; see metta `packages/coworld/src/coworld/runner/phase_timings.py`).
Nothing inside a game or player container is measured. The episode-internal
spans and metrics that spec 0080 freezes (`player.connect`, `player.turn`,
`game.step`, `player.turn.duration`, `game.step.duration`) exist only for the
in-process arena backend. Websocket transport time between game and player
pods is not measured anywhere.

The platform is deliberately agentless. Game and player containers emit no
telemetry. Data crosses the boundary only as artifacts: `results.json`, replay
bytes, the per-slot player artifact zip, and container logs. The game-facing
contract is frozen, so we cannot add env vars or worker behaviour.

## Goal

A Coworld whose game and bundled players exist only to measure. It runs as an
ordinary hosted episode, so it experiences the real cluster path: real pod
scheduling, the real Service between game and player pods, real CPU limits.
It writes every measurement into the artifacts the platform already collects.

Non-goals: emitting spans or metrics from the cluster; changing the runner;
packet capture or kernel-level timing; a scoring league.

## What it measures, mapped to the existing gaps

"Measured" means two timestamps in one process around a named operation.
"Bounded" means observable events bracket an interval without exposing its
internal boundaries. "Unobservable" means no process we control sees it.

| Existing phase | Unexplained today | Profiler establishes |
|---|---|---|
| `game.bootstrap` (`game_boot_s`) | container start, interpreter, imports, config fetch, app init, listen, health poll | Measured: first Python marker onward, imports, config read + decode, server startup, each `/healthz` request. Bounded: process birth from `/proc/self/stat`. Unobservable: container runtime work, failed health polls that never reach the app. |
| `player.launch` (`player_launch_s`) | worker contract checks, pod create API calls | Measured: game-side handling of `/client/player`, bad-token rejection, `/client/global`. Unobservable: the API calls. |
| span-less `first_step_s` | pod scheduling, image pull, init container health polling, player interpreter start, DNS, TCP, handshake, first observation | Measured: every player-side milestone from first Python marker through first action; game-side accept, ready, first send per slot. Bounded: game-listening to slot-ready via clock-offset alignment. Unobservable: scheduling versus image pull split, init-container failures. |
| `episode.loop` (`gameplay_s`) | everything | Measured: tick scheduling lag, per-turn RTT decomposed into game encode, game send await, player decode, think, encode, send await, transport residual; staleness; fan-out dispersion; event-loop lag; cgroup throttling; GC pauses. |
| `episode.finalize` (`artifact_upload_s`) | worker uploads | Measured: game-side replay and results write durations, player-side zip build and PUT duration. Unobservable: the worker's own S3 uploads. |
| arena-only `player.connect` | per-slot connect | Player-side DNS, TCP, handshake durations, and game-side ready-to-accepted per slot. |
| arena-only `player.turn` / `game.step` | per-turn and per-step | Full per-turn decomposition and the synchronous state-update duration, as raw records in the replay and as distribution aggregates in results. |

Interpretation rule: never subtract profiler totals from rendered span widths
and call the remainder network time. The event processor reconstructs span
positions by cumulative summation with no anchors
(`app_backend/.../job_lifecycle_trace.py:304-345`), so compare matching
intervals and keep the unobservable remainders explicit.

## Measurement design

### Clocks

- `time.monotonic_ns()` for every duration and ordering decision.
- `time.time_ns()` only for cross-process alignment.
- `time.thread_time_ns()` for the busy player's CPU budget.
- Never subtract a game monotonic timestamp from a player one.
- Each process records bracketed anchors (monotonic before, wall, monotonic
  after) at startup, every 10 s, and at shutdown, so wall-minus-monotonic
  drift is visible.

### Round trip decomposition

Timestamps per observation/action pair:

| Stamp | Boundary |
|---|---|
| g0 | game begins JSON encode of the observation |
| g1 | game calls send with the encoded text |
| g2 | send await returns |
| p0 | player receive returns a complete message |
| p1 | player JSON decode done |
| p2a, p2b | think begin, think end |
| p3a, p3b | action encode begin, end |
| p4 | player calls send |
| p5 | player send await returns |
| g3 | game receive returns |
| g4 | game decode and validation done |

Derived:

```
websocket.application_rtt    = g3 - g1
player.turn.duration         = g4 - g1
player.processing.duration   = p4 - p0
websocket.transport_residual = (g3 - g1) - (p4 - p0)
game.websocket.send_await    = g2 - g1
player.websocket.send_await  = p5 - p4
```

The transport residual is socket buffering plus framing plus scheduling plus
wire time. It is not pure network transit and the doc says so wherever it is
shown. Negative residuals are flagged, never clamped.

A reply cannot contain its own send timestamp. Action N carries the completed
timing record for action N-1. The game requests a final flush before it
finalizes. Missing reports yield null decompositions, never assumed zeros.

### Clock offset

Standard NTP four-timestamp probes (RFC 5905 §8): game sends `clock_probe`
with T1; player stamps T2 on receipt and T3 before reply; game stamps T4. The
player reports T2 and T3 in a follow-up `clock_report`.

```
offset  = ((T2 - T1) + (T3 - T4)) / 2      player clock minus game clock
delay   = (T4 - T1) - (T3 - T2)
bounds  = [T3 - T4, T2 - T1]
```

Eight probes per slot before and after the measured phase, one per 10 s
during it. The minimum-delay probe per window is the representative. One-way
estimates are reported with their bounds and the symmetric-delay assumption
stated.

### Connect decomposition (player side)

`getaddrinfo` timed, then `socket.create_connection` timed, then the
connected socket handed to `websockets.connect(..., sock=...)` so the upgrade
is timed separately. This is player-process DNS and TCP setup. The platform
init container has already polled the Service, so this is a warm path, and the
results name it that way.

### Modes

Fixed tick: tick `t` is scheduled at `origin + t * step_seconds` on the
monotonic clock. Observation `t` is sent to every slot through a one-deep
per-slot send queue (a newer observation replaces an unsent older one and is
counted as dropped). At tick `t+1` the game applies the action for `t` if it
arrived, else noop. Late actions are recorded with how many tick boundaries
they missed and are never applied to a later state. This matches what
Crewrift, CTF, and the MettaGrid worlds do.

Blocking: round-robin. Send one slot an observation, await its action or the
decision timeout, apply, advance. This matches the cogweb turn-based games.

### Fan-out and payload

Variants sweep slot count (2, 4, 8, 16) and payload size (1 KiB, 16 KiB,
256 KiB, 2 MiB). Payloads are deterministic JSON generated before the measured
phase. Both endpoints set `max_size` to 4 MiB and disable compression.

### Other captures

- Event-loop lag sampler (10 ms period) in both game and player.
- cgroup v2 `cpu.stat`, `cpu.max`, `memory.current`, `memory.peak` once per
  second and at phase boundaries. Missing files are recorded as unknown.
- `gc.callbacks` begin/end pairs.
- Protocol ping RTT: each player sends one ping per second and records the
  pong latency from `ClientConnection.ping()`.

## Delivery

### results.json

Aggregates only, flat, declared in `results_schema` with
`additionalProperties: false`. `scores` is `0.0` per slot: this is an
infrastructure experiment and a latency-derived score would reward workload
avoidance. Field prefixes follow the frozen vocabulary so a future trusted
reader can map them: `player_connect_*`, `player_turn_duration_*`,
`game_step_duration_*`, `episode_loop_*`. All durations in seconds.

### Replay

Gzip JSONL with a header record. Record types: `metadata`, `lifecycle`,
`clock`, `step`, `turn`, `ping`, `resource`, `gc`, `artifact`, `summary`. It
is the full evidence. The game serves `/client/replay` and `/replay` so the
platform can view it in replay mode.

### Player artifact zip

Per slot: `metadata.json`, `lifecycle.jsonl`, `connect_attempts.jsonl`,
`turns.jsonl`, `clock_anchors.jsonl`, `pings.jsonl`, `resources.jsonl`,
`loop_lag.jsonl`, `gc.jsonl`, `summary.json`, plus `game_summary.json`, the
game-side aggregate the game sends in its `final` message. The last file
matters because the raw `results.json` route is Softmax-team-only, while
policy artifacts are readable by the policy owner.

### Logs

Single-line JSON for milestones, connection attempts, a 10 s summary, artifact
outcomes, and the final summary. Never payloads, tokens, or URLs.

### Publication order

1. Stop observations, drain replies to a bounded deadline.
2. Request timing flush from every player.
3. Players build and PUT their zips, then send `artifact_done` with duration.
4. Game sends `final` carrying the game summary; players write it into the zip
   and re-PUT.
5. Game writes replay, then results, each atomically (temp file plus rename
   for `file://`).

## Players

One image, three commands:

| Policy | Behaviour |
|---|---|
| echo | decode, noop, encode, send |
| busy | same, plus a deterministic loop burning `--think-cpu-ms` of thread CPU per turn |
| slow-start | sleeps `--connect-delay-seconds` before the first connect (bundled as 5 s) |

All keep `ping_timeout=None` (metta `packages/coworld/tests/test_coworld_player_keepalive.py`).

## Variants

| Variant | Question |
|---|---|
| `baseline-2p-20ms` | RTT, scheduling, startup baseline |
| `fanout-4p`, `fanout-8p`, `fanout-16p` | scaling with slots |
| `tick-2p-24fps`, `tick-2p-50ms` | production tick rates |
| `payload-sweep` | size-dependent cost, two passes over four sizes |
| `blocking-turns` | per-decision wait |
| `long-10000-ticks` | GC, keepalive, drift |

Certification fixture: one of each bundled player (echo, busy 5 ms,
slow-start 5 s), 1 KiB, 2 warmup and 20 measured ticks. The platform
requires every bundled player to run in the fixture, which is why the
bundled slow-start delay is 5 s rather than the 20 s first planned.

## Repository layout

```
profiler/
  clocks.py       anchors and NTP math
  recorder.py     in-memory records, JSONL, percentiles
  resources.py    cgroup, /proc, loop-lag sampler, gc hooks
  io.py           URI read/write, atomic file writes
  payload.py      deterministic payload generation
  protocol.py     pydantic message models
  config.py       pydantic game config
  game/           server.py, episode.py, results.py, client/*.html
  player/         player.py, connection.py, artifact.py
tests/
tools/            analyze_replay.py, fetch_episode.py
docs/             this doc, player-protocol.md, measurement-reference.md
```

## Implementation order

1. Contract skeleton: game serves all routes, players connect, tiny episode
   produces results, replay, and zips. Local `coworld run-episode` passes.
2. Correlated timing with delayed reports and the flush barrier.
3. Fixed-tick deadlines, per-slot queues, staleness; blocking mode.
4. Clock probes, connect decomposition, resource and loop-lag samplers.
5. Payload and fan-out variants, replay viewer, analysis tool.
6. Certify, push repo, upload, hosted experience requests, read back.
   Done 2026-09-09; results in `docs/results/2026-09-09-hosted-findings.md`.

## Decisions made without asking

- Repo `Metta-AI/coworld-profiler`, public, MIT, default branch `main`.
- `scores` are all zero.
- Cut from v1: binary codec control, compression comparison, packet capture.
- Design doc lives in `docs/designs/` per James's global conventions.
