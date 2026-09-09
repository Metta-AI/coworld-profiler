"""Process-level context samplers: cgroup v2 CPU/memory, /proc RSS, asyncio loop lag, GC pauses.

None of these are per-turn measurements. They are the context that explains
why a tail sample was slow: the container was throttled, the loop was busy,
the collector paused. Missing counters are reported as None, never zero.
"""

from __future__ import annotations

import asyncio
import gc
import os
import time
from pathlib import Path

from pydantic import BaseModel

from profiler import FIRST_MARK_MONO_NS

_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _own_cgroup_dir() -> Path | None:
    try:
        line = Path("/proc/self/cgroup").read_text().strip().splitlines()[-1]
    except (OSError, IndexError):
        return None
    relative = line.split(":", 2)[-1].lstrip("/")
    candidate = _CGROUP_ROOT / relative
    return candidate if candidate.is_dir() else (_CGROUP_ROOT if _CGROUP_ROOT.is_dir() else None)


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


class ResourceSample(BaseModel):
    mono_ns: int
    cpu_usage_usec: int | None = None
    cpu_user_usec: int | None = None
    cpu_system_usec: int | None = None
    cpu_nr_periods: int | None = None
    cpu_nr_throttled: int | None = None
    cpu_throttled_usec: int | None = None
    cpu_quota_usec: int | None = None
    cpu_period_usec: int | None = None
    memory_current_bytes: int | None = None
    memory_peak_bytes: int | None = None
    process_rss_bytes: int | None = None
    process_cpu_ns: int | None = None


def sample_resources() -> ResourceSample:
    sample = ResourceSample(mono_ns=time.monotonic_ns(), process_cpu_ns=time.process_time_ns())
    cgroup = _own_cgroup_dir()
    if cgroup is not None:
        try:
            stat = dict(line.split() for line in (cgroup / "cpu.stat").read_text().splitlines() if " " in line)
        except OSError:
            stat = {}
        for key in ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"):
            value = stat.get(key)
            setattr(sample, f"cpu_{key}", int(value) if value is not None and value.isdigit() else None)
        try:
            quota, period = (cgroup / "cpu.max").read_text().split()
            sample.cpu_quota_usec = int(quota) if quota.isdigit() else None
            sample.cpu_period_usec = int(period)
        except (OSError, ValueError):
            pass
        sample.memory_current_bytes = _read_int(cgroup / "memory.current")
        sample.memory_peak_bytes = _read_int(cgroup / "memory.peak")
    try:
        statm = Path("/proc/self/statm").read_text().split()
        sample.process_rss_bytes = int(statm[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        pass
    return sample


class ProcessBirth(BaseModel):
    """Approximate process start relative to our first Python marker, from /proc/self/stat.

    `starttime` is in clock ticks since boot, so the resolution is one tick
    (usually 10 ms). This includes exec and interpreter startup, which is why
    it is reported as a bound rather than a measurement.
    """

    birth_to_first_mark_ns: int | None
    tick_resolution_ns: int | None


def process_birth() -> ProcessBirth:
    try:
        stat = Path("/proc/self/stat").read_text()
        uptime_s = float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        return ProcessBirth(birth_to_first_mark_ns=None, tick_resolution_ns=None)
    fields = stat.rsplit(")", 1)[1].split()
    ticks_per_s = os.sysconf("SC_CLK_TCK")
    start_ticks = int(fields[19])
    now_mono = time.monotonic_ns()
    # /proc/uptime and CLOCK_MONOTONIC share an origin on Linux, so the birth
    # instant on the monotonic timeline is uptime_at_birth expressed in ns.
    uptime_now_ns = int(uptime_s * 1e9)
    birth_mono_ns = now_mono - uptime_now_ns + int(start_ticks * 1e9 / ticks_per_s)
    return ProcessBirth(
        birth_to_first_mark_ns=FIRST_MARK_MONO_NS - birth_mono_ns,
        tick_resolution_ns=int(1e9 / ticks_per_s),
    )


class LoopLagSample(BaseModel):
    mono_ns: int
    lag_ns: int


class LoopLagSampler:
    """Measures asyncio scheduling delay: how late a periodic sleep wakes up."""

    def __init__(self, *, period_s: float = 0.01) -> None:
        self.period_ns = int(period_s * 1e9)
        self.samples: list[LoopLagSample] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        target = time.monotonic_ns() + self.period_ns
        while True:
            await asyncio.sleep(max(0, target - time.monotonic_ns()) / 1e9)
            now = time.monotonic_ns()
            self.samples.append(LoopLagSample(mono_ns=now, lag_ns=now - target))
            target = now + self.period_ns


class GcPause(BaseModel):
    generation: int
    start_mono_ns: int
    duration_ns: int


class GcRecorder:
    def __init__(self) -> None:
        self.pauses: list[GcPause] = []
        self._start_ns = 0

    def install(self) -> None:
        gc.callbacks.append(self._callback)

    def _callback(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._start_ns = time.monotonic_ns()
        else:
            now = time.monotonic_ns()
            self.pauses.append(GcPause(generation=info["generation"], start_mono_ns=self._start_ns, duration_ns=now - self._start_ns))
