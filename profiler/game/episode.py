"""The measured episode: per-slot connections, the tick loop, and turn correlation.

The engine is transport-agnostic: a `SlotTransport` is anything with
`send_text` / `close`, so tests can drive it with in-memory pairs and the
server drives it with FastAPI websockets. Receiving is pushed in through
`handle_message`, stamped by the caller the instant the bytes arrived.

Timestamps follow docs/designs/2026-09-09-profiler-design.md: g0..g4 are the
game-side stamps of one turn, p0..p5 arrive later from the player.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol

from pydantic import BaseModel

from profiler.clocks import ClockProbeSample, take_anchor
from profiler.config import GameConfig
from profiler.payload import build_payload, encoded_size
from profiler.protocol import (
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
    TurnTiming,
    player_message_adapter,
)
from profiler.recorder import Recorder


class SlotTransport(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class TurnRecord(BaseModel):
    """Game-side view of one observation/action pair, filled in as events arrive."""

    slot: int
    seq: int
    tick: int
    phase: str
    cell_id: str
    observation_bytes: int
    scheduled_mono_ns: int | None = None
    enqueue_mono_ns: int = 0
    encode_begin_mono_ns: int = 0  # g0
    send_begin_mono_ns: int = 0  # g1
    send_end_mono_ns: int = 0  # g2
    send_wall_ns: int = 0
    receive_mono_ns: int | None = None  # g3
    decode_end_mono_ns: int | None = None  # g4
    receive_wall_ns: int | None = None
    action_bytes: int | None = None
    outcome: str = "pending"  # applied | late_discarded | unanswered | dropped_before_send | timeout
    arrival_late_ticks: int | None = None
    player: TurnTiming | None = None

    @property
    def rtt_ns(self) -> int | None:
        return None if self.receive_mono_ns is None else self.receive_mono_ns - self.send_begin_mono_ns

    @property
    def turn_ns(self) -> int | None:
        return None if self.decode_end_mono_ns is None else self.decode_end_mono_ns - self.send_begin_mono_ns

    @property
    def processing_ns(self) -> int | None:
        return None if self.player is None else self.player.send_begin_mono_ns - self.player.receive_mono_ns

    @property
    def transport_residual_ns(self) -> int | None:
        rtt, processing = self.rtt_ns, self.processing_ns
        return None if rtt is None or processing is None else rtt - processing


class StepRecord(BaseModel):
    tick: int
    phase: str
    cell_id: str
    scheduled_mono_ns: int
    actual_mono_ns: int
    lag_ns: int
    step_duration_ns: int
    applied: int
    noop: int


class LifecycleEvent(BaseModel):
    event: str
    mono_ns: int
    wall_ns: int
    slot: int | None = None
    duration_ns: int | None = None
    detail: dict[str, Any] | None = None


class _Slot:
    def __init__(self, slot: int) -> None:
        self.slot = slot
        self.transport: SlotTransport | None = None
        self.ready = False
        self.startup: PlayerHello | None = None
        self.accept_mono_ns: int | None = None
        self.ready_mono_ns: int | None = None
        self.turns: dict[int, TurnRecord] = {}
        self.pending: TurnRecord | None = None  # one-deep send queue
        self.pending_event = asyncio.Event()
        self.sender: asyncio.Task[None] | None = None
        self.last_applied_seq = -1
        self.action_events: dict[int, asyncio.Event] = {}
        self.probes: dict[int, dict[str, int]] = {}
        self.probe_samples: list[ClockProbeSample] = []
        self.probe_replied: dict[int, asyncio.Event] = {}
        self.flush_reply: FlushReply | None = None
        self.flush_event = asyncio.Event()
        self.dropped_before_send = 0
        self.disconnected = False


class Episode:
    def __init__(self, config: GameConfig, *, run_id: str, recorder: Recorder) -> None:
        self.config = config
        self.run_id = run_id
        self.recorder = recorder
        self.slots = {slot: _Slot(slot) for slot in range(config.slot_count)}
        self.started = False
        self.done = False
        self.tick = 0
        self.stage = "lobby"
        self.play_task: asyncio.Task[None] | None = None
        self._payloads: dict[str, list[list[int]]] = {}
        self._payload_bytes: dict[str, int] = {}
        self._schedule: list[tuple[int, str, str]] = []  # (tick, phase, cell_id)
        self._state = [0] * config.slot_count
        self._probe_counter = 0
        self.first_mark_wall_ns = 0
        self.on_done: list[asyncio.Event] = []
        self.game_summary: dict[str, Any] = {}

    # ----- setup ---------------------------------------------------------

    def prepare_payloads(self) -> dict[str, int]:
        """Generate every cell's payload up front so the hot path only encodes. Returns generation ns per cell."""
        timings: dict[str, int] = {}
        for cell in self.config.cells:
            started = time.monotonic_ns()
            rows = build_payload(self.config.seed ^ hash(cell.cell_id) & 0xFFFF, cell.target_bytes)
            self._payloads[cell.cell_id] = rows
            self._payload_bytes[cell.cell_id] = encoded_size(rows)
            timings[cell.cell_id] = time.monotonic_ns() - started
            for _ in range(cell.warmup_ticks):
                self._schedule.append((len(self._schedule), "warmup", cell.cell_id))
            for _ in range(cell.measure_ticks):
                self._schedule.append((len(self._schedule), "measure", cell.cell_id))
        return timings

    def lifecycle(self, event: str, *, slot: int | None = None, duration_ns: int | None = None, detail: dict | None = None) -> None:
        anchor = take_anchor()
        self.recorder.add(
            "lifecycle",
            LifecycleEvent(event=event, mono_ns=anchor.mono_before_ns, wall_ns=anchor.wall_ns, slot=slot, duration_ns=duration_ns, detail=detail),
        )

    # ----- connections ---------------------------------------------------

    def valid_credentials(self, slot: int, token: str) -> bool:
        return 0 <= slot < self.config.slot_count and self.config.tokens[slot] == token

    async def attach(self, slot: int, transport: SlotTransport) -> None:
        state = self.slots[slot]
        if state.transport is not None:
            raise ValueError(f"slot {slot} already connected")
        state.transport = transport
        state.accept_mono_ns = time.monotonic_ns()
        self.lifecycle("player.accept", slot=slot)
        hello = GameHello(
            run_id=self.run_id,
            slot=slot,
            slot_count=self.config.slot_count,
            mode=self.config.mode,
            step_seconds=self.config.step_seconds,
            game_first_mark_wall_ns=self.first_mark_wall_ns,
        )
        await transport.send_text(hello.model_dump_json())
        state.sender = asyncio.create_task(self._sender(state))

    def detach(self, slot: int) -> None:
        state = self.slots[slot]
        state.disconnected = True
        state.transport = None
        self.lifecycle("player.disconnect", slot=slot)
        if state.sender is not None:
            state.sender.cancel()
        for event in state.action_events.values():
            event.set()
        state.flush_event.set()

    def connected_slots(self) -> list[int]:
        return [slot for slot, state in self.slots.items() if state.transport is not None and not state.disconnected]

    def ready_slots(self) -> list[int]:
        return [slot for slot, state in self.slots.items() if state.ready]

    async def handle_message(self, slot: int, raw: str, *, receive_mono_ns: int, receive_wall_ns: int) -> None:
        """Called by the transport the instant a text frame is available. `receive_mono_ns` is g3."""
        state = self.slots[slot]
        message = player_message_adapter.validate_json(raw)
        decode_end = time.monotonic_ns()
        if isinstance(message, PlayerHello):
            state.startup = message
            state.ready = True
            state.ready_mono_ns = receive_mono_ns
            self.lifecycle("player.ready", slot=slot, detail={"policy": message.startup.policy})
            self.recorder.add("player_startup", {"slot": slot, **message.startup.model_dump(mode="json")})
            return
        if isinstance(message, Action):
            self._absorb_reports(state, message.completed_timings, message.clock_reports)
            turn = state.turns.get(message.reply_to)
            if turn is None:
                return
            turn.receive_mono_ns = receive_mono_ns
            turn.decode_end_mono_ns = decode_end
            turn.receive_wall_ns = receive_wall_ns
            turn.action_bytes = len(raw.encode())
            if turn.outcome == "pending":
                turn.outcome = "answered"
            event = state.action_events.get(message.reply_to)
            if event is not None:
                event.set()
            return
        if isinstance(message, ClockReply):
            probe = state.probes.get(message.probe_id)
            if probe is not None:
                probe["t4"] = receive_wall_ns
                probe["t4_mono"] = receive_mono_ns
            replied = state.probe_replied.get(message.probe_id)
            if replied is not None:
                replied.set()
            return
        if isinstance(message, FlushReply):
            self._absorb_reports(state, message.completed_timings, message.clock_reports)
            state.flush_reply = message
            state.flush_event.set()
            return

    def _absorb_reports(self, state: _Slot, timings: list[TurnTiming], reports: list[ClockReport]) -> None:
        for timing in timings:
            turn = state.turns.get(timing.seq)
            if turn is not None:
                turn.player = timing
        for report in reports:
            probe = state.probes.get(report.probe_id)
            if probe is None or "t4" not in probe:
                continue
            state.probe_samples.append(
                ClockProbeSample(probe_id=report.probe_id, t1_ns=probe["t1"], t2_ns=report.t2_wall_ns, t3_ns=report.t3_wall_ns, t4_ns=probe["t4"])
            )

    # ----- sending -------------------------------------------------------

    def _enqueue(self, state: _Slot, turn: TurnRecord) -> None:
        if state.pending is not None:
            state.pending.outcome = "dropped_before_send"
            state.dropped_before_send += 1
        turn.enqueue_mono_ns = time.monotonic_ns()
        state.pending = turn
        state.turns[turn.seq] = turn
        state.pending_event.set()

    async def _sender(self, state: _Slot) -> None:
        """One active send per connection; a newer pending observation replaces an unsent one."""
        while True:
            await state.pending_event.wait()
            state.pending_event.clear()
            turn = state.pending
            if turn is None or state.transport is None:
                continue
            state.pending = None
            payload = self._payloads[turn.cell_id]
            turn.encode_begin_mono_ns = time.monotonic_ns()
            text = Observation(
                run_id=self.run_id, seq=turn.seq, tick=turn.tick, phase=turn.phase, cell_id=turn.cell_id, payload=payload
            ).model_dump_json()
            turn.observation_bytes = len(text.encode())
            turn.send_wall_ns = time.time_ns()
            turn.send_begin_mono_ns = time.monotonic_ns()
            try:
                await state.transport.send_text(text)
            except Exception as error:  # noqa: BLE001 - a dead socket is a slot outcome, not a game failure
                turn.outcome = "send_failed"
                self.lifecycle("player.send_failed", slot=state.slot, detail={"error": repr(error)})
                self.detach(state.slot)
                return
            turn.send_end_mono_ns = time.monotonic_ns()

    async def _send_control(self, state: _Slot, text: str) -> bool:
        if state.transport is None:
            return False
        try:
            await state.transport.send_text(text)
        except Exception:  # noqa: BLE001
            self.detach(state.slot)
            return False
        return True

    # ----- the episode ---------------------------------------------------

    def start_when_ready(self) -> None:
        if self.play_task is not None:
            return
        if len(self.ready_slots()) == self.config.slot_count:
            self.play_task = asyncio.create_task(self.run())

    async def start_after_timeout(self) -> None:
        await asyncio.sleep(self.config.player_connect_timeout_seconds)
        if self.play_task is None:
            self.lifecycle("player.connect_timeout", detail={"ready": self.ready_slots()})
            self.play_task = asyncio.create_task(self.run())

    async def run(self) -> None:
        self.started = True
        self.lifecycle("episode.start", detail={"ready_slots": self.ready_slots()})
        self.stage = "clock_probes_before"
        await self._clock_probe_window("before")
        self.stage = "measure"
        loop_started = time.monotonic_ns()
        if self.config.mode == "fixed_tick":
            await self._run_fixed_tick()
        else:
            await self._run_blocking()
        self.lifecycle("episode.loop", duration_ns=time.monotonic_ns() - loop_started)
        self.stage = "clock_probes_after"
        await self._clock_probe_window("after")
        self.stage = "drain"
        await self._drain()
        self.stage = "flush"
        await self._flush()
        self.done = True
        self.stage = "done"
        self.lifecycle("episode.end")
        for event in self.on_done:
            event.set()

    async def _run_fixed_tick(self) -> None:
        step_ns = int(self.config.step_seconds * 1e9)
        origin = time.monotonic_ns()
        order = list(self.slots)
        for tick, phase, cell_id in self._schedule:
            scheduled = origin + tick * step_ns
            remaining = scheduled - time.monotonic_ns()
            if remaining > 0:
                await asyncio.sleep(remaining / 1e9)
            else:
                await asyncio.sleep(0)  # overdue: still yield so socket tasks run
            actual = time.monotonic_ns()
            self.tick = tick
            applied, noop = self._apply_actions(tick)
            step_started = time.monotonic_ns()
            self._advance_state(tick)
            step_duration = time.monotonic_ns() - step_started
            self.recorder.add(
                "step",
                StepRecord(
                    tick=tick,
                    phase=phase,
                    cell_id=cell_id,
                    scheduled_mono_ns=scheduled,
                    actual_mono_ns=actual,
                    lag_ns=actual - scheduled,
                    step_duration_ns=step_duration,
                    applied=applied,
                    noop=noop,
                ),
            )
            # Rotate enqueue order so no slot is always first.
            order = order[1:] + order[:1]
            for slot in order:
                state = self.slots[slot]
                if state.transport is None or not state.ready:
                    continue
                turn = TurnRecord(slot=slot, seq=tick, tick=tick, phase=phase, cell_id=cell_id, observation_bytes=0, scheduled_mono_ns=scheduled)
                self._enqueue(state, turn)
        # Give the last tick's actions one more period to arrive before the loop ends.
        await asyncio.sleep(self.config.step_seconds)
        self._apply_actions(len(self._schedule))

    def _apply_actions(self, tick: int) -> tuple[int, int]:
        """Apply, for every slot, the action answering the previous tick. Classify everything older."""
        applied = noop = 0
        for state in self.slots.values():
            target = tick - 1
            turn = state.turns.get(target)
            if turn is None:
                continue
            if turn.receive_mono_ns is not None and turn.outcome == "answered":
                turn.outcome = "applied"
                turn.arrival_late_ticks = 0
                state.last_applied_seq = target
                applied += 1
            else:
                noop += 1
            # Anything older that is still pending or answered late is now stale.
            for seq, older in state.turns.items():
                if seq < target and older.outcome in ("pending", "answered"):
                    older.outcome = "late_discarded" if older.receive_mono_ns is not None else "pending"
        return applied, noop

    def _advance_state(self, tick: int) -> None:
        """A small deterministic update so `game.step` measures real, if tiny, work."""
        for index in range(len(self._state)):
            value = (self._state[index] * 1103515245 + 12345 + tick) & 0x7FFFFFFF
            self._state[index] = value

    async def _run_blocking(self) -> None:
        timeout_ns = int(self.config.decision_timeout_seconds * 1e9)
        for tick, phase, cell_id in self._schedule:
            scheduled = time.monotonic_ns()
            self.tick = tick
            applied = noop = 0
            for slot, state in self.slots.items():
                if state.transport is None or not state.ready:
                    noop += 1
                    continue
                turn = TurnRecord(slot=slot, seq=tick, tick=tick, phase=phase, cell_id=cell_id, observation_bytes=0, scheduled_mono_ns=scheduled)
                event = asyncio.Event()
                state.action_events[tick] = event
                self._enqueue(state, turn)
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout_ns / 1e9)
                except TimeoutError:
                    turn.outcome = "timeout"
                    noop += 1
                else:
                    if turn.receive_mono_ns is not None:
                        turn.outcome = "applied"
                        turn.arrival_late_ticks = 0
                        applied += 1
                    else:
                        noop += 1
                finally:
                    state.action_events.pop(tick, None)
            step_started = time.monotonic_ns()
            self._advance_state(tick)
            step_duration = time.monotonic_ns() - step_started
            self.recorder.add(
                "step",
                StepRecord(
                    tick=tick,
                    phase=phase,
                    cell_id=cell_id,
                    scheduled_mono_ns=scheduled,
                    actual_mono_ns=scheduled,
                    lag_ns=0,
                    step_duration_ns=step_duration,
                    applied=applied,
                    noop=noop,
                ),
            )

    # ----- clock probes --------------------------------------------------

    async def _clock_probe_window(self, window: str) -> None:
        for _ in range(self.config.clock_probes_per_window):
            await asyncio.gather(*(self._probe_slot(state, window) for state in self.slots.values() if state.transport is not None))
            await asyncio.sleep(0.05)

    async def _probe_slot(self, state: _Slot, window: str) -> None:
        self._probe_counter += 1
        probe_id = self._probe_counter
        replied = asyncio.Event()
        state.probe_replied[probe_id] = replied
        t1_mono = time.monotonic_ns()
        t1 = time.time_ns()
        state.probes[probe_id] = {"t1": t1, "t1_mono": t1_mono, "window": {"before": 0, "during": 1, "after": 2}[window]}
        if not await self._send_control(state, ClockProbe(probe_id=probe_id, t1_wall_ns=t1).model_dump_json()):
            return
        try:
            await asyncio.wait_for(replied.wait(), timeout=2.0)
        except TimeoutError:
            state.probes[probe_id]["timeout"] = 1
        finally:
            state.probe_replied.pop(probe_id, None)

    # ----- end of episode ------------------------------------------------

    async def _drain(self) -> None:
        """Let outstanding replies land, bounded by drain_seconds, then classify leftovers."""
        deadline = time.monotonic_ns() + int(min(self.config.drain_seconds, 2.0) * 1e9)
        while time.monotonic_ns() < deadline:
            if all(turn.outcome != "pending" for state in self.slots.values() for turn in state.turns.values()):
                break
            await asyncio.sleep(0.05)
        for state in self.slots.values():
            for turn in state.turns.values():
                if turn.outcome == "pending":
                    turn.outcome = "unanswered"
                elif turn.outcome == "answered":
                    turn.outcome = "late_discarded"

    async def _flush(self) -> None:
        for state in self.slots.values():
            if state.transport is not None:
                await self._send_control(state, FlushRequest().model_dump_json())
        deadline = time.monotonic_ns() + int(self.config.drain_seconds * 1e9)
        for state in self.slots.values():
            if state.transport is None:
                continue
            remaining = (deadline - time.monotonic_ns()) / 1e9
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(state.flush_event.wait(), timeout=remaining)
            except TimeoutError:
                self.lifecycle("player.flush_timeout", slot=state.slot)
        self.lifecycle("flush.complete", detail={"replied": [s for s, st in self.slots.items() if st.flush_reply is not None]})

    async def send_final(self, game_summary: dict[str, Any]) -> None:
        self.game_summary = game_summary
        text = Final(game_summary=game_summary).model_dump_json()
        for state in self.slots.values():
            if state.transport is not None:
                await self._send_control(state, text)

    # ----- export --------------------------------------------------------

    def export_turns(self) -> None:
        for state in self.slots.values():
            for turn in sorted(state.turns.values(), key=lambda record: record.seq):
                record = turn.model_dump(mode="json")
                record["rtt_ns"] = turn.rtt_ns
                record["turn_ns"] = turn.turn_ns
                record["processing_ns"] = turn.processing_ns
                record["transport_residual_ns"] = turn.transport_residual_ns
                self.recorder.add("turn", record)
            for probe_id, probe in state.probes.items():
                self.recorder.add("clock_probe", {"slot": state.slot, "probe_id": probe_id, **probe})
            for sample in state.probe_samples:
                self.recorder.add(
                    "clock",
                    {
                        "slot": state.slot,
                        **sample.model_dump(mode="json"),
                        "offset_ns": sample.offset_ns,
                        "delay_ns": sample.delay_ns,
                        "valid": sample.valid,
                    },
                )

    def snapshot(self) -> dict[str, Any]:
        """Small state for the /global viewer and the lobby message."""
        return {
            "type": "state",
            "run_id": self.run_id,
            "stage": self.stage,
            "tick": self.tick,
            "total_ticks": len(self._schedule),
            "mode": self.config.mode,
            "step_seconds": self.config.step_seconds,
            "slot_count": self.config.slot_count,
            "connected": self.connected_slots(),
            "ready": self.ready_slots(),
            "started": self.started,
            "done": self.done,
            "player_names": [player.name for player in self.config.players],
        }


def parse_json(text: str) -> dict[str, Any]:
    return json.loads(text)
