import copy
import json
from datetime import datetime, timedelta

import pytest

from tools import fetch_spans, reconcile_spans


def _api_span(span_id):
    return {
        "attributes": {
            "span_id": span_id,
            "trace_id": "trace",
            "operation_name": "job.lifecycle",
            "resource_name": "job",
            "start_timestamp": "2026-09-09T00:00:00Z",
            "end_timestamp": "2026-09-09T00:00:01Z",
            "parent_id": "0",
            "custom": {"duration": 1_000_000_000},
        }
    }


def test_search_reads_all_pages_and_deduplicates_span_ids(monkeypatch):
    requests = []
    pages = iter(
        [
            {"data": [_api_span(str(i)) for i in range(200)], "meta": {"status": "done", "page": {"after": "next"}}},
            {"data": [_api_span("199"), _api_span("200")], "meta": {"status": "done"}},
        ]
    )

    def page(body):
        requests.append(copy.deepcopy(body))
        return next(pages)

    monkeypatch.setattr(fetch_spans, "_search_page", page)
    spans = fetch_spans.search("trace_id:trace", limit=200)
    assert len(spans) == 201
    first, second = [request["data"]["attributes"] for request in requests]
    assert first["filter"] == second["filter"]
    assert datetime.fromisoformat(first["filter"]["to"]) - datetime.fromisoformat(first["filter"]["from"]) == timedelta(hours=48)
    assert second["page"] == {"limit": 200, "cursor": "next"}


@pytest.mark.parametrize("metadata", [{"status": "timeout"}, {"warnings": [{"title": "partial"}]}])
def test_search_rejects_partial_results(monkeypatch, metadata):
    monkeypatch.setattr(fetch_spans, "_search_page", lambda _: {"data": [], "meta": metadata})
    with pytest.raises(RuntimeError, match="partial"):
        fetch_spans.search("trace_id:trace", limit=200)


def test_search_rejects_repeating_cursor(monkeypatch):
    monkeypatch.setattr(fetch_spans, "_search_page", lambda _: {"data": [_api_span("same")], "meta": {"page": {"after": "again"}}})
    with pytest.raises(RuntimeError, match="repeated"):
        fetch_spans.search("trace_id:trace", limit=200)


def _span(span_id, name="image.pull", role="game", uid="game-uid", resource="worker", seconds=2):
    return {
        "span_id": span_id,
        "operation_name": name,
        "resource_name": resource,
        "duration_s": seconds,
        "start": "2026-09-09T00:00:00Z",
        "end": "2026-09-09T00:00:02Z",
        "custom": {"pod": {"role": role, "uid": uid}},
    }


def test_reconciliation_retains_container_and_player_groups():
    spans = [
        _span("a"),
        _span("b", resource="game", seconds=3),
        _span("c", role="player", uid="player-1", resource="player", seconds=10),
        _span("d", role="player", uid="player-2", resource="player", seconds=20),
    ]
    view = reconcile_spans.worker_view({"spans": spans + [spans[0]]})
    assert len(view["span_groups"]) == 4
    assert "container.run" not in view["ambiguous_names"]
    assert view["image.pull"] == 5
    assert view["image_pull_count"] == 2
    assert "image.pull" not in view["ambiguous_names"]


def test_reconciliation_omits_ambiguous_single_span_and_uses_explicit_viewer():
    view = reconcile_spans.worker_view(
        {
            "spans": [
                _span("a", name="episode.loop"),
                _span("b", name="episode.loop"),
                _span("c", name="worker.viewer_wait", seconds=7),
            ]
        }
    )
    assert view["episode.loop"] is None
    assert view["first_step_s"] is None
    assert view["worker.viewer_wait"] == 7


def test_empty_reconciliation_reports_missing_data(tmp_path, capsys):
    reconcile_spans.main(str(tmp_path))
    assert "joined episodes: 0" in capsys.readouterr().out


def test_refresh_refetches_existing_trace_but_default_reuses_it(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    out = tmp_path / "spans"
    out.mkdir()
    target = out / "job.json"
    target.write_text(json.dumps({"trace_id": "trace", "spans": []}))
    queries = []

    def search(query, **kwargs):
        queries.append(query)
        if query.startswith("@job.id"):
            return [{"attributes": {"trace_id": "trace"}}]
        return []

    monkeypatch.setattr(fetch_spans, "search", search)
    monkeypatch.setattr(fetch_spans.time, "sleep", lambda _: None)
    fetch_spans.main(str(jobs), str(out))
    assert queries == []
    assert json.loads(target.read_text()) == {"trace_id": "trace", "spans": []}
    fetch_spans.main(str(jobs), str(out), refresh=True)
    assert len(queries) == 2
    assert json.loads(target.read_text())["trace_id"] == "trace"


def test_grouping_distinguishes_historical_pod_names_and_slots():
    a, b = _span("a", role="player"), _span("b", role="player")
    for slot, span in enumerate((a, b)):
        span["custom"]["pod"] = {"role": "player", "name": f"pod-{slot}"}
        span["custom"]["player"] = {"slot": slot}
    view = reconcile_spans.worker_view({"spans": [a, b]})
    assert len(view["span_groups"]) == 2
    assert {key[3] for key in view["span_groups"]} == {0, 1}


def test_historical_gap_and_untagged_collisions():
    launch, loop = _span("a", name="player.launch"), _span("b", name="episode.loop")
    loop["start"] = "2026-09-09T00:00:05Z"
    assert reconcile_spans.worker_view({"spans": [launch, loop]})["first_step_s"] == 3
    for span in (launch, loop):
        span["operation_name"] = "pod.create"
        span["custom"] = {}
    view = reconcile_spans.worker_view({"spans": [launch, loop]})
    assert view["pod.create"] is None
    assert view["ambiguous_names"] == ["pod.create"]


def test_refresh_without_root_retains_file_and_index(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    target = tmp_path / "job.json"
    target.write_text(json.dumps({"trace_id": "trace", "spans": []}))
    monkeypatch.setattr(fetch_spans, "search", lambda *args, **kwargs: [])
    fetch_spans.main(str(jobs), str(tmp_path), refresh=True)
    assert json.loads((tmp_path / "index.json").read_text()) == {"job": "job.json"}
    assert target.exists()


def test_refresh_preserves_older_spans_outside_window(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    target = tmp_path / "job.json"
    target.write_text(json.dumps({"trace_id": "trace", "spans": [_span("older")]}))
    monkeypatch.setattr(
        fetch_spans,
        "search",
        lambda query, **kwargs: [{"attributes": {"trace_id": "trace"}}] if query.startswith("@job.id") else [_api_span("new")],
    )
    monkeypatch.setattr(fetch_spans.time, "sleep", lambda _: None)
    fetch_spans.main(str(jobs), str(tmp_path), refresh=True)
    assert [span["span_id"] for span in json.loads(target.read_text())["spans"]] == ["older", "new"]
    assert not target.with_suffix(".json.tmp").exists()


def test_failed_job_preserves_index_of_completed_jobs(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": job, "ereq": "episode", "round": "round2"} for job in ("first", "second")]))

    def search(query, **kwargs):
        if "second" in query:
            raise RuntimeError("partial results")
        return [{"attributes": {"trace_id": "trace"}}] if query.startswith("@job.id") else [_api_span("root")]

    monkeypatch.setattr(fetch_spans, "search", search)
    monkeypatch.setattr(fetch_spans.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="partial"):
        fetch_spans.main(str(jobs), str(tmp_path))
    assert json.loads((tmp_path / "index.json").read_text()) == {"first": "first.json"}
    assert (tmp_path / "first.json").exists()
    assert not (tmp_path / "second.json").exists()


def test_multiple_lifecycle_traces_fail_without_replacing_saved_trace(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    target = tmp_path / "job.json"
    target.write_text(json.dumps({"trace_id": "trace", "spans": []}))
    original = target.read_bytes()
    monkeypatch.setattr(
        fetch_spans,
        "search",
        lambda *args, **kwargs: [{"attributes": {"trace_id": trace}} for trace in ("trace", "another")],
    )
    with pytest.raises(ValueError, match="multiple"):
        fetch_spans.main(str(jobs), str(tmp_path), refresh=True)
    assert target.read_bytes() == original


def test_empty_page_with_cursor_continues_until_cursor_ends(monkeypatch):
    pages = iter(
        [
            {"data": [_api_span("first")], "meta": {"page": {"after": "next"}}},
            {"data": [], "meta": {"page": {"after": "empty-page-cursor"}}},
            {"data": [_api_span("last")], "meta": {}},
        ]
    )
    monkeypatch.setattr(fetch_spans, "_search_page", lambda _: next(pages))
    assert [span["attributes"]["span_id"] for span in fetch_spans.search("trace_id:trace", limit=1)] == ["first", "last"]


def test_refresh_preserves_unlisted_index_entries(tmp_path):
    jobs = tmp_path / "jobs.json"
    jobs.write_text("[]")
    (tmp_path / "prior-job.json").write_text("{}")
    (tmp_path / "index.json").write_text(json.dumps({"prior-job": "prior-job.json", "missing": "missing.json"}))
    fetch_spans.main(str(jobs), str(tmp_path), refresh=True)
    assert json.loads((tmp_path / "index.json").read_text()) == {"prior-job": "prior-job.json"}


def test_first_fetch_with_empty_trace_does_not_save_snapshot(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    monkeypatch.setattr(fetch_spans, "search", lambda query, **_: [{"attributes": {"trace_id": "trace"}}] if query.startswith("@job.id") else [])
    monkeypatch.setattr(fetch_spans.time, "sleep", lambda _: None)
    fetch_spans.main(str(jobs), str(tmp_path / "spans"))
    assert not (tmp_path / "spans/job.json").exists()
    assert json.loads((tmp_path / "spans/index.json").read_text()) == {}


def test_naive_window_is_rejected_with_timezone_message(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        fetch_spans.main(str(tmp_path / "jobs.json"), str(tmp_path), window="2026-09-08T00:00:00")


def test_multiple_viewer_observations_do_not_reenable_historical_gap():
    spans = [
        _span("launch", name="player.launch"),
        _span("loop", name="episode.loop"),
        _span("viewer-1", name="worker.viewer_wait"),
        _span("viewer-2", name="worker.viewer_wait"),
    ]
    view = reconcile_spans.worker_view({"spans": spans})
    assert view["worker.viewer_wait"] is None
    assert view["first_step_s"] is None
    assert "worker.viewer_wait" in view["ambiguous_names"]


def test_refresh_rejects_changed_trace_identity_without_replacing_file(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    target = tmp_path / "job.json"
    target.write_text(json.dumps({"trace_id": "old-trace", "spans": [_span("older")]}))
    before = target.read_bytes()
    monkeypatch.setattr(fetch_spans, "search", lambda *args, **kwargs: [_api_span("new")])
    with pytest.raises(ValueError, match="saved trace ID differs"):
        fetch_spans.main(str(jobs), str(tmp_path), refresh=True)
    assert target.read_bytes() == before


def test_fetch_preserves_container_and_dispatch_evidence(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"job_id": "job", "ereq": "episode", "round": "round2"}]))
    span = _api_span("run")
    span["attributes"]["custom"].update(container={"exit_code": 137}, dispatch={"attempt_id": "attempt"}, player_file={"count": 2})
    monkeypatch.setattr(fetch_spans, "search", lambda *args, **kwargs: [span])
    monkeypatch.setattr(fetch_spans.time, "sleep", lambda _: None)
    fetch_spans.main(str(jobs), str(tmp_path))
    saved = json.loads((tmp_path / "job.json").read_text())["spans"][0]
    assert saved["custom"]["container"] == {"exit_code": 137}
    assert saved["custom"]["dispatch"] == {"attempt_id": "attempt"}
    assert saved["custom"]["player_file"] == {"count": 2}


def test_explicit_and_legacy_startup_positions_remain_separate():
    bootstrap = _span("boot", name="game.bootstrap", seconds=4)
    launch, loop = _span("launch", name="player.launch"), _span("loop", name="episode.loop")
    finalize = _span("upload", name="episode.finalize", seconds=1)
    finalize.update(start="2026-09-09T00:00:03Z", end="2026-09-09T00:00:04Z")
    spans = [bootstrap, launch, loop, finalize]
    old = reconcile_spans.worker_view({"spans": spans})
    assert old["legacy_bootstrap_s"] == 4 and old["container_bootstrap_s"] is None
    assert old["post_loop_gap_s"] is None
    for span in spans:
        span["custom"]["timing"] = {"source": "worker_artifact"}
    bootstrap["custom"]["timing"]["boundary"] = "container_started_to_health_observed"
    new = reconcile_spans.worker_view({"spans": spans})
    assert new["legacy_bootstrap_s"] is None and new["container_bootstrap_s"] == 4
    assert new["first_step_s"] is None
    assert new["post_loop_gap_s"] == 1


def test_grouping_separates_restart_positions_and_startup_outcomes():
    spans = [_span("old", name="container.run"), _span("current", name="container.run")]
    spans[0]["custom"]["container"] = {"run_position": "previous"}
    spans[1]["custom"]["container"] = {"run_position": "current"}
    startup = [_span("dead", name="player.startup_observed", role="player"), _span("started", name="player.startup_observed", role="player")]
    for span, outcome in zip(startup, ("dead", "started"), strict=True):
        span["custom"]["player"] = {"startup_outcome": outcome}
    view = reconcile_spans.worker_view({"spans": spans + startup})
    assert len(view["span_groups"]) == 4
    assert "container.run" not in view["ambiguous_names"]
    assert {key[5] for key in view["span_groups"] if key[0] == "container.run"} == {"previous", "current"}
    assert {key[6] for key in view["span_groups"] if key[0] == "player.startup_observed"} == {"dead", "started"}


def test_timestamp_markers_are_counts_not_duration_statistics(tmp_path, capsys):
    traces, results = tmp_path / "traces", tmp_path / "results"
    traces.mkdir()
    episode = results / "ereq_test"
    episode.mkdir(parents=True)
    marker = _span("marker", name="container.started", seconds=0)
    (traces / "job.json").write_text(json.dumps({"job_id": "job", "round": "round2", "spans": [marker]}))
    (episode / "episode.json").write_text(json.dumps({"job_id": "job"}))
    values = {
        key: None
        for key in [
            "game_listening_to_first_health_s",
            "game_listening_to_first_global_s",
            "game_process_birth_to_first_mark_s",
            "game_bootstrap_import_s",
            "game_bootstrap_config_read_s",
            "game_bootstrap_config_decode_s",
            "game_bootstrap_payload_build_s",
            "game_bootstrap_server_start_s",
            "episode_loop_measurement_s",
            "replay_prepare_s",
        ]
    }
    values.update(slots=[], player_slot_count=1, step_count=1, mode="echo")
    (episode / "results.json").write_text(json.dumps(values))
    reconcile_spans.main(str(traces), str(results))
    output = capsys.readouterr().out
    durations, markers = output.split("## Timestamp markers")
    assert "container.started" not in durations
    assert "| container.started | game | worker | - | - | - | 1 |" in markers


def test_report_table_columns_and_explicit_reconciliation(tmp_path, capsys):
    test_timestamp_markers_are_counts_not_duration_statistics(tmp_path, capsys)
    trace_path = tmp_path / "traces/job.json"
    trace = json.loads(trace_path.read_text())
    boot = _span("boot", name="game.bootstrap", seconds=4)
    boot["custom"]["timing"] = {"source": "worker_artifact", "boundary": "container_started_to_health_observed"}
    trace["spans"] += [boot, _span("viewer", name="worker.viewer_wait", seconds=2)]
    trace_path.write_text(json.dumps(trace))
    values_path = tmp_path / "results/ereq_test/results.json"
    values = json.loads(values_path.read_text())
    values["game_listening_to_first_health_s"] = 1
    values_path.write_text(json.dumps(values))
    reconcile_spans.main(str(tmp_path / "traces"), str(tmp_path / "results"))
    output = capsys.readouterr().out
    width = None
    for line in output.splitlines():
        if line.startswith("|"):
            cells = len(line.split("|")) - 2
            if width is None:
                width = cells
            assert cells == width, line
        else:
            width = None
    assert "explicit viewer wait: median 2,000" in output
    assert "container start to health upper bound median 4,000" in output
    assert "legacy worker entry to health median -" in output
    assert "worker game_boot_s median" not in output
