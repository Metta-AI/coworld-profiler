# Player protocol

Text websocket frames, one JSON object each, discriminated by `type`. The
models are in `profiler/protocol.py` and are the source of truth. Slot
identity comes from the `slot` and `token` query parameters of the
`/player` URL the runner hands the player; no message can override it.

All `*_mono_ns` values are `time.monotonic_ns()` in the process that took
them and are meaningless across processes. All `*_wall_ns` values are
`time.time_ns()` and exist only for clock alignment.

## Sequence

```
player                                  game
  |---- connect /player?slot&token ------>|   (bad token: close 1008 before accept)
  |<--- hello ----------------------------|   run_id, slot, mode, step_seconds
  |---- hello (startup milestones) ------>|   slot is now "ready"
  |                                       |   game starts when every slot is ready
  |                                       |   or player_connect_timeout_seconds elapses
  |<--- clock_probe x N ------------------|   T1 in the probe
  |---- clock_reply ---------------------->|   immediately; T2/T3 reported later
  |<--- observation (tick t) -------------|   payload of the current cell
  |---- action (reply_to t) -------------->|   carries completed_timings for t-1
  |        ... repeated per tick ...      |
  |<--- clock_probe x N ------------------|
  |<--- flush ----------------------------|
  |        player builds + PUTs zip       |
  |---- flush_reply ---------------------->|   remaining timings, ping RTTs, artifact outcome
  |<--- final (game_summary) -------------|
  |        player adds game_summary.json  |
  |        to zip and PUTs again, exits   |
```

## Game to player

| type | fields |
|---|---|
| `hello` | `protocol_version`, `run_id`, `slot`, `slot_count`, `mode`, `step_seconds`, `game_first_mark_wall_ns` |
| `observation` | `run_id`, `seq`, `tick`, `phase` (`warmup` or `measure`), `cell_id`, `payload` (list of int rows) |
| `clock_probe` | `probe_id`, `t1_wall_ns` |
| `flush` | none |
| `final` | `game_summary` (the results aggregate plus per-slot rows) |

## Player to game

| type | fields |
|---|---|
| `hello` | `protocol_version`, `startup` (see `PlayerStartup`: first mark, imports, env read, connect attempts with DNS/TCP/upgrade stamps, policy, versions) |
| `action` | `run_id`, `reply_to` (the observation `seq`), `action` (`noop`), `completed_timings` (list of `TurnTiming` for earlier seqs), `clock_reports` |
| `clock_reply` | `probe_id` |
| `flush_reply` | `completed_timings`, `clock_reports`, `ping_rtts_ns`, `loop_lag_p99_ns`, `artifact_publish_ns`, `artifact_bytes`, `artifact_error` |

### TurnTiming

`seq`, `receive_mono_ns` (p0), `decode_end_mono_ns` (p1),
`think_begin_mono_ns` (p2a), `think_end_mono_ns` (p2b), `think_cpu_ns`,
`encode_begin_mono_ns` (p3a), `encode_end_mono_ns` (p3b),
`send_begin_mono_ns` (p4), `send_end_mono_ns` (p5), `action_bytes`,
`receive_wall_ns`, `send_wall_ns`.

The record for `seq` cannot travel in the action answering `seq`, because p4
and p5 are not known until that action has been sent. It travels in the next
action, or in `flush_reply`.

## Rules

- Keep `ping_timeout=None` on the client. The game answers protocol pings;
  the player sends one per second and records the pong latency.
- Both sides set `max_size` to 4 MiB and disable compression.
- In fixed-tick mode the game never waits for a reply. An action that arrives
  after the next tick is recorded as late and never applied.
- In blocking mode the game waits up to `decision_timeout_seconds` per slot.

## Global viewer

`/global` sends a small JSON `state` snapshot on connect and once per
second: `stage`, `tick`, `total_ticks`, `ready`, `connected`, `done`. After
finalization the last snapshot carries `results`. No workload frames are
sent to the viewer.

## Replay

`/replay` sends one JSON document built from the replay bytes: `header`,
`summary`, thinned per-slot turn series, thinned steps, lifecycle events,
clock samples. Replay bytes themselves are gzip JSONL; see
`profiler/game/results.py` for the record order.
