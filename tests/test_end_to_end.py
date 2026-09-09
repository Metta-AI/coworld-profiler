"""Run the real game server and real player processes over loopback, then check every artifact.

This is the local stand-in for `coworld run-episode`: same env contract, no
Docker. It also exercises the replay-mode server against the produced replay.
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(port: int, process: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"game exited early with {process.returncode}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise AssertionError("game never became healthy")


def _run_episode(tmp_path: Path, config: dict, policies: list[list[str]]) -> tuple[dict, list[dict], list[Path], str]:
    port = _free_port()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tokens = [f"tok{slot}" for slot in range(len(policies))]
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps({**config, "tokens": tokens}))
    env = {
        **os.environ,
        "COGAME_HOST": "127.0.0.1",
        "COGAME_PORT": str(port),
        "COGAME_CONFIG_URI": f"file://{config_path}",
        "COGAME_RESULTS_URI": f"file://{workspace / 'results.json'}",
        "COGAME_SAVE_REPLAY_URI": f"file://{workspace / 'replay'}",
    }
    game = subprocess.Popen(
        [sys.executable, "-m", "profiler.game.server"], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    players: list[subprocess.Popen[str]] = []
    try:
        _wait_healthy(port, game)
        # Contract checks the runner performs before launching players.
        assert httpx.get(f"http://127.0.0.1:{port}/client/player?slot=0&token={tokens[0]}", timeout=5).status_code == 200
        assert httpx.get(f"http://127.0.0.1:{port}/client/global", timeout=5).status_code == 200
        for slot, argv in enumerate(policies):
            player_env = {
                **os.environ,
                "COWORLD_PLAYER_WS_URL": f"ws://127.0.0.1:{port}/player?slot={slot}&token={tokens[slot]}",
                "COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": f"file://{workspace / f'policy_artifact_{slot}.zip'}",
            }
            players.append(
                subprocess.Popen(
                    [sys.executable, "-m", "profiler.player.player", *argv],
                    env=player_env,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        game_output, _ = game.communicate(timeout=120)
        assert game.returncode == 0, game_output
        for player in players:
            output, _ = player.communicate(timeout=30)
            assert player.returncode == 0, output
    finally:
        for process in [game, *players]:
            if process.poll() is None:
                process.kill()
    results = json.loads((workspace / "results.json").read_text())
    replay_lines = gzip.decompress((workspace / "replay").read_bytes()).decode().splitlines()
    records = [json.loads(line) for line in replay_lines]
    zips = sorted(workspace.glob("policy_artifact_*.zip"))
    return results, records, zips, game_output


BASE_CONFIG = {
    "players": [{"name": "Echo A"}, {"name": "Echo B"}],
    "seed": 3,
    "mode": "fixed_tick",
    "step_seconds": 0.02,
    "cells": [{"cell_id": "json-1k", "target_bytes": 1024, "warmup_ticks": 5, "measure_ticks": 40}],
    "player_connect_timeout_seconds": 30,
    "drain_seconds": 5,
    "clock_probes_per_window": 4,
}


@pytest.mark.timeout(180)
def test_fixed_tick_episode_produces_all_artifacts(tmp_path: Path) -> None:
    results, records, zips, output = _run_episode(tmp_path, BASE_CONFIG, [["--policy", "echo"], ["--policy", "busy", "--think-cpu-ms", "2"]])

    assert results["scores"] == [0.0, 0.0]
    assert results["measurement_complete"] is True
    assert results["step_count"] == 40 and results["warmup_step_count"] == 5
    assert results["player_slot_count"] == 2
    assert results["websocket_application_rtt"]["count"] > 30
    assert results["websocket_application_rtt"]["p50_s"] < 0.02
    assert results["player_turn_duration"]["p99_s"] is not None
    assert results["game_step_duration"]["count"] == 40
    assert results["game_bootstrap_import_s"] > 0
    assert results["game_listening_to_first_health_s"] is not None
    assert results["timing_report_missing_count"] == 0
    assert results["player_artifact_success_count"] == 2

    slots = {slot["player_slot"]: slot for slot in results["slots"]}
    assert slots[0]["policy"] == "echo" and slots[1]["policy"] == "busy"
    assert slots[1]["player_think_cpu"]["p50_s"] >= 0.002
    assert slots[1]["player_processing_duration"]["p50_s"] > slots[0]["player_processing_duration"]["p50_s"]
    for slot in slots.values():
        assert slot["connect_attempts"] == 1
        assert slot["player_connect_dns_s"] is not None and slot["player_connect_tcp_s"] is not None
        assert slot["clock_probe_valid_count"] >= 4
        assert slot["clock_offset_lower_ns"] <= slot["clock_offset_ns"] <= slot["clock_offset_upper_ns"]
        assert slot["negative_residual_count"] == 0
        assert slot["ping_rtt"]["count"] >= 0

    types = {record["record_type"] for record in records}
    assert {"header", "lifecycle", "step", "turn", "clock", "summary", "resource", "loop_lag"} <= types
    assert records[0]["format"] == "coworld-profiler-replay"
    assert "tokens" not in records[0]["config"]
    turns = [record for record in records if record["record_type"] == "turn" and record["phase"] == "measure"]
    assert all(turn["outcome"] in ("applied", "late_discarded", "unanswered") for turn in turns)
    assert sum(1 for turn in turns if turn["outcome"] == "applied") > 60

    assert len(zips) == 2
    with zipfile.ZipFile(zips[0]) as archive:
        names = set(archive.namelist())
        assert {"metadata.json", "turns.jsonl", "summary.json", "game_summary.json", "connect_attempts.jsonl"} <= names
        game_summary = json.loads(archive.read("game_summary.json"))
        assert game_summary["results"]["run_id"] == results["run_id"]
        player_turns = [json.loads(line) for line in archive.read("turns.jsonl").decode().splitlines()]
        assert len(player_turns) == 45

    log_events = [json.loads(line)["event"] for line in output.splitlines() if line.startswith("{")]
    assert "listening" in log_events and "finalized" in log_events
    assert "tok0" not in output


@pytest.mark.timeout(180)
def test_blocking_mode_and_replay_view(tmp_path: Path) -> None:
    config = {
        **BASE_CONFIG,
        "mode": "blocking",
        "decision_timeout_seconds": 1.0,
        "cells": [{"cell_id": "json-1k", "target_bytes": 1024, "warmup_ticks": 2, "measure_ticks": 20}],
    }
    results, records, _zips, _output = _run_episode(tmp_path, config, [["--policy", "echo"], ["--policy", "echo"]])
    assert results["mode"] == "blocking"
    assert results["step_count"] == 20
    assert results["action_late_fraction"] == 0.0
    turns = [record for record in records if record["record_type"] == "turn" and record["phase"] == "measure"]
    assert all(turn["outcome"] == "applied" for turn in turns)

    # Replay mode: the same image must serve the replay it wrote, over /replay, with no players or results.
    view = _serve_replay(tmp_path / "workspace" / "replay")
    assert view["type"] == "replay"
    assert view["summary"]["run_id"] == results["run_id"]
    assert set(view["slots"]) == {"0", "1"}
    assert len(view["steps"]) == 22


def _serve_replay(replay_path: Path) -> dict:
    import asyncio

    import websockets

    port = _free_port()
    env = {**os.environ, "COGAME_HOST": "127.0.0.1", "COGAME_PORT": str(port), "COGAME_LOAD_REPLAY_URI": f"file://{replay_path}"}
    game = subprocess.Popen(
        [sys.executable, "-m", "profiler.game.server"], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        _wait_healthy(port, game)
        assert httpx.get(f"http://127.0.0.1:{port}/client/replay", timeout=5).status_code == 200

        async def fetch() -> dict:
            async with websockets.connect(f"ws://127.0.0.1:{port}/replay", max_size=None) as socket:
                return json.loads(await socket.recv())

        return asyncio.run(fetch())
    finally:
        game.kill()
        game.wait()
