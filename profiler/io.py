"""URI read/write helpers shared by the game and players.

The Coworld contract hands the game `file://` or `http(s)://` URIs for config,
results, and replay, and hands each player a `file://` or presigned `https://`
URL for its artifact. Every call here is timed and the timing returned, because
artifact I/O is one of the intervals the profiler exists to measure.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel

# urllib's default User-Agent is blocked by some CDN WAFs; any non-default UA works.
HTTP_USER_AGENT = "coworld-profiler/0.1"


class IoTiming(BaseModel):
    scheme: str
    method: str
    byte_count: int
    duration_ns: int
    http_status: int | None = None


def _local_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    if parsed.scheme == "":
        return Path(uri)
    return None


def read_data(uri: str) -> tuple[bytes, IoTiming]:
    started = time.monotonic_ns()
    path = _local_path(uri)
    if path is not None:
        data = path.read_bytes()
        return data, IoTiming(scheme="file", method="read", byte_count=len(data), duration_ns=time.monotonic_ns() - started)
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URI scheme for read_data: {parsed.scheme!r}")
    request = Request(uri, headers={"User-Agent": HTTP_USER_AGENT})
    with urlopen(request, timeout=30) as response:
        data = response.read()
        status = response.status
    return data, IoTiming(scheme=parsed.scheme, method="GET", byte_count=len(data), duration_ns=time.monotonic_ns() - started, http_status=status)


def write_data(
    uri: str,
    data: bytes,
    *,
    content_type: str,
    http_method: Literal["PUT", "POST"] = "PUT",
) -> IoTiming:
    """Write bytes to a URI. Local writes are atomic: temp sibling then rename.

    Atomicity matters because the hosted worker polls for the results file's
    existence and would otherwise read a partial file.
    """
    started = time.monotonic_ns()
    path = _local_path(uri)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
        return IoTiming(scheme="file", method="write", byte_count=len(data), duration_ns=time.monotonic_ns() - started)
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URI scheme for write_data: {parsed.scheme!r}")
    request = Request(uri, data=data, method=http_method)
    request.add_header("Content-Type", content_type)
    request.add_header("User-Agent", HTTP_USER_AGENT)
    with urlopen(request, timeout=120) as response:
        status = response.status
    return IoTiming(
        scheme=parsed.scheme,
        method=http_method,
        byte_count=len(data),
        duration_ns=time.monotonic_ns() - started,
        http_status=status,
    )


def artifact_method(env_var: str) -> Literal["PUT", "POST"]:
    method = os.environ.get(env_var, "PUT").upper()
    if method not in ("PUT", "POST"):
        raise ValueError(f"{env_var} must be PUT or POST")
    return "PUT" if method == "PUT" else "POST"
