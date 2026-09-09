"""Message models for the game <-> player websocket protocol.

Every message is a JSON object with a `type` field. Slot identity comes from
the authenticated connection, never from the body. Timestamps are integer
nanoseconds; monotonic ones are only meaningful inside the process that took
them, wall ones are only used for clock alignment.

See docs/player-protocol.md for the sequence.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from profiler import PROTOCOL_VERSION

# --- records the player reports back to the game ---------------------------


class TurnTiming(BaseModel):
    """Player-side timestamps for one observation, reported in a later message.

    The record for observation `seq` cannot travel in the action that answers
    it, because the send timestamps are not known until that action is sent.
    """

    seq: int
    receive_mono_ns: int
    decode_end_mono_ns: int
    think_begin_mono_ns: int
    think_end_mono_ns: int
    think_cpu_ns: int
    encode_begin_mono_ns: int
    encode_end_mono_ns: int
    send_begin_mono_ns: int
    send_end_mono_ns: int
    action_bytes: int
    receive_wall_ns: int
    send_wall_ns: int


class ClockReport(BaseModel):
    probe_id: int
    t2_wall_ns: int
    t3_wall_ns: int


class ConnectAttempt(BaseModel):
    attempt: int
    dns_begin_mono_ns: int
    dns_end_mono_ns: int
    tcp_begin_mono_ns: int
    tcp_end_mono_ns: int
    upgrade_begin_mono_ns: int
    upgrade_end_mono_ns: int
    success: bool
    error: str | None = None
    address_family: str | None = None


class PlayerStartup(BaseModel):
    """Milestones from the player process, all relative to its first Python marker."""

    first_mark_wall_ns: int
    birth_to_first_mark_ns: int | None
    imports_ns: int
    env_read_ns: int
    connect_delay_ns: int
    attempts: list[ConnectAttempt]
    connected_mono_ns: int
    policy: str
    think_cpu_target_ms: float
    python_version: str
    websockets_version: str


# --- game -> player -------------------------------------------------------


class GameHello(BaseModel):
    type: Literal["hello"] = "hello"
    protocol_version: int = PROTOCOL_VERSION
    run_id: str
    slot: int
    slot_count: int
    mode: Literal["fixed_tick", "blocking"]
    step_seconds: float
    game_first_mark_wall_ns: int


class Observation(BaseModel):
    type: Literal["observation"] = "observation"
    run_id: str
    seq: int
    tick: int
    phase: Literal["warmup", "measure"]
    cell_id: str
    payload: list[list[int]]


class ClockProbe(BaseModel):
    type: Literal["clock_probe"] = "clock_probe"
    probe_id: int
    t1_wall_ns: int


class FlushRequest(BaseModel):
    type: Literal["flush"] = "flush"


class Final(BaseModel):
    """Last message. Carries the game-side summary so each player can store it in its artifact."""

    type: Literal["final"] = "final"
    game_summary: dict


GameMessage = Annotated[GameHello | Observation | ClockProbe | FlushRequest | Final, Field(discriminator="type")]
game_message_adapter: TypeAdapter[GameHello | Observation | ClockProbe | FlushRequest | Final] = TypeAdapter(GameMessage)


# --- player -> game -------------------------------------------------------


class PlayerHello(BaseModel):
    type: Literal["hello"] = "hello"
    protocol_version: int = PROTOCOL_VERSION
    startup: PlayerStartup


class Action(BaseModel):
    type: Literal["action"] = "action"
    run_id: str
    reply_to: int
    action: Literal["noop"] = "noop"
    completed_timings: list[TurnTiming] = Field(default_factory=list)
    clock_reports: list[ClockReport] = Field(default_factory=list)


class ClockReply(BaseModel):
    """Immediate reply to a probe; T2/T3 follow in a ClockReport since they are not known yet."""

    type: Literal["clock_reply"] = "clock_reply"
    probe_id: int


class FlushReply(BaseModel):
    type: Literal["flush_reply"] = "flush_reply"
    completed_timings: list[TurnTiming]
    clock_reports: list[ClockReport]
    ping_rtts_ns: list[int]
    loop_lag_p99_ns: int | None
    artifact_publish_ns: int | None
    artifact_bytes: int | None
    artifact_error: str | None


PlayerMessage = Annotated[PlayerHello | Action | ClockReply | FlushReply, Field(discriminator="type")]
player_message_adapter: TypeAdapter[PlayerHello | Action | ClockReply | FlushReply] = TypeAdapter(PlayerMessage)
