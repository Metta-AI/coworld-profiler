"""In-memory record store with JSONL export and percentile summaries.

Both the game and the players buffer compact records during the episode and
serialize only at the end, so the recorder itself adds as little to the
measured intervals as possible. Records are plain dicts validated at the
edges (pydantic models produce them); the recorder does not interpret them.
"""

from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from typing import Any

from pydantic import BaseModel


class Distribution(BaseModel):
    """Nearest-rank percentiles in seconds over a list of nanosecond samples."""

    count: int
    sum_s: float | None
    p50_s: float | None
    p90_s: float | None
    p99_s: float | None
    max_s: float | None


def summarize_ns(samples: list[int]) -> Distribution:
    if not samples:
        return Distribution(count=0, sum_s=None, p50_s=None, p90_s=None, p99_s=None, max_s=None)
    ordered = sorted(samples)

    def nearest_rank(percentile: float) -> float:
        rank = max(1, math.ceil(percentile / 100 * len(ordered)))
        return ordered[rank - 1] / 1e9

    return Distribution(
        count=len(ordered),
        sum_s=sum(ordered) / 1e9,
        p50_s=nearest_rank(50),
        p90_s=nearest_rank(90),
        p99_s=nearest_rank(99),
        max_s=ordered[-1] / 1e9,
    )


class Recorder:
    """Append-only record buffers keyed by record type, with a byte budget.

    `max_records` caps the total; past it, records are counted as dropped
    instead of stored, so an unexpectedly long episode degrades to "incomplete
    trace" rather than to an OOM.
    """

    def __init__(self, *, max_records: int = 2_000_000) -> None:
        self._records: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._max_records = max_records
        self._count = 0
        self.dropped = 0

    def add(self, record_type: str, record: BaseModel | dict[str, Any]) -> None:
        payload = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        if self._count >= self._max_records:
            self.dropped += 1
            return
        self._records[record_type].append(payload)
        self._count += 1

    def records(self, record_type: str) -> list[dict[str, Any]]:
        return self._records[record_type]

    def counts(self) -> dict[str, int]:
        return {record_type: len(records) for record_type, records in self._records.items()}

    def to_jsonl(self, record_type: str) -> bytes:
        lines = [json.dumps(record, separators=(",", ":")) for record in self._records[record_type]]
        return ("\n".join(lines) + "\n").encode() if lines else b""

    def to_gzip_jsonl_stream(self, header: dict[str, Any], ordered_types: list[str]) -> bytes:
        """One gzip JSONL document: header line, then records grouped by type in `ordered_types`."""
        lines = [json.dumps({"record_type": "header", **header}, separators=(",", ":"))]
        for record_type in ordered_types:
            for record in self._records[record_type]:
                lines.append(json.dumps({"record_type": record_type, **record}, separators=(",", ":")))
        return gzip.compress(("\n".join(lines) + "\n").encode())
