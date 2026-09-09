"""Instrumented websocket connect: DNS, TCP, and the HTTP upgrade timed separately.

`websockets.connect` normally resolves and connects internally. To split
those stages we resolve with `getaddrinfo`, connect a plain socket, and hand
it to the library through `sock=`, which asyncio's `create_connection`
accepts. The URI is still passed so the library builds the correct Host
header and request target.
"""

from __future__ import annotations

import asyncio
import socket
import time
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import ClientConnection

from profiler.protocol import ConnectAttempt

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class ConnectFailure(Exception):
    def __init__(self, attempt: ConnectAttempt) -> None:
        super().__init__(attempt.error)
        self.attempt = attempt


async def connect_instrumented(url: str, *, attempt_number: int) -> tuple[ClientConnection, ConnectAttempt]:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    if host is None:
        raise ValueError("player websocket URL has no host")
    loop = asyncio.get_running_loop()
    stamps = {"dns_begin": time.monotonic_ns(), "dns_end": 0, "tcp_begin": 0, "tcp_end": 0, "upgrade_begin": 0, "upgrade_end": 0}
    family_name: str | None = None
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        stamps["dns_end"] = time.monotonic_ns()
        if not infos:
            raise OSError(f"no addresses for {host}")
        family, socktype, proto, _canon, sockaddr = infos[0]
        family_name = family.name
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)
        stamps["tcp_begin"] = time.monotonic_ns()
        await loop.sock_connect(sock, sockaddr)
        stamps["tcp_end"] = time.monotonic_ns()
        stamps["upgrade_begin"] = time.monotonic_ns()
        connection = await websockets.connect(
            url,
            sock=sock,
            ping_interval=20,
            ping_timeout=None,  # required by the Coworld player contract; see metta test_coworld_player_keepalive.py
            max_size=MAX_MESSAGE_BYTES,
            compression=None,
            open_timeout=30,
            proxy=None,
        )
        stamps["upgrade_end"] = time.monotonic_ns()
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as error:
        now = time.monotonic_ns()
        attempt = ConnectAttempt(
            attempt=attempt_number,
            dns_begin_mono_ns=stamps["dns_begin"],
            dns_end_mono_ns=stamps["dns_end"] or now,
            tcp_begin_mono_ns=stamps["tcp_begin"] or now,
            tcp_end_mono_ns=stamps["tcp_end"] or now,
            upgrade_begin_mono_ns=stamps["upgrade_begin"] or now,
            upgrade_end_mono_ns=now,
            success=False,
            error=f"{type(error).__name__}: {error}",
            address_family=family_name,
        )
        raise ConnectFailure(attempt) from error
    attempt = ConnectAttempt(
        attempt=attempt_number,
        dns_begin_mono_ns=stamps["dns_begin"],
        dns_end_mono_ns=stamps["dns_end"],
        tcp_begin_mono_ns=stamps["tcp_begin"],
        tcp_end_mono_ns=stamps["tcp_end"],
        upgrade_begin_mono_ns=stamps["upgrade_begin"],
        upgrade_end_mono_ns=stamps["upgrade_end"],
        success=True,
        address_family=family_name,
    )
    return connection, attempt


async def connect_with_retries(url: str, *, max_attempts: int = 30, retry_seconds: float = 1.0) -> tuple[ClientConnection, list[ConnectAttempt]]:
    attempts: list[ConnectAttempt] = []
    for number in range(1, max_attempts + 1):
        try:
            connection, attempt = await connect_instrumented(url, attempt_number=number)
        except ConnectFailure as failure:
            attempts.append(failure.attempt)
            if number == max_attempts:
                raise
            await asyncio.sleep(retry_seconds)
            continue
        attempts.append(attempt)
        return connection, attempts
    raise AssertionError("unreachable")
