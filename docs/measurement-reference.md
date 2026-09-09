# Measurement reference

What each number in `results.json` means, how it was taken, and what it can
and cannot explain. Durations are seconds unless the name ends in `_ns` or
`_bytes`. Distributions carry `count`, `sum_s`, `p50_s`, `p90_s`, `p99_s`,
`max_s` (nearest-rank percentiles over the measured phase only; warmup ticks
are excluded).

## Turn decomposition

For one observation/action pair the game stamps g0 (encode begin), g1 (send
call), g2 (send await returned), g3 (reply received), g4 (reply decoded).
The player stamps p0 (received), p1 (decoded), p2a/p2b (think), p3a/p3b
(encode), p4 (send call), p5 (send await returned) and reports them in its
next action.

| Field | Formula | Meaning |
|---|---|---|
| `websocket_application_rtt` | g3 - g1 | Application-level round trip as the game sees it |
| `player_turn_duration` | g4 - g1 | Same plus the game's decode; the hosted analogue of the frozen `player.turn` span |
| `player_processing_duration` (per slot) | p4 - p0 | Everything the player did between receiving and sending |
| `player_think_cpu` (per slot) | thread CPU inside think | The busy player's real CPU burn |
| `websocket_transport_residual` | (g3 - g1) - (p4 - p0) | What is left after subtracting player processing: socket buffers, framing, library work, scheduling, and wire time. Not pure network transit. |
| `game_websocket_send_await` (per slot) | g2 - g1 | How long the game's send call blocked. Overlaps the RTT; do not add it. |
| `player_websocket_send_await` (per slot) | p5 - p4 | Same on the player side |
| `game_encode` (per cell) | g1 - g0 | JSON encode of the observation; grows with payload |

`negative_residual_count` counts turns whose residual came out negative.
That is impossible physically and indicates a timing report mismatch; those
samples are kept and flagged, never clamped.

## Scheduling and staleness

| Field | Meaning |
|---|---|
| `game_tick_lag` | Actual tick start minus scheduled start on the game's monotonic clock |
| `game_event_loop_lag` | How late a 10 ms periodic sleep woke up in the game process |
| `player_loop_lag_p99_s` (per slot) | Same in the player process |
| `game_step_duration` | The synchronous state update only; the hosted analogue of the frozen `game.step` span |
| `action_late_fraction` | Measured-phase observations whose action was late, unanswered, or timed out, over all measured observations |
| `late_discarded_count` (per slot) | Actions that arrived after the next tick began and were never applied |
| `dropped_before_send_count` (per slot) | Observations replaced in the one-deep send queue before they were sent, which happens when a send blocks for more than one tick |

## Connect

| Field | Meaning |
|---|---|
| `player_connect_dns_s` | `getaddrinfo` on the Service hostname, in the player process |
| `player_connect_tcp_s` | TCP connect to the resolved address |
| `player_connect_upgrade_s` | Websocket handshake on the connected socket |
| `player_connect_ready_s` | Game listening to the slot's protocol hello, on the game clock. Includes pod scheduling, image pull, the platform init container's health polling, and the player's own startup. |
| `player_connect_ready_min_s` / `max_s` | Across slots |

The platform init container polls the game Service before the player
process starts, so DNS and TCP here are a warm path. Cold Service
propagation is unobservable from inside the pod.

## Bootstrap

| Field | Meaning |
|---|---|
| `game_process_birth_to_first_mark_s` | From `/proc/self/stat` start time to our first Python marker. Includes exec and interpreter start. One clock tick resolution. None outside Linux. |
| `game_bootstrap_import_s` | Importing FastAPI, uvicorn, websockets, pydantic |
| `game_bootstrap_config_read_s` / `_decode_s` | Reading `COGAME_CONFIG_URI` and validating it |
| `game_bootstrap_payload_build_s` | Generating every cell's payload |
| `game_bootstrap_server_start_s` | From config ready to uvicorn's startup event |
| `game_listening_to_first_health_s` | Listening to the first `/healthz` request, which is the worker's first observation of the game |
| `game_listening_to_first_global_s` | Listening to the first `/global` viewer connection (the worker opens one before launching players) |

## Clocks

Per slot: `clock_offset_ns` is the player wall clock minus the game wall
clock from the minimum-delay valid NTP-style probe;
`clock_offset_lower_ns` and `clock_offset_upper_ns` bound it without the
symmetric-delay assumption; `clock_probe_delay_ns` is that probe's round
trip; `clock_probe_valid_count` says how many probes were usable. The replay
holds every probe and every bracketed wall/monotonic anchor, so drift is
visible over long episodes.

## Context

| Field | Meaning |
|---|---|
| `game_cpu_throttled_s` | Delta of cgroup v2 `cpu.stat throttled_usec` over the episode. None when the file is unavailable. |
| `game_memory_peak_bytes` | Max of cgroup `memory.peak` samples |
| `game_gc_pause` | Python GC pauses from `gc.callbacks` |
| `replay_prepare_s` / `replay_publish_s` / `replay_size_bytes` | Building and writing the replay |
| `player_artifact_publish_s` / `_bytes` / `_error` (per slot) | The player's first zip PUT, as reported in `flush_reply` |

## What this cannot see

- Kubernetes scheduling, image pull, and the init container's failed polls.
- The worker's own S3 uploads after the game exits.
- Whether the game and a player landed on the same node. The Service path is
  measured; placement must be joined from outside.
- Anything after the last log line: process exit is an external observation.

## Mapping to the frozen span vocabulary (metta spec 0080)

| Results prefix | Span or metric |
|---|---|
| `player_connect_*` | `player.connect` |
| `player_turn_duration*` | `player.turn`, `player.turn.duration` |
| `game_step_duration*` | `game.step`, `game.step.duration` |
| `episode_loop_measurement_s` | `episode.loop` |

Nothing here is emitted as telemetry. A trusted reader on the backend could
turn these aggregates into the frozen metrics; the shape is designed for
that, but the wiring is not part of this repo.
