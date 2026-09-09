"""The instrumented player. One image, three behaviours selected by --policy.

    python -m profiler.player.player --policy echo
    python -m profiler.player.player --policy busy --think-cpu-ms 5
    python -m profiler.player.player --policy slow-start --connect-delay-seconds 20

Every stage of every turn is stamped (p0..p5 in the design doc) and reported
back to the game in the *next* action, since a message cannot carry its own
send timestamp. Startup milestones, connect attempts, loop lag, GC, resource
samples, and ping RTTs are buffered and written into the per-slot artifact
zip at flush time.
"""

from __future__ import annotations

_IMPORTS_BEGIN = __import__("time").monotonic_ns()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

import websockets  # noqa: E402
from websockets.asyncio.client import ClientConnection  # noqa: E402

from profiler import FIRST_MARK_MONO_NS, FIRST_MARK_WALL_NS, SCHEMA_VERSION  # noqa: E402
from profiler.clocks import take_anchor  # noqa: E402
from profiler.player.artifact import ArtifactOutcome, publish_zip  # noqa: E402
from profiler.player.connection import connect_with_retries  # noqa: E402
from profiler.protocol import (  # noqa: E402
    Action,
    ClockProbe,
    ClockReply,
    ClockReport,
    Final,
    FlushReply,
    FlushRequest,
    GameHello,
    Observation,
    PlayerHello,
    PlayerStartup,
    TurnTiming,
    game_message_adapter,
)
from profiler.recorder import Recorder, summarize_ns  # noqa: E402
from profiler.resources import GcRecorder, LoopLagSampler, process_birth, sample_resources  # noqa: E402

_IMPORTS_END = time.monotonic_ns()


def log(event: str, **details: Any) -> None:
    """Single-line JSON to stdout: the runner collects it per slot. Never payloads, tokens, or URLs."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "role": "player",
        "event": event,
        "mono_ns": time.monotonic_ns(),
        "wall_ns": time.time_ns(),
        **details,
    }
    print(json.dumps(record, separators=(",", ":")), flush=True)


def burn_cpu(target_ns: int) -> int:
    """Consume `target_ns` of thread CPU time with deterministic arithmetic. Returns CPU ns actually used.

    CPU time, not wall time: a wall-clock loop would do less work when the
    container is throttled and hide exactly the effect we are looking for.
    """
    start = time.thread_time_ns()
    value = 0x9E3779B9
    while time.thread_time_ns() - start < target_ns:
        for _ in range(256):
            value = (value * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
    return time.thread_time_ns() - start


class Player:
    def __init__(self, *, policy: str, think_cpu_ms: float, connect_delay_seconds: float) -> None:
        self.policy = policy
        self.think_cpu_ns = int(think_cpu_ms * 1e6)
        self.connect_delay_seconds = connect_delay_seconds
        self.recorder = Recorder()
        self.hello: GameHello | None = None
        self.completed: list[TurnTiming] = []
        self.clock_reports: list[ClockReport] = []
        self.ping_rtts_ns: list[int] = []
        self.loop_lag = LoopLagSampler()
        self.gc = GcRecorder()
        self.artifact: ArtifactOutcome | None = None
        self.turn_count = 0
        self.startup: PlayerStartup | None = None
        self._done = asyncio.Event()

    # ----- lifecycle -----------------------------------------------------

    async def run(self) -> None:
        env_begin = time.monotonic_ns()
        url = os.environ["COWORLD_PLAYER_WS_URL"]
        env_end = time.monotonic_ns()
        self.recorder.add("lifecycle", {"event": "python.first_mark", "mono_ns": FIRST_MARK_MONO_NS, "wall_ns": FIRST_MARK_WALL_NS})
        self.recorder.add("lifecycle", {"event": "imports", "mono_ns": _IMPORTS_BEGIN, "duration_ns": _IMPORTS_END - _IMPORTS_BEGIN})
        self.recorder.add("clock_anchor", take_anchor())
        self.recorder.add("resource", sample_resources())
        birth = process_birth()
        log("startup", policy=self.policy, imports_ns=_IMPORTS_END - _IMPORTS_BEGIN, birth_to_first_mark_ns=birth.birth_to_first_mark_ns)
        self.gc.install()

        delay_begin = time.monotonic_ns()
        if self.connect_delay_seconds > 0:
            await asyncio.sleep(self.connect_delay_seconds)
        delay_end = time.monotonic_ns()

        connection, attempts = await connect_with_retries(url)
        connected = time.monotonic_ns()
        for attempt in attempts:
            self.recorder.add("connect_attempt", attempt)
        log(
            "connected",
            attempts=len(attempts),
            dns_ns=attempts[-1].dns_end_mono_ns - attempts[-1].dns_begin_mono_ns,
            tcp_ns=attempts[-1].tcp_end_mono_ns - attempts[-1].tcp_begin_mono_ns,
            upgrade_ns=attempts[-1].upgrade_end_mono_ns - attempts[-1].upgrade_begin_mono_ns,
        )
        self.startup = PlayerStartup(
            first_mark_wall_ns=FIRST_MARK_WALL_NS,
            birth_to_first_mark_ns=birth.birth_to_first_mark_ns,
            imports_ns=_IMPORTS_END - _IMPORTS_BEGIN,
            env_read_ns=env_end - env_begin,
            connect_delay_ns=delay_end - delay_begin,
            attempts=attempts,
            connected_mono_ns=connected,
            policy=self.policy,
            think_cpu_target_ms=self.think_cpu_ns / 1e6,
            python_version=platform.python_version(),
            websockets_version=websockets.__version__,
        )
        self.loop_lag.start()
        sampler = asyncio.create_task(self._sample_periodically(connection))
        try:
            await self._session(connection)
        finally:
            sampler.cancel()
            await self.loop_lag.stop()
            await connection.close()

    async def _sample_periodically(self, connection: ClientConnection) -> None:
        # Stagger pings by slot so sixteen players do not ping in lockstep.
        await asyncio.sleep(((self.hello.slot if self.hello else 0) % 8) * 0.1)
        while True:
            self.recorder.add("resource", sample_resources())
            self.recorder.add("clock_anchor", take_anchor())
            try:
                pong = await connection.ping()
                started = time.monotonic_ns()
                latency = await asyncio.wait_for(pong, timeout=5.0)
                self.ping_rtts_ns.append(int(latency * 1e9))
                self.recorder.add("ping", {"mono_ns": started, "rtt_ns": int(latency * 1e9)})
            except TimeoutError:
                self.recorder.add("ping", {"mono_ns": time.monotonic_ns(), "rtt_ns": None, "timeout": True})
            except websockets.exceptions.ConnectionClosed:
                return
            await asyncio.sleep(1.0)

    # ----- the message loop ----------------------------------------------

    async def _session(self, connection: ClientConnection) -> None:
        async for raw in connection:
            p0 = time.monotonic_ns()
            p0_wall = time.time_ns()
            if isinstance(raw, bytes):
                raw = raw.decode()
            message = game_message_adapter.validate_json(raw)
            p1 = time.monotonic_ns()
            if isinstance(message, GameHello):
                self.hello = message
                self.recorder.add("lifecycle", {"event": "hello.received", "mono_ns": p0, "slot": message.slot})
                assert self.startup is not None
                await connection.send(PlayerHello(startup=self.startup).model_dump_json())
                self.recorder.add("lifecycle", {"event": "hello.sent", "mono_ns": time.monotonic_ns()})
                log("ready", slot=message.slot, mode=message.mode, step_seconds=message.step_seconds)
            elif isinstance(message, Observation):
                await self._answer(connection, message, p0=p0, p0_wall=p0_wall, p1=p1, observation_bytes=len(raw))
            elif isinstance(message, ClockProbe):
                reply = ClockReply(probe_id=message.probe_id).model_dump_json()
                t3 = time.time_ns()
                await connection.send(reply)
                self.clock_reports.append(ClockReport(probe_id=message.probe_id, t2_wall_ns=p0_wall, t3_wall_ns=t3))
            elif isinstance(message, FlushRequest):
                await self._flush(connection)
            elif isinstance(message, Final):
                self.recorder.add("lifecycle", {"event": "final.received", "mono_ns": p0})
                self._publish(game_summary=message.game_summary)
                log("final", turns=self.turn_count, artifact=self.artifact.model_dump() if self.artifact else None)
                return
        log("connection_closed_by_game", turns=self.turn_count)

    async def _answer(
        self, connection: ClientConnection, observation: Observation, *, p0: int, p0_wall: int, p1: int, observation_bytes: int
    ) -> None:
        p2a = time.monotonic_ns()
        cpu_used = burn_cpu(self.think_cpu_ns) if self.think_cpu_ns > 0 else 0
        p2b = time.monotonic_ns()
        # Ship the previous turn's completed record and any pending clock reports.
        action = Action(run_id=observation.run_id, reply_to=observation.seq, completed_timings=self.completed, clock_reports=self.clock_reports)
        self.completed = []
        self.clock_reports = []
        p3a = time.monotonic_ns()
        text = action.model_dump_json()
        p3b = time.monotonic_ns()
        p4_wall = time.time_ns()
        p4 = time.monotonic_ns()
        await connection.send(text)
        p5 = time.monotonic_ns()
        timing = TurnTiming(
            seq=observation.seq,
            receive_mono_ns=p0,
            decode_end_mono_ns=p1,
            think_begin_mono_ns=p2a,
            think_end_mono_ns=p2b,
            think_cpu_ns=cpu_used,
            encode_begin_mono_ns=p3a,
            encode_end_mono_ns=p3b,
            send_begin_mono_ns=p4,
            send_end_mono_ns=p5,
            action_bytes=len(text.encode()),
            receive_wall_ns=p0_wall,
            send_wall_ns=p4_wall,
        )
        self.completed.append(timing)
        self.recorder.add(
            "turn",
            {
                **timing.model_dump(mode="json"),
                "tick": observation.tick,
                "phase": observation.phase,
                "cell_id": observation.cell_id,
                "observation_bytes": observation_bytes,
            },
        )
        self.turn_count += 1

    async def _flush(self, connection: ClientConnection) -> None:
        self.recorder.add("lifecycle", {"event": "flush.received", "mono_ns": time.monotonic_ns()})
        self._publish(game_summary=None)
        lag = summarize_ns([sample.lag_ns for sample in self.loop_lag.samples])
        reply = FlushReply(
            completed_timings=self.completed,
            clock_reports=self.clock_reports,
            ping_rtts_ns=self.ping_rtts_ns,
            loop_lag_p99_ns=int(lag.p99_s * 1e9) if lag.p99_s is not None else None,
            artifact_publish_ns=self.artifact.publish_ns if self.artifact else None,
            artifact_bytes=self.artifact.byte_count if self.artifact else None,
            artifact_error=self.artifact.error if self.artifact else None,
        )
        self.completed = []
        self.clock_reports = []
        await connection.send(reply.model_dump_json())
        log("flushed", artifact=self.artifact.model_dump() if self.artifact else None)

    # ----- artifact ------------------------------------------------------

    def _publish(self, *, game_summary: dict[str, Any] | None) -> None:
        for pause in self.gc.pauses:
            self.recorder.add("gc", pause)
        self.gc.pauses.clear()
        for sample in self.loop_lag.samples:
            self.recorder.add("loop_lag", sample)
        self.loop_lag.samples.clear()
        summary = {
            "policy": self.policy,
            "slot": self.hello.slot if self.hello else None,
            "turns": self.turn_count,
            "ping_rtt": summarize_ns(self.ping_rtts_ns).model_dump(),
            "loop_lag": summarize_ns([s["lag_ns"] for s in self.recorder.records("loop_lag")]).model_dump(),
            "processing": summarize_ns([t["send_begin_mono_ns"] - t["receive_mono_ns"] for t in self.recorder.records("turn")]).model_dump(),
            "think_cpu": summarize_ns([t["think_cpu_ns"] for t in self.recorder.records("turn")]).model_dump(),
            "record_counts": self.recorder.counts(),
            "dropped_records": self.recorder.dropped,
            "previous_artifact": self.artifact.model_dump() if self.artifact else None,
        }
        files = {
            "metadata.json": json.dumps(
                {"schema_version": SCHEMA_VERSION, "startup": self.startup.model_dump(mode="json") if self.startup else None, "python": sys.version},
                indent=1,
            ).encode(),
            "lifecycle.jsonl": self.recorder.to_jsonl("lifecycle"),
            "connect_attempts.jsonl": self.recorder.to_jsonl("connect_attempt"),
            "turns.jsonl": self.recorder.to_jsonl("turn"),
            "clock_anchors.jsonl": self.recorder.to_jsonl("clock_anchor"),
            "pings.jsonl": self.recorder.to_jsonl("ping"),
            "resources.jsonl": self.recorder.to_jsonl("resource"),
            "loop_lag.jsonl": self.recorder.to_jsonl("loop_lag"),
            "gc.jsonl": self.recorder.to_jsonl("gc"),
            "summary.json": json.dumps(summary, indent=1).encode(),
        }
        if game_summary is not None:
            files["game_summary.json"] = json.dumps(game_summary, indent=1).encode()
        self.artifact = publish_zip(files)
        self.recorder.add("lifecycle", {"event": "artifact.published", "mono_ns": time.monotonic_ns(), **self.artifact.model_dump()})


def main() -> None:
    parser = argparse.ArgumentParser(description="coworld-profiler instrumented player")
    parser.add_argument("--policy", choices=["echo", "busy", "slow-start"], default="echo")
    parser.add_argument("--think-cpu-ms", type=float, default=0.0, help="thread CPU to burn per turn (busy policy)")
    parser.add_argument("--connect-delay-seconds", type=float, default=0.0, help="sleep before first connect (slow-start policy)")
    args = parser.parse_args()
    think = args.think_cpu_ms if args.policy == "busy" else 0.0
    delay = args.connect_delay_seconds if args.policy == "slow-start" else 0.0
    asyncio.run(Player(policy=args.policy, think_cpu_ms=think, connect_delay_seconds=delay).run())


if __name__ == "__main__":
    main()
