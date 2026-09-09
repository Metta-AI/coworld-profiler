"""Aggregate the episode's records into results.json and build the replay document.

Result field names follow the vocabulary frozen by metta spec 0080 so a future
trusted reader can map them: `player_connect_*`, `player_turn_duration_*`,
`game_step_duration_*`, `episode_loop_*`. Durations are seconds.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from profiler import SCHEMA_VERSION
from profiler.clocks import estimate_offset
from profiler.config import GameConfig
from profiler.game.episode import Episode
from profiler.recorder import Distribution, summarize_ns


class SlotResult(BaseModel):
    player_slot: int
    policy: str | None
    connected: bool
    connect_attempts: int | None
    player_connect_dns_s: float | None
    player_connect_tcp_s: float | None
    player_connect_upgrade_s: float | None
    player_connect_ready_s: float | None  # game listening -> slot protocol-ready (game clock)
    player_turn_duration: Distribution
    websocket_application_rtt: Distribution
    player_processing_duration: Distribution
    player_think_cpu: Distribution
    websocket_transport_residual: Distribution
    game_websocket_send_await: Distribution
    player_websocket_send_await: Distribution
    ping_rtt: Distribution
    observation_count: int
    applied_count: int
    late_discarded_count: int
    unanswered_count: int
    dropped_before_send_count: int
    timeout_count: int
    timing_report_missing_count: int
    negative_residual_count: int
    clock_offset_ns: int | None
    clock_offset_lower_ns: int | None
    clock_offset_upper_ns: int | None
    clock_probe_delay_ns: int | None
    clock_probe_valid_count: int
    player_loop_lag_p99_s: float | None
    player_artifact_publish_s: float | None
    player_artifact_bytes: int | None
    player_artifact_error: str | None


class CellResult(BaseModel):
    cell_id: str
    payload_target_bytes: int
    observation_bytes: int
    measure_ticks: int
    player_turn_duration: Distribution
    websocket_application_rtt: Distribution
    websocket_transport_residual: Distribution
    game_encode: Distribution


class Results(BaseModel):
    """The results artifact. `extra="forbid"` makes the generated schema close the object."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    scores: list[float]
    run_id: str
    mode: str
    seed: int
    player_slot_count: int
    step_seconds: float
    step_count: int
    warmup_step_count: int
    measurement_complete: bool
    game_bootstrap_import_s: float | None
    game_bootstrap_config_read_s: float | None
    game_bootstrap_config_decode_s: float | None
    game_bootstrap_payload_build_s: float | None
    game_bootstrap_server_start_s: float | None
    game_process_birth_to_first_mark_s: float | None
    game_listening_to_first_health_s: float | None
    game_listening_to_first_global_s: float | None
    player_connect_ready_max_s: float | None
    player_connect_ready_min_s: float | None
    episode_loop_measurement_s: float | None
    game_step_duration: Distribution
    game_tick_lag: Distribution
    game_event_loop_lag: Distribution
    game_gc_pause: Distribution
    player_turn_duration: Distribution
    websocket_application_rtt: Distribution
    websocket_transport_residual: Distribution
    player_turn_duration_worst_slot_p99_s: float | None
    action_late_fraction: float | None
    action_unanswered_count: int
    observation_dropped_count: int
    timing_report_missing_count: int
    game_cpu_throttled_s: float | None
    game_memory_peak_bytes: int | None
    replay_size_bytes: int | None
    replay_prepare_s: float | None
    replay_publish_s: float | None
    player_artifact_success_count: int
    trace_dropped_record_count: int
    slots: list[SlotResult]
    cells: list[CellResult] = Field(default_factory=list)


def _seconds(ns: int | None) -> float | None:
    return None if ns is None else ns / 1e9


def _measured(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [turn for turn in turns if turn["phase"] == "measure"]


def _dist(turns: list[dict[str, Any]], key: str) -> Distribution:
    return summarize_ns([turn[key] for turn in turns if turn.get(key) is not None])


def build_results(episode: Episode, *, bootstrap: dict[str, int | None], game_context: dict[str, Any]) -> Results:
    config: GameConfig = episode.config
    recorder = episode.recorder
    turns = recorder.records("turn")
    steps = recorder.records("step")
    measured_turns = _measured(turns)
    listening_mono = bootstrap.get("listening_mono_ns")

    slot_results: list[SlotResult] = []
    for slot, state in episode.slots.items():
        slot_turns = [turn for turn in measured_turns if turn["slot"] == slot]
        startup = state.startup.startup if state.startup is not None else None
        last_attempt = startup.attempts[-1] if startup is not None and startup.attempts else None
        flush = state.flush_reply
        applied = sum(1 for turn in slot_turns if turn["outcome"] == "applied")
        late = sum(1 for turn in slot_turns if turn["outcome"] == "late_discarded")
        unanswered = sum(1 for turn in slot_turns if turn["outcome"] == "unanswered")
        timeouts = sum(1 for turn in slot_turns if turn["outcome"] == "timeout")
        answered = [turn for turn in slot_turns if turn.get("rtt_ns") is not None]
        offset = estimate_offset(state.probe_samples)
        slot_results.append(
            SlotResult(
                player_slot=slot,
                policy=startup.policy if startup else None,
                connected=state.ready,
                connect_attempts=len(startup.attempts) if startup else None,
                player_connect_dns_s=_seconds(last_attempt.dns_end_mono_ns - last_attempt.dns_begin_mono_ns) if last_attempt else None,
                player_connect_tcp_s=_seconds(last_attempt.tcp_end_mono_ns - last_attempt.tcp_begin_mono_ns) if last_attempt else None,
                player_connect_upgrade_s=_seconds(last_attempt.upgrade_end_mono_ns - last_attempt.upgrade_begin_mono_ns) if last_attempt else None,
                player_connect_ready_s=_seconds(state.ready_mono_ns - listening_mono) if state.ready_mono_ns and listening_mono else None,
                player_turn_duration=_dist(answered, "turn_ns"),
                websocket_application_rtt=_dist(answered, "rtt_ns"),
                player_processing_duration=_dist(answered, "processing_ns"),
                player_think_cpu=summarize_ns([turn["player"]["think_cpu_ns"] for turn in answered if turn.get("player")]),
                websocket_transport_residual=_dist(answered, "transport_residual_ns"),
                game_websocket_send_await=summarize_ns(
                    [turn["send_end_mono_ns"] - turn["send_begin_mono_ns"] for turn in slot_turns if turn["send_end_mono_ns"]]
                ),
                player_websocket_send_await=summarize_ns(
                    [turn["player"]["send_end_mono_ns"] - turn["player"]["send_begin_mono_ns"] for turn in answered if turn.get("player")]
                ),
                ping_rtt=summarize_ns(flush.ping_rtts_ns if flush else []),
                observation_count=len(slot_turns),
                applied_count=applied,
                late_discarded_count=late,
                unanswered_count=unanswered,
                dropped_before_send_count=state.dropped_before_send,
                timeout_count=timeouts,
                timing_report_missing_count=sum(1 for turn in answered if not turn.get("player")),
                negative_residual_count=sum(1 for turn in answered if (turn.get("transport_residual_ns") or 0) < 0),
                clock_offset_ns=offset.offset_ns,
                clock_offset_lower_ns=offset.offset_lower_ns,
                clock_offset_upper_ns=offset.offset_upper_ns,
                clock_probe_delay_ns=offset.delay_ns,
                clock_probe_valid_count=offset.valid_count,
                player_loop_lag_p99_s=_seconds(flush.loop_lag_p99_ns) if flush else None,
                player_artifact_publish_s=_seconds(flush.artifact_publish_ns) if flush else None,
                player_artifact_bytes=flush.artifact_bytes if flush else None,
                player_artifact_error=flush.artifact_error if flush else None,
            )
        )

    cells: list[CellResult] = []
    for cell in config.cells:
        cell_turns = [turn for turn in measured_turns if turn["cell_id"] == cell.cell_id and turn.get("rtt_ns") is not None]
        cells.append(
            CellResult(
                cell_id=cell.cell_id,
                payload_target_bytes=cell.target_bytes,
                observation_bytes=episode._payload_bytes.get(cell.cell_id, 0),
                measure_ticks=cell.measure_ticks,
                player_turn_duration=_dist(cell_turns, "turn_ns"),
                websocket_application_rtt=_dist(cell_turns, "rtt_ns"),
                websocket_transport_residual=_dist(cell_turns, "transport_residual_ns"),
                game_encode=summarize_ns([turn["send_begin_mono_ns"] - turn["encode_begin_mono_ns"] for turn in cell_turns]),
            )
        )

    answered_all = [turn for turn in measured_turns if turn.get("rtt_ns") is not None]
    measured_steps = [step for step in steps if step["phase"] == "measure"]
    ready_times = [state.ready_mono_ns - listening_mono for state in episode.slots.values() if state.ready_mono_ns and listening_mono]
    late_total = sum(1 for turn in measured_turns if turn["outcome"] in ("late_discarded", "unanswered", "timeout"))
    resources = recorder.records("resource")
    throttled = None
    if len(resources) >= 2 and resources[0].get("cpu_throttled_usec") is not None and resources[-1].get("cpu_throttled_usec") is not None:
        throttled = (resources[-1]["cpu_throttled_usec"] - resources[0]["cpu_throttled_usec"]) / 1e6
    peaks = [sample["memory_peak_bytes"] for sample in resources if sample.get("memory_peak_bytes") is not None]
    loop_lifecycle = [event for event in recorder.records("lifecycle") if event["event"] == "episode.loop"]

    return Results(
        scores=[0.0 for _ in range(config.slot_count)],
        run_id=episode.run_id,
        mode=config.mode,
        seed=config.seed,
        player_slot_count=config.slot_count,
        step_seconds=config.step_seconds,
        step_count=sum(cell.measure_ticks for cell in config.cells),
        warmup_step_count=sum(cell.warmup_ticks for cell in config.cells),
        measurement_complete=len(measured_steps) == sum(cell.measure_ticks for cell in config.cells),
        game_bootstrap_import_s=_seconds(bootstrap.get("imports_ns")),
        game_bootstrap_config_read_s=_seconds(bootstrap.get("config_read_ns")),
        game_bootstrap_config_decode_s=_seconds(bootstrap.get("config_decode_ns")),
        game_bootstrap_payload_build_s=_seconds(bootstrap.get("payload_build_ns")),
        game_bootstrap_server_start_s=_seconds(bootstrap.get("server_start_ns")),
        game_process_birth_to_first_mark_s=_seconds(bootstrap.get("birth_to_first_mark_ns")),
        game_listening_to_first_health_s=_seconds(bootstrap.get("first_health_ns")),
        game_listening_to_first_global_s=_seconds(bootstrap.get("first_global_ns")),
        player_connect_ready_max_s=_seconds(max(ready_times)) if ready_times else None,
        player_connect_ready_min_s=_seconds(min(ready_times)) if ready_times else None,
        episode_loop_measurement_s=_seconds(loop_lifecycle[0]["duration_ns"]) if loop_lifecycle else None,
        game_step_duration=summarize_ns([step["step_duration_ns"] for step in measured_steps]),
        game_tick_lag=summarize_ns([step["lag_ns"] for step in measured_steps]),
        game_event_loop_lag=summarize_ns([sample["lag_ns"] for sample in recorder.records("loop_lag")]),
        game_gc_pause=summarize_ns([pause["duration_ns"] for pause in recorder.records("gc")]),
        player_turn_duration=_dist(answered_all, "turn_ns"),
        websocket_application_rtt=_dist(answered_all, "rtt_ns"),
        websocket_transport_residual=_dist(answered_all, "transport_residual_ns"),
        player_turn_duration_worst_slot_p99_s=max(
            (s.player_turn_duration.p99_s for s in slot_results if s.player_turn_duration.p99_s is not None), default=None
        ),
        action_late_fraction=(late_total / len(measured_turns)) if measured_turns else None,
        action_unanswered_count=sum(1 for turn in measured_turns if turn["outcome"] == "unanswered"),
        observation_dropped_count=sum(state.dropped_before_send for state in episode.slots.values()),
        timing_report_missing_count=sum(slot.timing_report_missing_count for slot in slot_results),
        game_cpu_throttled_s=throttled,
        game_memory_peak_bytes=max(peaks) if peaks else None,
        replay_size_bytes=game_context.get("replay_size_bytes"),
        replay_prepare_s=_seconds(game_context.get("replay_prepare_ns")),
        replay_publish_s=_seconds(game_context.get("replay_publish_ns")),
        player_artifact_success_count=sum(
            1 for slot in slot_results if slot.player_artifact_publish_s is not None and slot.player_artifact_error is None
        ),
        trace_dropped_record_count=recorder.dropped,
        slots=slot_results,
        cells=cells,
    )


REPLAY_RECORD_ORDER = ["metadata", "lifecycle", "player_startup", "clock_probe", "clock", "step", "turn", "loop_lag", "resource", "gc", "summary"]


def replay_view(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The subset of a replay the viewer needs: header, summary, per-slot turn series (thinned), steps (thinned)."""
    header = next((r for r in records if r.get("record_type") == "header"), {})
    summary = next((r for r in records if r.get("record_type") == "summary"), {})
    turns = [r for r in records if r.get("record_type") == "turn"]
    steps = [r for r in records if r.get("record_type") == "step"]
    lifecycle = [r for r in records if r.get("record_type") == "lifecycle"]
    clocks = [r for r in records if r.get("record_type") == "clock"]

    def thin(items: list[dict[str, Any]], limit: int = 4000) -> list[dict[str, Any]]:
        if len(items) <= limit:
            return items
        stride = len(items) / limit
        return [items[int(i * stride)] for i in range(limit)]

    series: dict[int, list[dict[str, Any]]] = {}
    for turn in turns:
        series.setdefault(turn["slot"], []).append(
            {
                "tick": turn["tick"],
                "phase": turn["phase"],
                "cell_id": turn["cell_id"],
                "rtt_ns": turn.get("rtt_ns"),
                "processing_ns": turn.get("processing_ns"),
                "residual_ns": turn.get("transport_residual_ns"),
                "outcome": turn["outcome"],
                "send_mono_ns": turn["send_begin_mono_ns"],
            }
        )
    return {
        "type": "replay",
        "header": header,
        "summary": summary,
        "slots": {str(slot): thin(items) for slot, items in series.items()},
        "steps": thin(
            [
                {
                    "tick": s["tick"],
                    "phase": s["phase"],
                    "lag_ns": s["lag_ns"],
                    "step_duration_ns": s["step_duration_ns"],
                    "applied": s["applied"],
                    "noop": s["noop"],
                    "actual_mono_ns": s["actual_mono_ns"],
                }
                for s in steps
            ]
        ),
        "lifecycle": lifecycle[:500],
        "clocks": clocks,
    }
