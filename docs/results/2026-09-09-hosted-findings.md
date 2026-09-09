# Hosted findings, 2026-09-09

Superseded by the five-round research report in `docs/reports/hosted-timing-2026-09-09.md`; kept as the round 1 and 2 record.

First measurements of the hosted Observatory episode path using
coworld-profiler 0.1.0 (coworld `cow_f4c8de04-7d32-4bc8-a316-8cd8cda6a729`,
image `coworld-profiler:coworld-8e682f09bc97`, source commit `d400b90`).

Data: 5 hosted smoke episodes (certification fixture, 3 echo slots), then
two rounds of experience requests over every variant. Round 1 (19
episodes) ran with the busy player's argv malformed, so its slot 0 never
connected and every game waited the full 180 s connect timeout before
starting with one seat empty; its echo-slot measurements are valid and are
used for the startup medians. Round 2 (19 episodes) is the clean run with a
5 ms busy player in slot 0 and echo players elsewhere. Raw artifacts are in
the episode requests listed at the end. Reproduce with
`tools/summarize_experiments.py`.

Every duration below is milliseconds unless stated.

## 1. Websocket round trip between game and player pods

Pooled over the 61 echo slots in round 2's fixed-tick and blocking
variants (1 KiB observations):

| Quantity | p50 | p99 |
|---|---|---|
| application round trip, game send to game receive | 1.13 | 1.92 |
| player processing (decode, noop, encode, send) | 0.08 | |
| transport residual, round trip minus processing | 1.04 | 1.84 |
| game `send` await | 0.01 | |
| player `send` await | 0.07 | |
| protocol ping/pong RTT | 0.54 | |

For the 17 busy slots (5 ms of thread CPU per turn): round trip p50 5.81,
processing p50 5.10 with 5.02 of CPU inside the think loop, residual p50
0.73. The residual is the same in both populations, which is what should
happen if the decomposition is right. No negative residuals occurred in
160 slot-episodes, and no timing reports went missing.

So: **a 1 KiB observation and its reply cost about 1 ms of transport per
turn between pods**, half of which the raw ping shows as the wire and
framing floor. At the 20 ms tick that Crewrift-style games use, transport
is 5 percent of the budget. Late actions in round 2 at 20 ms: 105 of
79,000 echo observations (0.13 percent), 34 of 35,000 busy observations.

## 2. Payload size

Two passes over four sizes, 2 slots, 20 ms tick, round 2 (busy slot
present). First pass, ascending:

| Observation | RTT p50 | RTT p99 | residual p50 | game encode p50 |
|---|---|---|---|---|
| 1 KiB | 1.0 to 1.3 | 6.1 | 0.7 | 0.03 |
| 16 KiB | 1.7 to 2.1 | 6.8 | 0.8 to 0.9 | 0.29 |
| 256 KiB | 9.9 | 24.8 | 0.9 to 1.0 | 3.8 |
| 2 MiB | 252 to 262 | 333 | 213 to 223 | 31 |

Up to 256 KiB the wire cost barely moves; the RTT growth is JSON encode on
the game and decode on the player. At 2 MiB the residual jumps to over 200
ms: the player's websocket library has to reassemble a 2 MiB text message
before `recv` returns, and that time lands in the residual by construction.
CTF's sprite frames are multi-megabyte, so this is the regime that game
lives in, and a 20 ms tick cannot be met by a Python game at that size: the
game loop fell 10 s behind (tick lag p99 10,270) and 64 percent of actions
were late. The descending second pass shows the aftermath: the 1 KiB cell
that follows the 2 MiB cells has a p99 of about 1,100 ms because the
catch-up ticks run back to back and the socket buffers still hold stale
large frames. Round 1, with one slot, showed the same shape at lower
magnitude (2 MiB RTT p50 150 to 190, residual 118 to 151).

Practical reading: at multi-MB frames the per-turn cost is dominated by
serialization and message reassembly, not by the network, and it is well
over one Crewrift tick.

## 3. Fan-out

Round 2 medians across the fixed-tick variants at 1 KiB and 20 ms:

| Slots | RTT p50 (pooled) | residual p50 | residual p99 | game loop lag p99 | late |
|---|---|---|---|---|---|
| 2 | 3.24 (busy slot dominates the pool) | 0.72 | 0.97 | 1.05 | 0.05 % |
| 4 | 0.76 | 0.65 | 1.13 | 1.05 | 0.06 % |
| 8 | 1.00 | 0.87 | 1.49 | 1.31 | 0.006 % |
| 16 | 1.38 | 1.25 | 2.64 | 3.22 | 0.31 % |

Per-slot RTT p50 in a 16-slot episode ranged 1.04 to 1.53 for echo slots
with no slot systematically first or last (send order rotates). The game's
`send` await never exceeded 0.05 at p99 even at 16 slots, so the game's own
event loop, not the sockets, is where fan-out cost appears: loop lag p99
tripled and GC max pauses rose from 13 to 51.

## 4. Tick rate and blocking mode

24 fps and 50 ms ticks behave like 20 ms at 1 KiB: residual p50 0.82 and
0.90, zero late actions in both. Blocking round-robin decisions (the cogweb
shape) measured per-decision turn time p50 5.43 with the busy slot at 5.51
and the echo slot at 0.49; 2,000 decisions took 6.9 s of loop time.

## 5. Startup: where `first_step_s` goes

Medians over all 43 hosted episodes:

| Interval | median | min | max |
|---|---|---|---|
| game process birth to first Python marker | 66 | 45 | 127 |
| game imports (FastAPI, uvicorn, websockets, pydantic) | 435 | 314 | 555 |
| game config read | 0.1 | | 0.8 |
| game server start (config ready to uvicorn ready) | 22 | 19 | 60 |
| game listening to first `/healthz` hit | 463 | 58 | 817 |
| game listening to first `/global` viewer | 794 | 604 | 1,811 |
| game listening to first slot ready | 5,222 | 3,768 | 10,327 |
| game listening to last slot ready | 6,502 | 4,412 | 24,956 |
| player process birth to first marker | 67 | | |
| player imports | 236 | | |
| player DNS resolve of the Service | 4.3 | 3.1 | 10.4 |
| player TCP connect | 0.56 | 0.24 | 1.15 |
| player websocket upgrade | 2.0 | 1.3 | 7.9 |

The game is ready about 0.6 s after its process starts. The worker sees it
within its 1 s health poll. Then each player slot takes 4 to 25 s to
appear, of which the player's own process accounts for about 0.3 s and the
network path for under 10 ms. **Everything else, 4 to 24 s per slot, is pod
scheduling, image pull, and the platform init container's Service poll,
none of which is observable from inside the containers.** The 16-slot
episodes had the widest spread (4.8 to 8.1 s in round 2, up to 25 s in
round 1).

## 6. Finalize

| Interval | median | max |
|---|---|---|
| game replay prepare (gzip JSONL of all records) | 199 | 1,238 |
| game replay write to the shared workdir | 0.37 | 3.3 |
| player artifact zip PUT through the worker's upload server | 120 | 1,137 |

The worker's own S3 uploads after the game exits are not visible here.

## 7. Context: CPU, GC, clocks

- Neither the game nor the player pods had a CPU quota (`cpu.max` quota is
  unlimited, period 100 ms); cgroup throttling was 0 everywhere. A
  player that sizes thread pools from `os.cpu_count()` sees the whole node.
- Python GC pauses in the game: max 13 in 2-slot episodes, 51 at 16 slots,
  69 in the 10,000-tick run (321 collections). These are partly the
  profiler's own record buffers and are an upper bound on what a Python
  game with a similar allocation pattern would see.
- Wall clock offset between game and player pods, from 8 NTP-style probes
  per window: median 0.07, range -0.16 to 0.29, with median bound width
  0.72. Node clocks are tight enough that one-way estimates are meaningful
  to about half a millisecond.
- The 10,000-tick episode (202 s of loop) showed no drift in RTT or
  residual and 0.065 percent late actions.

## 8. Platform behaviour observed along the way

- When a player container exits immediately (round 1's argparse failure),
  the game waits the full `player_connect_timeout_seconds` (180 s here),
  runs with the seat empty, and the episode is recorded as `completed`
  with a 0 score for that policy. Nothing in the episode row says the seat
  never connected. Only the game's own results and the missing policy
  artifact reveal it.
- `coworld upload-policy` splits `--run` values exactly as given; a value
  containing a space becomes one argv token.
- The first `upload-policy` completion call returned HTTP 500 once and
  succeeded on retry with identical input.

## Episode requests

Round 1 (busy slot absent):
`xreq_9f13c600`, `xreq_ae3d919d`, `xreq_051ba6dc`, `xreq_4ff8c270`,
`xreq_37f28745`, `xreq_b4fb3be7`, `xreq_3fea4d83`, `xreq_6258c93c`,
`xreq_6bfe3480`.

Round 2 (clean):
`xreq_07a5d464` baseline x3, `xreq_c30fdeff` fanout-4p, `xreq_b0db21a8`
fanout-8p, `xreq_a7112d58` fanout-16p, `xreq_3f510b34` tick-24fps,
`xreq_df59e7a2` tick-50ms, `xreq_a351bb00` payload-sweep, `xreq_de281355`
blocking-turns, `xreq_376e28bc` long-10000-ticks.

Smoke: `ereq_290f7936`, `ereq_89a36168`, `ereq_8a7405b3`, `ereq_c000f207`,
`ereq_c082a437`.
