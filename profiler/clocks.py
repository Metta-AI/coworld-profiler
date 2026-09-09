"""Clock helpers: bracketed wall/monotonic anchors and NTP-style offset math.

Rules used throughout the profiler:

- `time.monotonic_ns()` for every duration and ordering decision.
- `time.time_ns()` only for aligning two processes' timelines.
- Never subtract a monotonic timestamp taken in one process from one taken in
  another. Cross-process comparisons go through `OffsetEstimate`.
"""

from __future__ import annotations

import time

from pydantic import BaseModel


class ClockAnchor(BaseModel):
    """One bracketed reading: monotonic before, wall clock, monotonic after.

    The bracket width bounds how precisely the wall reading can be placed on
    the monotonic timeline. `wall_minus_mono_ns` is the quantity whose drift
    over an episode reveals wall-clock adjustments.
    """

    mono_before_ns: int
    wall_ns: int
    mono_after_ns: int

    @property
    def bracket_ns(self) -> int:
        return self.mono_after_ns - self.mono_before_ns

    @property
    def wall_minus_mono_ns(self) -> int:
        midpoint = (self.mono_before_ns + self.mono_after_ns) // 2
        return self.wall_ns - midpoint


def take_anchor() -> ClockAnchor:
    before = time.monotonic_ns()
    wall = time.time_ns()
    after = time.monotonic_ns()
    return ClockAnchor(mono_before_ns=before, wall_ns=wall, mono_after_ns=after)


class ClockProbeSample(BaseModel):
    """One four-timestamp exchange, all wall-clock nanoseconds.

    T1: sender stamps just before sending the probe.
    T2: receiver stamps just after receiving it.
    T3: receiver stamps just before sending the reply.
    T4: sender stamps just after receiving the reply.
    """

    probe_id: int
    t1_ns: int
    t2_ns: int
    t3_ns: int
    t4_ns: int

    @property
    def offset_ns(self) -> int:
        """Receiver clock minus sender clock, assuming symmetric delay (RFC 5905 §8)."""
        return ((self.t2_ns - self.t1_ns) + (self.t3_ns - self.t4_ns)) // 2

    @property
    def delay_ns(self) -> int:
        """Round trip excluding the receiver's processing time."""
        return (self.t4_ns - self.t1_ns) - (self.t3_ns - self.t2_ns)

    @property
    def offset_lower_ns(self) -> int:
        return self.t3_ns - self.t4_ns

    @property
    def offset_upper_ns(self) -> int:
        return self.t2_ns - self.t1_ns

    @property
    def valid(self) -> bool:
        return self.delay_ns >= 0 and self.t3_ns >= self.t2_ns


class OffsetEstimate(BaseModel):
    """Summary of a window of probes for one peer.

    The representative offset is the minimum-delay valid probe; the bounds are
    the tightest lower/upper bounds across valid probes. `symmetric_delay_assumed`
    is always true and is carried so consumers cannot forget it.
    """

    sample_count: int
    valid_count: int
    offset_ns: int | None
    delay_ns: int | None
    offset_lower_ns: int | None
    offset_upper_ns: int | None
    symmetric_delay_assumed: bool = True


def estimate_offset(samples: list[ClockProbeSample]) -> OffsetEstimate:
    valid = [sample for sample in samples if sample.valid]
    if not valid:
        return OffsetEstimate(
            sample_count=len(samples),
            valid_count=0,
            offset_ns=None,
            delay_ns=None,
            offset_lower_ns=None,
            offset_upper_ns=None,
        )
    best = min(valid, key=lambda sample: sample.delay_ns)
    return OffsetEstimate(
        sample_count=len(samples),
        valid_count=len(valid),
        offset_ns=best.offset_ns,
        delay_ns=best.delay_ns,
        offset_lower_ns=max(sample.offset_lower_ns for sample in valid),
        offset_upper_ns=min(sample.offset_upper_ns for sample in valid),
    )
