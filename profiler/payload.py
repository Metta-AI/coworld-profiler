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

    A row of eight ints in 0..999 encodes to about 33 bytes including
    delimiters, so the row count is derived from that and then adjusted by
    measuring the actual encoding.
    """
    rng = random.Random(seed)
    estimated_rows = max(1, target_bytes // 33)
    rows = [[rng.randrange(0, 1000) for _ in range(ROW_WIDTH)] for _ in range(estimated_rows)]
    encoded = len(json.dumps(rows, separators=(",", ":")))
    while encoded < target_bytes:
        rows.append([rng.randrange(0, 1000) for _ in range(ROW_WIDTH)])
        encoded = len(json.dumps(rows, separators=(",", ":")))
    while encoded > target_bytes and len(rows) > 1:
        rows.pop()
        encoded = len(json.dumps(rows, separators=(",", ":")))
    return rows


def encoded_size(rows: list[list[int]]) -> int:
    return len(json.dumps(rows, separators=(",", ":")).encode())
