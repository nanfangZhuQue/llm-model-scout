import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest

from llm_model_scout.agent.history import SCHEMA_VERSION, HistoryError, HistoryStore
from llm_model_scout.domain import DiscoveryResult, ModelResult, ProbeConfig, ScanPolicy, ScanReport
from llm_model_scout.report import report_dict

NOW = "2026-11-26T12:00:00+00:00"


def view(scan_id="scan-1", state="queued", **fields):
    return {"scan_id": scan_id, "state": state, "progress": {"discovered": 0}, **fields}


def conditions():
    return {
        "provider_revision": 3,
        "endpoints": [
            {
                "id": "openai",
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "probe": asdict(ProbeConfig()),
                "models": ["sample"],
                "uses_proxy": True,
            }
        ],
        "policy": asdict(ScanPolicy()),
    }


@pytest.fixture
def store(tmp_path):
    history = HistoryStore(tmp_path / "history.sqlite3")
    history.open()
    try:
        yield history
    finally:
        history.close()


def test_initializes_version_and_allows_readonly_snapshot_with_live_writer(store):
    other = HistoryStore(store.path)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    assert other.get("scan-1") is None
    store.create(view(), conditions())
    assert other.get("scan-1") == {
        "view": view(),
        "conditions": conditions(),
        "discovery": [],
        "results": [],
        "report": None,
    }
    with pytest.raises(HistoryError, match="in use"):
        other.open()
    store.close()
    other.open()
    other.close()
    assert other.get("scan-1")["view"]["state"] == "queued"


def test_unknown_schema_is_rejected_and_initialization_releases_lock(tmp_path):
    history = HistoryStore(tmp_path / "history.sqlite3")
    with closing(sqlite3.connect(history.path)) as db:
        db.execute("PRAGMA user_version = 999")
    for candidate in (history, HistoryStore(history.path)):
        with pytest.raises(HistoryError, match="Unsupported"):
            candidate.open()
    with pytest.raises(HistoryError, match="Unsupported"):
        history.get("scan-1")
    with closing(sqlite3.connect(history.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (999,)


def test_checkpoint_rolls_back_view_and_batch_when_result_cannot_be_written(store):
    store.create(view(), conditions())
    discovered = asdict(DiscoveryResult("openai", "openai_chat", "api", models=["a"]))
    running = view(state="running", progress={"discovered": 1})
    store.checkpoint(running, discovery=discovered)
    before = store.get("scan-1")
    broken = {"model": "a", "latency_ms": float("nan")}
    with pytest.raises(HistoryError, match="could not be read or written"):
        store.checkpoint(
            view(state="running", progress={"discovered": 2, "finished": 1}),
            discovery=discovered,
            result=broken,
        )
    assert store.get("scan-1") == before


def test_recovers_queued_running_and_cancelling_without_replacing_details(store):
    result = asdict(ModelResult("openai", "openai_chat", "sample", "baseline", status="available"))
    discovered = asdict(DiscoveryResult("openai", "openai_chat", "api", models=["sample"]))
    for state in ("queued", "running", "cancelling", "failed"):
        store.create(view(state), conditions())
        if state == "failed":
            store.checkpoint(view(state, "running"), discovery=discovered, result=result)
            store.finish(view(state, state), None)
        elif state != "queued":
            store.checkpoint(
                view(state, state, progress={"discovered": 1, "finished": 1}),
                discovery=discovered,
                result=result,
            )
    original_failed = store.get("failed")
    assert original_failed["results"] == [result]
    assert original_failed["report"] is None
    store.close()
    restarted = HistoryStore(store.path)
    restarted.open()
    try:
        restarted.recover(NOW)
        assert restarted.get("failed") == original_failed
        for original in ("queued", "running", "cancelling"):
            record = restarted.get(original)
            assert record["view"]["state"] == "interrupted"
            assert record["view"]["finished_at"] == NOW
            assert record["report"] is None
            if original != "queued":
                assert record["results"] == [result]
                assert record["discovery"] == [discovered]
                assert record["view"]["progress"]["finished"] == 1
        with pytest.raises(HistoryError, match="already closed"):
            restarted.checkpoint(view("running", "running"))
    finally:
        restarted.close()


def test_finalization_replaces_partial_rows_and_serializes_only_known_fields(store):
    secret = "must-not-reach-history"
    snapshot = conditions()
    snapshot["endpoints"][0]["api_key"] = secret
    snapshot["endpoints"][0]["probe"]["api_key"] = secret
    snapshot["endpoints"][0]["proxy"] = secret
    store.create(view(api_key=secret), snapshot)
    discovered = DiscoveryResult("openai", "openai_chat", "api", models=["a", "b"])
    endpoint = {
        "id": "openai",
        "protocol": "openai_chat",
        "base_url": "https://example.test/v1",
        "probe": asdict(ProbeConfig()),
    }
    first = ModelResult("openai", "openai_chat", "a", "baseline", status="available")
    second = ModelResult("openai", "openai_chat", "a", "baseline", status="unavailable")
    report = report_dict(
        ScanReport(
            "Provider",
            [endpoint],
            asdict(ScanPolicy()),
            discovery=[discovered],
            results=[first, second],
            requests_sent=2,
        )
    )
    for result in (first, second):
        store.checkpoint(view(state="running"), result={**asdict(result), "raw_response": secret})
    assert len(store.get("scan-1")["results"]) == 2
    bad_report = {**report, "results": [{"latency_ms": float("nan")}]}
    with pytest.raises(HistoryError):
        store.finish(view(state="completed"), bad_report)
    assert store.get("scan-1")["view"]["state"] == "running"
    assert len(store.get("scan-1")["results"]) == 2
    finished = view(state="completed", report_available=True)
    store.finish(finished, {**report, "raw_response": secret})
    record = store.get("scan-1")
    assert record["view"] == finished
    assert record["conditions"] == conditions()
    assert record["report"] == report
    assert record["results"] == report["results"]
    assert record["discovery"] == report["discovery"]
    assert secret.encode() not in store.path.read_bytes()


def test_upgrades_v1_database_preserving_conditions_and_checkpoint(tmp_path):
    store = HistoryStore(tmp_path / "legacy.sqlite3")
    legacy = view(
        state="running",
        provider_id="provider-1",
        provider_name="Original Name",
        created_at=NOW,
        progress={"finished": 1},
        report_available=False,
    )
    result = asdict(ModelResult("openai", "openai_chat", "sample", "baseline", status="available"))
    with closing(sqlite3.connect(store.path)) as db, db:
        db.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE scan_runs (
                scan_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                view_json TEXT NOT NULL,
                conditions_json TEXT NOT NULL,
                report_json TEXT
            );
            CREATE TABLE scan_discovery (
                scan_id TEXT NOT NULL REFERENCES scan_runs(scan_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (scan_id, ordinal)
            );
            CREATE TABLE scan_results (
                scan_id TEXT NOT NULL REFERENCES scan_runs(scan_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (scan_id, ordinal)
            );
            """
        )
        db.execute(
            "INSERT INTO scan_runs VALUES (?, ?, ?, ?, NULL)",
            ("scan-1", "running", json.dumps(legacy), json.dumps(conditions())),
        )
        db.execute("INSERT INTO scan_results VALUES (?, 0, ?)", ("scan-1", json.dumps(result)))

    store.open()
    try:
        page = store.list_views(limit=5, offset=0, provider_id="provider-1")
        assert page["scans"] == [legacy] and page["total"] == 1
        store.recover(NOW)
        snapshot = store.get("scan-1")
        assert snapshot["results"] == [result]
        assert snapshot["conditions"] == conditions()
        assert snapshot["view"]["state"] == "interrupted"
        assert snapshot["view"]["report_available"] is True
        assert snapshot["view"]["report_partial"] is True
        assert store.get_view("scan-1") == snapshot["view"]
        assert store.list_views(limit=5, offset=0)["scans"] == [snapshot["view"]]
    finally:
        store.close()


def test_pages_newest_first_and_filters_with_all_retained_provider_choices(store):
    samples = (
        ("id-a", "alpha", "Old Alpha", "2026-11-25T12:00:00+00:00"),
        ("id-b", "beta", "Beta", NOW),
        ("id-c", "alpha", "New Alpha", NOW),
        ("id-d", "gamma", "Gamma", "2026-11-24T12:00:00+00:00"),
    )
    for scan_id, provider_id, name, created_at in samples:
        store.create(
            view(scan_id, provider_id=provider_id, provider_name=name, created_at=created_at),
            conditions(),
        )
    page = store.list_views(limit=2, offset=0)
    assert page["total"] == 4
    assert [row["scan_id"] for row in page["scans"]] == ["id-c", "id-b"]
    tail = store.list_views(limit=2, offset=2)
    assert [row["scan_id"] for row in tail["scans"]] == ["id-a", "id-d"]
    filtered = store.list_views(limit=1, offset=1, provider_id="alpha")
    assert filtered["total"] == 2
    assert [row["scan_id"] for row in filtered["scans"]] == ["id-a"]
    assert (
        filtered["providers"]
        == page["providers"]
        == [
            {"provider_id": "alpha", "provider_name": "New Alpha"},
            {"provider_id": "beta", "provider_name": "Beta"},
            {"provider_id": "gamma", "provider_name": "Gamma"},
        ]
    )
    assert store.list_views(limit=2, offset=0, provider_id="missing")["total"] == 0
    assert store.get_view("missing") is None
    assert not any(row["report_available"] for row in page["scans"])


def test_retention_prunes_terminal_details_by_finish_time_and_spares_active_runs(store):
    discovered = asdict(DiscoveryResult("openai", "openai_chat", "api", models=["sample"]))
    result = asdict(ModelResult("openai", "openai_chat", "sample", "baseline"))
    for scan_id, finished_at in (
        ("oldest-finish", "2026-11-25T12:00:00+00:00"),
        ("second-finish", "2026-11-25T13:00:00+00:00"),
        ("third-finish", "2026-11-25T14:00:00+00:00"),
    ):
        store.create(view(scan_id, created_at=NOW, provider_id=scan_id), conditions())
        store.checkpoint(view(scan_id, "running"), discovery=discovered, result=result)
        store.finish(view(scan_id, "failed", finished_at=finished_at), None)

    assert store.recover(NOW, max_history=2) == ["oldest-finish"]
    assert store.get_view("oldest-finish") is None
    store.create(view("active", created_at="2026-10-01"), conditions())
    store.checkpoint(view("active", "running"), discovery=discovered)
    assert store.get_view("active")["report_available"] is False
    store.create(view("new", created_at="2026-10-02"), conditions())
    store.checkpoint(view("new", "running"), discovery=discovered)
    assert store.finish(view("new", "failed", finished_at=NOW), None) == ["second-finish"]
    assert store.get_view("new")["report_partial"] is True
    page = store.list_views(limit=5, offset=0)
    assert page["total"] == 3
    assert {row["scan_id"] for row in page["scans"]} == {"third-finish", "active", "new"}
    assert page["providers"] == [{"provider_id": "third-finish", "provider_name": "third-finish"}]
    with closing(sqlite3.connect(store.path)) as db:
        for table in ("scan_discovery", "scan_results"):
            count = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE scan_id IN (?, ?)",
                ("oldest-finish", "second-finish"),
            ).fetchone()[0]
            assert count == 0


def test_retest_key_and_run_are_atomic_and_follow_retention(store):
    store.recover(NOW, max_history=1)
    store.create(view("old-source"), conditions())
    store.finish(view("old-source", "completed", finished_at="2026-11-25T13:00:00+00:00"), None)
    source = {"baseline_id": "old-source", "current_id": "source", "endpoint_id": "openai"}
    key = ("request-id", "fingerprint")
    store.create(
        view("retest", source={**source, "api_key": "not-stored"}), conditions(), retest_key=key
    )

    with pytest.raises(HistoryError):
        store.create(view("duplicate", source=source), conditions(), retest_key=key)
    assert store.get("duplicate") is None
    store.finish(view("retest", "completed", source=source, finished_at=NOW), None)
    assert store.get("old-source") is None
    assert HistoryStore(store.path).get_retest("request-id")[1]["source"] == source
    assert store.get_retest("request-id")[0] == "fingerprint"
    store.create(view("new"), conditions())
    store.finish(view("new", "completed", finished_at="2026-11-27T12:00:00+00:00"), None)
    assert store.get_retest("request-id") is None


def test_v2_history_upgrades_with_retest_keys(tmp_path):
    path = tmp_path / "history.sqlite3"
    history = HistoryStore(path)
    history.open()
    history.create(view(), conditions())
    history.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE retest_submissions")
        db.execute("PRAGMA user_version = 2")
    history.open()
    try:
        assert history.get("scan-1")["view"] == view()
        history.create(view("retest"), conditions(), retest_key=("key", "digest"))
        assert history.get_retest("key")[0] == "digest"
    finally:
        history.close()
