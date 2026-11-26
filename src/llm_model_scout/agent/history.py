"""Versioned, local storage for redacted Agent scans and their checkpoints."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3

_SCHEMA = (
    """CREATE TABLE scan_runs (
        scan_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        view_json TEXT NOT NULL,
        conditions_json TEXT NOT NULL,
        report_json TEXT
    )""",
    """CREATE TABLE scan_discovery (
        scan_id TEXT NOT NULL REFERENCES scan_runs(scan_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (scan_id, ordinal)
    )""",
    """CREATE TABLE scan_results (
        scan_id TEXT NOT NULL REFERENCES scan_runs(scan_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (scan_id, ordinal)
    )""",
)
_RETEST_SCHEMA = """CREATE TABLE retest_submissions (
    request_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL UNIQUE REFERENCES scan_runs(scan_id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL
)"""
_SOURCE_FIELDS = ("baseline_id", "current_id", "endpoint_id")
_ACTIVE = "('queued', 'running', 'cancelling')"
_TERMINAL = "('completed', 'failed', 'cancelled', 'interrupted')"
_PARTIAL_STATES = ("failed", "interrupted")
_HAS_PARTIAL = f"""scan_runs.report_json IS NULL AND scan_runs.state IN {_PARTIAL_STATES} AND (
    EXISTS (SELECT 1 FROM scan_discovery WHERE scan_id = scan_runs.scan_id) OR
    EXISTS (SELECT 1 FROM scan_results WHERE scan_id = scan_runs.scan_id)
)"""
_VIEW_SELECT = f"view_json, report_json IS NOT NULL, {_HAS_PARTIAL}"
_VIEW = (
    "scan_id provider_id provider_name operation mode capability state created_at started_at "
    "finished_at error report_available"
).split()
_PROGRESS = "discovered planned finished skipped cancelled total".split()
_POLICY = (
    "concurrency timeout retries max_requests max_models scan_timeout rate_limit "
    "max_response_bytes max_pages backoff_base"
).split()
_PROBE = "profile_id max_tokens temperature prompt capability".split()
_ENDPOINT = "id protocol base_url".split()
_DISCOVERY = (
    "endpoint_id protocol source status models error_category error_message http_status pages"
).split()
_RESULT = (
    "endpoint_id protocol model probe_id status http_status error_category error_message "
    "reason attempts latency_ms elapsed_ms timestamp diagnostic_code suggestion stop_reason "
    "capability"
).split()


class HistoryError(RuntimeError):
    """A local history operation failed without exposing disk paths or stored contents."""


def _pick(data: dict, fields) -> dict:
    return {key: data[key] for key in fields if key in data}


def _view(data: dict) -> dict:
    view = {**_pick(data, _VIEW), "progress": _pick(data["progress"], _PROGRESS)}
    if "source" in data:
        view["source"] = _pick(data["source"], _SOURCE_FIELDS)
    return view


def _summary(row) -> dict:
    payload, has_report, partial = row
    view = json.loads(payload)
    view["report_available"] = bool(has_report or partial)
    if partial:
        view["report_partial"] = True
    return view


def _endpoint(data: dict, fields) -> dict:
    return {**_pick(data, fields), "probe": _pick(data["probe"], _PROBE)}


def _conditions(data: dict) -> dict:
    fields = [*_ENDPOINT, "auth", "api_version", "models", "uses_proxy"]
    return {
        "provider_revision": data.get("provider_revision"),
        "endpoints": [_endpoint(endpoint, fields) for endpoint in data["endpoints"]],
        "policy": _pick(data["policy"], _POLICY),
    }


def _report_header(report: dict) -> dict:
    return {
        "schema_version": report["schema_version"],
        "tool": _pick(report["tool"], ("name", "version")),
        "provider": {
            "name": report["provider"]["name"],
            "endpoints": [_endpoint(ep, _ENDPOINT) for ep in report["provider"]["endpoints"]],
        },
        "scan": {
            **_pick(
                report["scan"], ("status", "reason", "started_at", "finished_at", "requests_sent")
            ),
            "policy": _pick(report["scan"]["policy"], _POLICY),
        },
        "summary": _pick(
            report["summary"], ("total", "available", "unavailable", "skipped", "cancelled")
        ),
    }


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def _add_query_columns(db) -> None:
    db.execute("ALTER TABLE scan_runs ADD COLUMN provider_id TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE scan_runs ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE scan_runs ADD COLUMN finished_at TEXT")
    rows = db.execute("SELECT scan_id, view_json FROM scan_runs").fetchall()
    for scan_id, payload in rows:
        view = json.loads(payload)
        db.execute(
            """UPDATE scan_runs SET provider_id = ?, created_at = ?, finished_at = ?
            WHERE scan_id = ?""",
            (
                view.get("provider_id", ""),
                view.get("created_at", ""),
                view.get("finished_at"),
                scan_id,
            ),
        )
    db.execute("CREATE INDEX scan_runs_created ON scan_runs (created_at DESC, scan_id DESC)")
    db.execute(
        "CREATE INDEX scan_runs_provider_created ON scan_runs "
        "(provider_id, created_at DESC, scan_id DESC)"
    )
    db.execute(
        "CREATE INDEX scan_runs_terminal ON scan_runs "
        f"(finished_at DESC, created_at DESC, scan_id DESC) WHERE state IN {_TERMINAL}"
    )


class HistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock_file = None
        self._max_history = None

    def open(self) -> None:
        if self._lock_file is not None:
            return
        try:
            self.path = self.path.resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            file = self.path.with_name(f"{self.path.name}-lock").open("a+b")
        except OSError:
            raise HistoryError("Cannot open the scan history writer lock") from None
        try:
            file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            raise HistoryError("Scan history is in use or its writer lock is unavailable") from None
        self._lock_file = file
        try:
            with self._connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == SCHEMA_VERSION:
                    return
                if (
                    version == 0
                    and not db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
                ):
                    for statement in _SCHEMA:
                        db.execute(statement)
                elif version not in (1, 2):
                    raise HistoryError("Unsupported scan history database version")
                if version < 2:
                    _add_query_columns(db)
                db.execute(_RETEST_SCHEMA)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    @contextmanager
    def _connect(self, *, readonly=False):
        if readonly:
            path = f"{self.path.resolve().as_uri()}?mode=ro"
        else:
            if self._lock_file is None:
                raise HistoryError("Scan history is not open for writing")
            path = self.path
        try:
            db = sqlite3.connect(path, uri=readonly, timeout=1)
            try:
                with db:
                    db.execute("PRAGMA foreign_keys = ON")
                    db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
                    yield db
            finally:
                db.close()
        except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
            raise HistoryError("Scan history could not be read or written") from None

    def recover(self, now: str, *, max_history: int | None = None) -> list[str]:
        """Close jobs left behind by the Agent that last owned this database."""
        if max_history is not None and max_history < 1:
            raise ValueError("Scan history retention must be positive")
        with self._connect() as db:
            rows = db.execute(
                f"SELECT view_json FROM scan_runs WHERE state IN {_ACTIVE}"
            ).fetchall()
            for (payload,) in rows:
                view = json.loads(payload)
                view.update(
                    state="interrupted",
                    error="Agent stopped before the scan finished",
                    finished_at=now,
                    report_available=False,
                )
                self._update_view(db, view)
            removed = self._prune(db, max_history)
        self._max_history = max_history
        return removed

    def create(
        self, view: dict, conditions: dict, *, retest_key: tuple[str, str] | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO scan_runs (
                    scan_id, state, view_json, conditions_json, provider_id, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    view["scan_id"],
                    view["state"],
                    _json(_view(view)),
                    _json(_conditions(conditions)),
                    view.get("provider_id", ""),
                    view.get("created_at", ""),
                    view.get("finished_at"),
                ),
            )
            if retest_key is not None:
                request_id, fingerprint = retest_key
                db.execute(
                    "INSERT INTO retest_submissions VALUES (?, ?, ?)",
                    (request_id, view["scan_id"], fingerprint),
                )

    def get_retest(self, request_id: str) -> tuple[str, dict] | None:
        if not self.path.exists():
            return None
        with self._connect(readonly=True) as db:
            self._check_version(db)
            row = db.execute(
                f"""SELECT retest_submissions.fingerprint, {_VIEW_SELECT}
                    FROM retest_submissions JOIN scan_runs USING (scan_id)
                    WHERE request_id = ?""",
                (request_id,),
            ).fetchone()
            return (row[0], _summary(row[1:])) if row is not None else None

    @staticmethod
    def _update_view(db, view: dict) -> None:
        changed = db.execute(
            f"""UPDATE scan_runs SET state = ?, view_json = ?, finished_at = ?
                WHERE scan_id = ? AND state IN {_ACTIVE}""",
            (view["state"], _json(_view(view)), view.get("finished_at"), view["scan_id"]),
        )
        if changed.rowcount != 1:
            raise HistoryError("Scan history record is missing or already closed")

    @staticmethod
    def _append(db, table: str, scan_id: str, payload: dict) -> None:
        # The ordinal preserves distinct events even if redaction makes identifiers identical.
        db.execute(
            f"""INSERT INTO {table} (scan_id, ordinal, payload)
                SELECT ?, COALESCE(MAX(ordinal) + 1, 0), ? FROM {table} WHERE scan_id = ?""",
            (scan_id, _json(payload), scan_id),
        )

    def checkpoint(
        self, view: dict, *, discovery: dict | None = None, result: dict | None = None
    ) -> None:
        with self._connect() as db:
            self._update_view(db, view)
            if discovery is not None:
                self._append(db, "scan_discovery", view["scan_id"], _pick(discovery, _DISCOVERY))
            if result is not None:
                self._append(db, "scan_results", view["scan_id"], _pick(result, _RESULT))

    def finish(self, view: dict, report: dict | None) -> list[str]:
        with self._connect() as db:
            self._update_view(db, view)
            if report is not None:
                for table, key, fields in (
                    ("scan_discovery", "discovery", _DISCOVERY),
                    ("scan_results", "results", _RESULT),
                ):
                    db.execute(f"DELETE FROM {table} WHERE scan_id = ?", (view["scan_id"],))
                    db.executemany(
                        f"INSERT INTO {table} (scan_id, ordinal, payload) VALUES (?, ?, ?)",
                        (
                            (view["scan_id"], ordinal, _json(_pick(row, fields)))
                            for ordinal, row in enumerate(report[key])
                        ),
                    )
                db.execute(
                    "UPDATE scan_runs SET report_json = ? WHERE scan_id = ?",
                    (_json(_report_header(report)), view["scan_id"]),
                )
            return self._prune(db, self._max_history)

    @staticmethod
    def _prune(db, max_history: int | None) -> list[str]:
        if max_history is None:
            return []
        rows = db.execute(
            f"""SELECT scan_id FROM scan_runs WHERE state IN {_TERMINAL}
                ORDER BY finished_at DESC, created_at DESC, scan_id DESC LIMIT -1 OFFSET ?""",
            (max_history,),
        ).fetchall()
        db.executemany("DELETE FROM scan_runs WHERE scan_id = ?", rows)
        return [scan_id for (scan_id,) in rows]

    def list_views(self, *, limit: int, offset: int, provider_id: str | None = None) -> dict:
        """Page and count in one readonly snapshot, with provider choices from all history."""
        if not self.path.exists():
            return {"scans": [], "total": 0, "providers": []}
        where = "" if provider_id is None else "WHERE provider_id = ?"
        filters = () if provider_id is None else (provider_id,)
        with self._connect(readonly=True) as db:
            self._check_version(db)
            total = db.execute(f"SELECT COUNT(*) FROM scan_runs {where}", filters).fetchone()[0]
            scans = [
                _summary(row)
                for row in db.execute(
                    f"""SELECT {_VIEW_SELECT}
                        FROM scan_runs {where}
                        ORDER BY created_at DESC, scan_id DESC LIMIT ? OFFSET ?""",
                    (*filters, limit, offset),
                )
            ]
            providers = [
                {
                    "provider_id": key,
                    "provider_name": json.loads(payload).get("provider_name") or key,
                }
                for key, payload in db.execute(
                    """SELECT choices.provider_id, (
                            SELECT view_json FROM scan_runs AS latest
                            WHERE latest.provider_id = choices.provider_id
                            ORDER BY created_at DESC, scan_id DESC LIMIT 1
                        )
                        FROM (SELECT DISTINCT provider_id FROM scan_runs WHERE provider_id <> '')
                            AS choices
                        ORDER BY choices.provider_id"""
                )
            ]
            return {"scans": scans, "total": total, "providers": providers}

    def get_view(self, scan_id: str) -> dict | None:
        """Read a run's summary without materializing discovery or result rows."""
        if not self.path.exists():
            return None
        with self._connect(readonly=True) as db:
            self._check_version(db)
            row = db.execute(
                f"SELECT {_VIEW_SELECT} FROM scan_runs WHERE scan_id = ?", (scan_id,)
            ).fetchone()
            return _summary(row) if row is not None else None

    @staticmethod
    def _check_version(db) -> None:
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise HistoryError("Unsupported scan history database version")

    def get(self, scan_id: str) -> dict | None:
        """Read a consistent snapshot without acquiring the Agent's writer lock."""
        if not self.path.exists():
            return None
        with self._connect(readonly=True) as db:
            self._check_version(db)
            return self._record(db, scan_id)

    def get_pair(self, baseline_id: str, current_id: str) -> tuple[dict | None, dict | None]:
        """Read both runs from one snapshot, even when another run triggers retention."""
        if not self.path.exists():
            return None, None
        with self._connect(readonly=True) as db:
            self._check_version(db)
            return self._record(db, baseline_id), self._record(db, current_id)

    @staticmethod
    def _record(db, scan_id: str) -> dict | None:
        record = db.execute(
            "SELECT view_json, conditions_json, report_json FROM scan_runs WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if record is None:
            return None
        view_json, conditions, header = record
        details = {
            key: [
                json.loads(payload)
                for (payload,) in db.execute(
                    f"SELECT payload FROM {table} WHERE scan_id = ? ORDER BY ordinal",
                    (scan_id,),
                )
            ]
            for key, table in (("discovery", "scan_discovery"), ("results", "scan_results"))
        }
        view = json.loads(view_json)
        if header is None and view["state"] in _PARTIAL_STATES and any(details.values()):
            view.update(report_available=True, report_partial=True)
        return {
            "view": view,
            "conditions": json.loads(conditions),
            **details,
            "report": {**json.loads(header), **details} if header is not None else None,
        }
