"""Build and publish the per-slot player artifact zip.

The platform hands each player `COWORLD_PLAYER_ARTIFACT_UPLOAD_URL` (a
presigned HTTP PUT hosted, a `file://` path locally, absent when disabled).
One object per slot; each upload replaces the previous one. Max 200 MB; we
cap our own zip far below that.
"""

from __future__ import annotations

import io
import os
import time
import zipfile

from pydantic import BaseModel

from profiler.io import write_data

MAX_ZIP_BYTES = 32 * 1024 * 1024


class ArtifactOutcome(BaseModel):
    attempted: bool
    byte_count: int | None = None
    build_ns: int | None = None
    publish_ns: int | None = None
    error: str | None = None


def build_zip(files: dict[str, bytes]) -> tuple[bytes, int]:
    started = time.monotonic_ns()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    payload = buffer.getvalue()
    if len(payload) > MAX_ZIP_BYTES:
        raise ValueError(f"player artifact would be {len(payload)} bytes, over the {MAX_ZIP_BYTES} cap")
    return payload, time.monotonic_ns() - started


def publish_zip(files: dict[str, bytes]) -> ArtifactOutcome:
    """Zip and upload. Never raises: a missing artifact must not fail the episode."""
    url = os.environ.get("COWORLD_PLAYER_ARTIFACT_UPLOAD_URL")
    if not url:
        return ArtifactOutcome(attempted=False)
    try:
        payload, build_ns = build_zip(files)
        timing = write_data(url, payload, content_type="application/zip", http_method="PUT")
    except Exception as error:  # noqa: BLE001 - reported, not raised, by contract
        return ArtifactOutcome(attempted=True, error=f"{type(error).__name__}: {error}")
    return ArtifactOutcome(attempted=True, byte_count=len(payload), build_ns=build_ns, publish_ns=timing.duration_ns)
