"""The profiler game container: FastAPI + uvicorn, one websocket per player slot.

Contract surface (metta packages/coworld/src/coworld/docs/roles/GAME.md):
GET /healthz, GET /client/{player,global,replay}, WS /player?slot&token,
WS /global, WS /replay (replay mode via COGAME_LOAD_REPLAY_URI). Results go to
COGAME_RESULTS_URI, replay bytes to COGAME_SAVE_REPLAY_URI.

Bootstrap milestones are stamped from the first Python marker so the
`game_boot_s` phase can be decomposed as far as the container can see it.
"""

from __future__ import annotations

_IMPORTS_BEGIN = __import__("time").monotonic_ns()

import asyncio  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from contextlib import asynccontextmanager, suppress  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import uvicorn  # noqa: E402
import websockets  # noqa: E402
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from profiler import FIRST_MARK_MONO_NS, FIRST_MARK_WALL_NS, SCHEMA_VERSION  # noqa: E402
from profiler.clocks import take_anchor  # noqa: E402
from profiler.config import GameConfig  # noqa: E402
from profiler.game.episode import Episode  # noqa: E402
from profiler.game.results import REPLAY_RECORD_ORDER, build_results, replay_view  # noqa: E402
from profiler.io import artifact_method, read_data, write_data  # noqa: E402
from profiler.recorder import Recorder  # noqa: E402
from profiler.resources import GcRecorder, LoopLagSampler, process_birth, sample_resources  # noqa: E402

_IMPORTS_END = time.monotonic_ns()

CLIENT_DIR = Path(__file__).parent / "client"
GAME_HOST = os.environ.get("COGAME_HOST", "0.0.0.0")
GAME_PORT = int(os.environ.get("COGAME_PORT", "8080"))
REPLAY_MODE = "COGAME_LOAD_REPLAY_URI" in os.environ
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def log(event: str, **details: Any) -> None:
    record = {"schema_version": SCHEMA_VERSION, "role": "game", "event": event, "mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns(), **details}
    print(json.dumps(record, separators=(",", ":")), flush=True)


class GameRuntime:
    """Everything that exists for one game process: config, episode, recorders, bootstrap stamps."""

    def __init__(self) -> None:
        self.recorder = Recorder()
        self.bootstrap: dict[str, int | None] = {"imports_ns": _IMPORTS_END - _IMPORTS_BEGIN}
        self.loop_lag = LoopLagSampler()
        self.gc = GcRecorder()
        self.listening_mono_ns: int | None = None
        self.health_hits: list[dict[str, Any]] = []
        self.first_global_mono_ns: int | None = None
        self.replay_document: dict[str, Any] | None = None
        self.finalized = asyncio.Event()
        self.context: dict[str, Any] = {}
        if REPLAY_MODE:
            self.config: GameConfig | None = None
            self.episode: Episode | None = None
            return
        read_begin = time.monotonic_ns()
        raw, read_timing = read_data(os.environ["COGAME_CONFIG_URI"])
        self.bootstrap["config_read_ns"] = time.monotonic_ns() - read_begin
        decode_begin = time.monotonic_ns()
        self.config = GameConfig.model_validate_json(raw)
        self.bootstrap["config_decode_ns"] = time.monotonic_ns() - decode_begin
        self.bootstrap["config_bytes"] = read_timing.byte_count
        self.episode = Episode(self.config, run_id=str(uuid.uuid4()), recorder=self.recorder)
        self.episode.first_mark_wall_ns = FIRST_MARK_WALL_NS
        payload_begin = time.monotonic_ns()
        cell_timings = self.episode.prepare_payloads()
        self.bootstrap["payload_build_ns"] = time.monotonic_ns() - payload_begin
        self.bootstrap["birth_to_first_mark_ns"] = process_birth().birth_to_first_mark_ns
        self.recorder.add("lifecycle", {"event": "python.first_mark", "mono_ns": FIRST_MARK_MONO_NS, "wall_ns": FIRST_MARK_WALL_NS})
        self.recorder.add("lifecycle", {"event": "imports", "mono_ns": _IMPORTS_BEGIN, "wall_ns": 0, "duration_ns": _IMPORTS_END - _IMPORTS_BEGIN})
        self.recorder.add(
            "lifecycle",
            {
                "event": "config.read",
                "mono_ns": read_begin,
                "wall_ns": 0,
                "duration_ns": self.bootstrap["config_read_ns"],
                "detail": {"scheme": read_timing.scheme, "bytes": read_timing.byte_count},
            },
        )
        self.recorder.add(
            "lifecycle",
            {
                "event": "payload.build",
                "mono_ns": payload_begin,
                "wall_ns": 0,
                "duration_ns": self.bootstrap["payload_build_ns"],
                "detail": {k: v for k, v in cell_timings.items()},
            },
        )
        log(
            "config",
            slots=self.config.slot_count,
            mode=self.config.mode,
            step_seconds=self.config.step_seconds,
            ticks=self.config.total_ticks,
            imports_ns=self.bootstrap["imports_ns"],
            config_read_ns=self.bootstrap["config_read_ns"],
        )

    # ----- lifecycle ------------------------------------------------------

    def on_listening(self) -> None:
        self.listening_mono_ns = time.monotonic_ns()
        self.bootstrap["server_start_ns"] = (
            self.listening_mono_ns
            - _IMPORTS_END
            - (self.bootstrap.get("config_read_ns") or 0)
            - (self.bootstrap.get("config_decode_ns") or 0)
            - (self.bootstrap.get("payload_build_ns") or 0)
        )
        self.bootstrap["listening_mono_ns"] = self.listening_mono_ns
        self.recorder.add(
            "lifecycle",
            {
                "event": "server.listening",
                "mono_ns": self.listening_mono_ns,
                "wall_ns": time.time_ns(),
                "detail": {"since_first_mark_ns": self.listening_mono_ns - FIRST_MARK_MONO_NS},
            },
        )
        log("listening", since_first_mark_ns=self.listening_mono_ns - FIRST_MARK_MONO_NS)

    def on_health(self, request: Request) -> None:
        now = time.monotonic_ns()
        peer = request.client.host if request.client else None
        if not self.health_hits and self.listening_mono_ns is not None:
            self.bootstrap["first_health_ns"] = now - self.listening_mono_ns
        if len(self.health_hits) < 200:
            self.health_hits.append({"mono_ns": now, "peer": peer})
            self.recorder.add(
                "lifecycle",
                {
                    "event": "health.request",
                    "mono_ns": now,
                    "wall_ns": time.time_ns(),
                    "detail": {"peer": peer, "loopback": peer in ("127.0.0.1", "::1")},
                },
            )

    async def sample_periodically(self) -> None:
        assert self.config is not None
        while True:
            self.recorder.add("resource", sample_resources())
            self.recorder.add("clock_anchor", take_anchor().model_dump())
            await asyncio.sleep(self.config.resource_sample_seconds)

    # ----- finalize -------------------------------------------------------

    async def finalize(self) -> None:
        """Write replay then results, atomically each, and tell the players the game summary."""
        assert self.episode is not None and self.config is not None
        episode = self.episode
        await self.loop_lag.stop()
        for sample in self.loop_lag.samples:
            self.recorder.add("loop_lag", sample)
        for pause in self.gc.pauses:
            self.recorder.add("gc", pause)
        self.recorder.add("resource", sample_resources())
        episode.export_turns()
        # Provisional results (without replay timings) feed the game summary sent to players.
        provisional = build_results(episode, bootstrap=self.bootstrap, game_context={})
        summary = provisional.model_dump(mode="json")
        summary.pop("slots", None)
        await episode.send_final({"results": summary, "slots": [slot.model_dump(mode="json") for slot in provisional.slots]})
        await asyncio.sleep(0.5)  # let players re-publish their zips with the summary before sockets close

        replay_prepare_begin = time.monotonic_ns()
        self.recorder.add("summary", summary)
        replay_bytes = self.recorder.to_gzip_jsonl_stream(
            {
                "schema_version": SCHEMA_VERSION,
                "format": "coworld-profiler-replay",
                "run_id": episode.run_id,
                "config": self.config.model_dump(mode="json", exclude={"tokens"}),
                "versions": {"python": platform.python_version(), "websockets": websockets.__version__, "uvicorn": uvicorn.__version__},
            },
            REPLAY_RECORD_ORDER + ["connect_attempt", "clock_anchor", "ping"],
        )
        self.context["replay_prepare_ns"] = time.monotonic_ns() - replay_prepare_begin
        self.context["replay_size_bytes"] = len(replay_bytes)
        replay_timing = write_data(
            os.environ["COGAME_SAVE_REPLAY_URI"],
            replay_bytes,
            content_type="application/gzip",
            http_method=artifact_method("COGAME_SAVE_REPLAY_METHOD"),
        )
        self.context["replay_publish_ns"] = replay_timing.duration_ns
        results = build_results(episode, bootstrap=self.bootstrap, game_context=self.context)
        results_bytes = results.model_dump_json().encode()
        results_timing = write_data(
            os.environ["COGAME_RESULTS_URI"], results_bytes, content_type="application/json", http_method=artifact_method("COGAME_RESULTS_METHOD")
        )
        log(
            "finalized",
            replay_bytes=len(replay_bytes),
            replay_prepare_ns=self.context["replay_prepare_ns"],
            replay_publish_ns=replay_timing.duration_ns,
            results_bytes=len(results_bytes),
            results_publish_ns=results_timing.duration_ns,
            rtt_p50_s=results.websocket_application_rtt.p50_s,
            rtt_p99_s=results.websocket_application_rtt.p99_s,
            residual_p50_s=results.websocket_transport_residual.p50_s,
            late_fraction=results.action_late_fraction,
        )
        self.finalized.set()

    def load_replay(self) -> dict[str, Any]:
        if self.replay_document is None:
            raw, _timing = read_data(os.environ["COGAME_LOAD_REPLAY_URI"])
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            records = [json.loads(line) for line in raw.decode().splitlines() if line]
            self.replay_document = replay_view(records)
        return self.replay_document


runtime = GameRuntime()
server: uvicorn.Server


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.on_listening()
    tasks: list[asyncio.Task[None]] = []
    if runtime.episode is not None:
        runtime.gc.install()
        runtime.loop_lag.start()
        tasks.append(asyncio.create_task(runtime.sample_periodically()))
        if runtime.config is not None and runtime.config.player_connect_timeout_seconds > 0:
            tasks.append(asyncio.create_task(runtime.episode.start_after_timeout()))
        tasks.append(asyncio.create_task(_finalize_when_done()))
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _finalize_when_done() -> None:
    assert runtime.episode is not None
    done = asyncio.Event()
    runtime.episode.on_done.append(done)
    await done.wait()
    await runtime.finalize()
    await asyncio.sleep(0.5)
    server.should_exit = True


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz(request: Request) -> dict[str, bool]:
    runtime.on_health(request)
    return {"ok": True}


@app.get("/client/player")
def player_client() -> HTMLResponse:
    return HTMLResponse((CLIENT_DIR / "player.html").read_text())


@app.get("/client/global")
def global_client() -> HTMLResponse:
    return HTMLResponse((CLIENT_DIR / "global.html").read_text())


@app.get("/client/replay")
def replay_client() -> HTMLResponse:
    return HTMLResponse((CLIENT_DIR / "replay.html").read_text())


@app.websocket("/global")
async def global_viewer(websocket: WebSocket) -> None:
    await websocket.accept()
    now = time.monotonic_ns()
    if runtime.first_global_mono_ns is None and runtime.listening_mono_ns is not None:
        runtime.first_global_mono_ns = now
        runtime.bootstrap["first_global_ns"] = now - runtime.listening_mono_ns
        runtime.recorder.add("lifecycle", {"event": "global.first_connect", "mono_ns": now, "wall_ns": time.time_ns()})
    if runtime.episode is None:
        await websocket.send_json({"type": "state", "stage": "replay"})
        with suppress(WebSocketDisconnect):
            async for _ in websocket.iter_text():
                pass
        return
    sender = asyncio.create_task(_send_snapshots(websocket))
    try:
        async for _ in websocket.iter_text():
            pass
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with suppress(asyncio.CancelledError):
            await sender


async def _send_snapshots(websocket: WebSocket) -> None:
    assert runtime.episode is not None
    await websocket.send_json(runtime.episode.snapshot())
    while not runtime.finalized.is_set():
        await asyncio.sleep(1.0)
        await websocket.send_json(runtime.episode.snapshot())
    await websocket.send_json({**runtime.episode.snapshot(), "results": runtime.episode.game_summary.get("results")})


@app.websocket("/replay")
async def replay_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json(runtime.load_replay())
    with suppress(WebSocketDisconnect):
        async for _ in websocket.iter_text():
            pass


class _FastapiTransport:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        await self._websocket.send_text(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await self._websocket.close(code=code, reason=reason)


@app.websocket("/player")
async def player_socket(websocket: WebSocket) -> None:
    episode = runtime.episode
    if episode is None:
        await websocket.close(code=1008)
        return
    slot_text = websocket.query_params.get("slot", "")
    token = websocket.query_params.get("token", "")
    slot = int(slot_text) if slot_text.isdigit() else -1
    if not episode.valid_credentials(slot, token):
        episode.lifecycle("player.rejected", detail={"slot": slot_text})
        await websocket.close(code=1008)
        return
    if episode.slots[slot].transport is not None:
        episode.lifecycle("player.duplicate_rejected", slot=slot)
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await episode.attach(slot, _FastapiTransport(websocket))
    try:
        while True:
            raw = await websocket.receive_text()
            receive_mono = time.monotonic_ns()
            receive_wall = time.time_ns()
            await episode.handle_message(slot, raw, receive_mono_ns=receive_mono, receive_wall_ns=receive_wall)
            if episode.slots[slot].ready and not episode.started:
                episode.start_when_ready()
    except WebSocketDisconnect:
        pass
    finally:
        if episode.slots[slot].transport is not None:
            episode.detach(slot)


def main() -> None:
    global server
    server = uvicorn.Server(
        uvicorn.Config(
            app, host=GAME_HOST, port=GAME_PORT, ws="websockets", ws_max_size=MAX_MESSAGE_BYTES, ws_per_message_deflate=False, log_level="warning"
        )
    )
    server.run()


if __name__ == "__main__":
    main()
