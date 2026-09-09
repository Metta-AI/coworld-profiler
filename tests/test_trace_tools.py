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
        lambda query, **kwargs: [{"attributes": {"trace_id": "trace"}}] if query.startswith("@job.id") else [],
    )
    monkeypatch.setattr(fetch_spans.time, "sleep", lambda _: None)
    fetch_spans.main(str(jobs), str(tmp_path), refresh=True)
    assert [span["span_id"] for span in json.loads(target.read_text())["spans"]] == ["older"]
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


def test_empty_last_page_with_cursor_finishes(monkeypatch):
    pages = iter(
        [
            {"data": [_api_span("first")], "meta": {"page": {"after": "next"}}},
            {"data": [], "meta": {"page": {"after": "empty-page-cursor"}}},
        ]
    )
    monkeypatch.setattr(fetch_spans, "_search_page", lambda _: next(pages))
    assert len(fetch_spans.search("trace_id:trace", limit=1)) == 1


def test_refresh_preserves_unlisted_index_entries(tmp_path, monkeypatch):
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
