from __future__ import annotations

import gzip
import json

from profiler.clocks import ClockProbeSample, estimate_offset, take_anchor
from profiler.config import GameConfig
from profiler.payload import build_payload, encoded_size
from profiler.recorder import Recorder, summarize_ns


def test_anchor_bracket_is_ordered() -> None:
    anchor = take_anchor()
    assert anchor.mono_after_ns >= anchor.mono_before_ns
    assert anchor.bracket_ns >= 0


def test_ntp_offset_recovers_known_skew() -> None:
    # Player clock runs 5 ms ahead; one-way delay 1 ms each direction.
    skew, one_way = 5_000_000, 1_000_000
    t1 = 1_000_000_000
    t2 = t1 + one_way + skew
    t3 = t2 + 200_000
    t4 = t3 - skew + one_way
    sample = ClockProbeSample(probe_id=1, t1_ns=t1, t2_ns=t2, t3_ns=t3, t4_ns=t4)
    assert sample.offset_ns == skew
    assert sample.delay_ns == 2 * one_way
    assert sample.valid
    estimate = estimate_offset([sample])
    assert estimate.offset_ns == skew
    assert estimate.offset_lower_ns <= skew <= estimate.offset_upper_ns


def test_offset_estimate_prefers_minimum_delay_probe() -> None:
    fast = ClockProbeSample(probe_id=1, t1_ns=0, t2_ns=1_000, t3_ns=1_100, t4_ns=2_100)
    slow = ClockProbeSample(probe_id=2, t1_ns=0, t2_ns=9_000, t3_ns=9_100, t4_ns=10_100)
    estimate = estimate_offset([slow, fast])
    assert estimate.delay_ns == fast.delay_ns
    assert estimate.valid_count == 2


def test_invalid_probe_is_excluded() -> None:
    broken = ClockProbeSample(probe_id=1, t1_ns=0, t2_ns=1_000, t3_ns=900, t4_ns=2_000)
    assert not broken.valid
    assert estimate_offset([broken]).offset_ns is None


def test_payload_hits_target_size() -> None:
    for target in (1024, 16 * 1024, 256 * 1024):
        rows = build_payload(7, target)
        size = encoded_size(rows)
        assert abs(size - target) <= 40, (target, size)
    assert build_payload(7, 1024) == build_payload(7, 1024)
    assert build_payload(7, 1024) != build_payload(8, 1024)


def test_percentiles_nearest_rank() -> None:
    samples = list(range(1, 101))
    dist = summarize_ns(samples)
    assert dist.count == 100
    assert dist.p50_s == 50 / 1e9
    assert dist.p90_s == 90 / 1e9
    assert dist.p99_s == 99 / 1e9
    assert dist.max_s == 100 / 1e9
    assert summarize_ns([]).p50_s is None


def test_recorder_caps_and_streams() -> None:
    recorder = Recorder(max_records=3)
    for index in range(5):
        recorder.add("turn", {"i": index})
    assert recorder.counts() == {"turn": 3}
    assert recorder.dropped == 2
    stream = gzip.decompress(recorder.to_gzip_jsonl_stream({"run_id": "x"}, ["turn"]))
    lines = [json.loads(line) for line in stream.decode().splitlines()]
    assert lines[0]["record_type"] == "header"
    assert [line["i"] for line in lines[1:]] == [0, 1, 2]


def test_game_config_requires_matching_slots() -> None:
    base = {
        "tokens": ["a", "b"],
        "players": [{"name": "x"}, {"name": "y"}],
        "seed": 1,
        "cells": [{"cell_id": "c", "target_bytes": 1024, "warmup_ticks": 1, "measure_ticks": 2}],
    }
    config = GameConfig.model_validate(base)
    assert config.slot_count == 2
    assert config.total_ticks == 3
    bad = {**base, "players": [{"name": "x"}]}
    try:
        GameConfig.model_validate(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("expected slot mismatch to fail")


def test_manifest_template_schemas_are_in_sync() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, str(root / "tools" / "render_manifest.py"), "--check"], cwd=root, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
