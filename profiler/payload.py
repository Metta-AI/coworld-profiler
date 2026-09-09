"""Deterministic observation payloads of a target encoded size.

Real games ship structured JSON (grids, entity lists). The profiler generates a
list of numeric rows from a seed and pads or trims it so the UTF-8 encoding of
the whole observation lands within a few percent of the target. Content is
generated once per payload cell before the measured phase; only encoding
happens on the hot path.
"""

from __future__ import annotations

import json
import random

ROW_WIDTH = 8


def build_payload(seed: int, target_bytes: int) -> list[list[int]]:
    """Rows of small ints whose JSON encoding is close to `target_bytes`.

    Each row of eight ints in 0..999 encodes to at most 33 bytes plus a comma,
    so the row count is estimated from that, the real encoded size is measured
    once, and the count is corrected proportionally before a final trim. This
    stays linear in the payload size; re-encoding per row made a 2 MiB payload
    take 20 s to build.
    """
    rng = random.Random(seed)

    def rows_of(count: int) -> list[list[int]]:
        local = random.Random(seed)
        return [[local.randrange(0, 1000) for _ in range(ROW_WIDTH)] for _ in range(count)]

    estimate = max(1, target_bytes // 30)
    rows = rows_of(estimate)
    encoded = encoded_size(rows)
    corrected = max(1, int(estimate * target_bytes / encoded))
    rows = rows_of(corrected)
    encoded = encoded_size(rows)
    while encoded < target_bytes:
        rows.append([rng.randrange(0, 1000) for _ in range(ROW_WIDTH)])
        encoded += len(json.dumps(rows[-1], separators=(",", ":"))) + 1
    while encoded > target_bytes and len(rows) > 1:
        encoded -= len(json.dumps(rows[-1], separators=(",", ":"))) + 1
        rows.pop()
    return rows


def encoded_size(rows: list[list[int]]) -> int:
    return len(json.dumps(rows, separators=(",", ":")).encode())
