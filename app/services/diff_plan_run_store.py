"""M4 运行、执行项和原始 m2.diff.v1 结果的 SQLite 持久化。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
from typing import Any, Iterator
from uuid import UUID, uuid4

from app.schemas.diff_plan import (
    DiffPlanRunItemPayload,
    DiffPlanRunListPayload,
    DiffPlanRunPayload,
    DiffPlanRunProgressPayload,
    DiffPlanRunSummaryPayload,
)
from app.services.diff_plan_store import DiffPlanError, _canonical, _hash, _now


TERMINAL_RUN_STATUSES = {"completed", "completed_with_failures", "cancelled", "failed"}
TERMINAL_ITEM_STATUSES = {
    "identical", "semantic_equal", "changed", "source_missing", "target_missing",
    "both_missing", "read_failed", "business_failed", "orchestration_failed", "cancelled",
}
RETRYABLE_ITEM_STATUSES = {"read_failed", "business_failed", "orchestration_failed", "cancelled"}
ITEM_LEASE_SECONDS = 60


def _future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DiffPlanRunStore:
    def __init__(self, database_path: Path, results_directory: Path, *, retention_days: int = 30):
        self.database_path = Path(database_path)
        self.results_directory = Path(results_directory)
        self.retention_days = max(1, int(retention_days))
        self._lock = Lock()
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self.results_directory.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database_path, timeout=30)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE IF NOT EXISTS diff_plan_runs (
                        run_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL,
                        retry_of_run_id TEXT,
                        request_id TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        plan_version INTEGER NOT NULL,
                        plan_name TEXT NOT NULL,
                        source_endpoint_id TEXT NOT NULL,
                        target_endpoint_ids_json TEXT NOT NULL,
                        workbook_paths_json TEXT NOT NULL,
                        source_revision INTEGER NOT NULL,
                        target_revisions_json TEXT NOT NULL,
                        errors_json TEXT NOT NULL DEFAULT '[]',
                        cancel_requested_at TEXT,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        details_expires_at TEXT,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(plan_id) REFERENCES diff_plans(plan_id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_m4_active_plan_run
                    ON diff_plan_runs(plan_id)
                    WHERE status IN ('queued','preparing','running','cancelling');
                    CREATE INDEX IF NOT EXISTS idx_m4_runs_plan_created
                    ON diff_plan_runs(plan_id, created_at DESC, run_id DESC);
                    CREATE TABLE IF NOT EXISTS diff_plan_run_items (
                        item_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        retry_of_item_id TEXT,
                        ordinal INTEGER NOT NULL,
                        workbook_path TEXT NOT NULL,
                        target_endpoint_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        lease_token TEXT,
                        lease_expires_at TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        recovery_count INTEGER NOT NULL DEFAULT 0,
                        candidate_status TEXT,
                        source_exists INTEGER,
                        target_exists INTEGER,
                        source_sha256 TEXT,
                        target_sha256 TEXT,
                        diff_status TEXT,
                        diff_error_count INTEGER NOT NULL DEFAULT 0,
                        result_ref TEXT UNIQUE,
                        result_path TEXT,
                        error_json TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES diff_plan_runs(run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_m4_items_run_ordinal
                    ON diff_plan_run_items(run_id, ordinal);
                    CREATE INDEX IF NOT EXISTS idx_m4_items_status
                    ON diff_plan_run_items(status, run_id, ordinal);
                    CREATE TABLE IF NOT EXISTS diff_plan_run_commands (
                        request_id TEXT PRIMARY KEY,
                        command_type TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                item_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(diff_plan_run_items)"
                    ).fetchall()
                }
                migrations = {
                    "lease_token": "TEXT",
                    "lease_expires_at": "TEXT",
                    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                    "recovery_count": "INTEGER NOT NULL DEFAULT 0",
                }
                for column, definition in migrations.items():
                    if column not in item_columns:
                        connection.execute(
                            f"ALTER TABLE diff_plan_run_items ADD COLUMN {column} {definition}"
                        )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def _progress(rows: list[sqlite3.Row]) -> DiffPlanRunProgressPayload:
        counts = {status: 0 for status in TERMINAL_ITEM_STATUSES}
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] += 1
        processed = sum(counts.values())
        total = len(rows)
        return DiffPlanRunProgressPayload(
            total_items=total,
            processed_items=processed,
            identical_items=counts["identical"],
            semantic_equal_items=counts["semantic_equal"],
            changed_items=counts["changed"],
            missing_items=counts["source_missing"] + counts["target_missing"] + counts["both_missing"],
            failed_items=counts["read_failed"] + counts["business_failed"] + counts["orchestration_failed"],
            cancelled_items=counts["cancelled"],
            ratio=processed / total if total else 0,
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> DiffPlanRunItemPayload:
        return DiffPlanRunItemPayload(
            item_id=row["item_id"], retry_of_item_id=row["retry_of_item_id"], ordinal=row["ordinal"],
            workbook_path=row["workbook_path"], target_endpoint_id=row["target_endpoint_id"],
            status=row["status"], candidate_status=row["candidate_status"],
            source_exists=None if row["source_exists"] is None else bool(row["source_exists"]),
            target_exists=None if row["target_exists"] is None else bool(row["target_exists"]),
            source_sha256=row["source_sha256"], target_sha256=row["target_sha256"],
            diff_status=row["diff_status"], diff_error_count=row["diff_error_count"],
            result_ref=row["result_ref"], error=json.loads(row["error_json"]) if row["error_json"] else None,
            started_at=row["started_at"], finished_at=row["finished_at"],
        )

    def _payload(self, connection: sqlite3.Connection, row: sqlite3.Row) -> DiffPlanRunPayload:
        item_rows = connection.execute(
            "SELECT * FROM diff_plan_run_items WHERE run_id=? ORDER BY ordinal", (row["run_id"],)
        ).fetchall()
        expires = row["details_expires_at"]
        expired = bool(expires and expires <= _now())
        return DiffPlanRunPayload(
            run_id=row["run_id"], plan_id=row["plan_id"], retry_of_run_id=row["retry_of_run_id"],
            status=row["status"], plan_version=row["plan_version"], plan_name=row["plan_name"],
            source_endpoint_id=row["source_endpoint_id"], target_endpoint_ids=json.loads(row["target_endpoint_ids_json"]),
            workbook_paths=json.loads(row["workbook_paths_json"]), source_revision=row["source_revision"],
            target_revisions=json.loads(row["target_revisions_json"]), progress=self._progress(item_rows),
            items=[self._item(item) for item in item_rows], errors=json.loads(row["errors_json"]),
            cancel_requested_at=row["cancel_requested_at"], created_at=row["created_at"], started_at=row["started_at"],
            finished_at=row["finished_at"], details_expires_at=expires, details_expired=expired,
        )

    @staticmethod
    def _summary(payload: DiffPlanRunPayload) -> DiffPlanRunSummaryPayload:
        data = {
            key: value for key, value in payload.model_dump(mode="json").items()
            if key in DiffPlanRunSummaryPayload.model_fields
        }
        data["schema_version"] = "m4.diff-plan-run-summary.v1"
        return DiffPlanRunSummaryPayload.model_validate(data)

    def create_run(
        self, *, request_id: UUID, request_hash: str, plan: Any,
        source_revision: int, target_revisions: dict[str, int],
        retry_of_run_id: UUID | str | None = None,
        retry_items: list[DiffPlanRunItemPayload] | None = None,
    ) -> tuple[DiffPlanRunPayload, bool]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute("SELECT * FROM diff_plan_runs WHERE request_id=?", (str(request_id),)).fetchone()
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DiffPlanError("DIFF_PLAN_RUN_IDEMPOTENCY_CONFLICT", "相同 request_id 已用于不同运行命令", status_code=409)
                return self._payload(connection, replay), False
            active = connection.execute(
                "SELECT run_id FROM diff_plan_runs WHERE plan_id=? AND status IN ('queued','preparing','running','cancelling')",
                (str(plan.plan_id),),
            ).fetchone()
            if active is not None:
                raise DiffPlanError("DIFF_PLAN_RUN_ACTIVE", "同一计划已有运行中的任务", status_code=409)
            run_id = str(uuid4())
            target_ids = list(target_revisions)
            workbook_paths = list(plan.workbook_paths)
            connection.execute(
                """INSERT INTO diff_plan_runs VALUES (
                    ?,?,?,?,?, 'queued',?,?,?,?,?,?,?, '[]', NULL, ?,NULL,NULL,NULL,?
                )""",
                (run_id, str(plan.plan_id), str(retry_of_run_id) if retry_of_run_id else None,
                 str(request_id), request_hash, plan.version, plan.name, plan.source_endpoint_id,
                 _canonical(target_ids), _canonical(workbook_paths), source_revision,
                 _canonical(target_revisions), now, now),
            )
            selected = retry_items or [
                None for _ in range(len(workbook_paths) * len(target_ids))
            ]
            ordinal = 0
            if retry_items:
                definitions = [(item.workbook_path, item.target_endpoint_id, str(item.item_id)) for item in retry_items]
            else:
                definitions = [(path, target, None) for path in workbook_paths for target in target_ids]
            for path_value, target_id, retry_of_item_id in definitions:
                connection.execute(
                    """INSERT INTO diff_plan_run_items (
                        item_id,run_id,retry_of_item_id,ordinal,workbook_path,target_endpoint_id,status,updated_at
                    ) VALUES (?,?,?,?,?,?,'queued',?)""",
                    (str(uuid4()), run_id, retry_of_item_id, ordinal, path_value, target_id, now),
                )
                ordinal += 1
            row = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (run_id,)).fetchone()
            return self._payload(connection, row), True

    def replay_run(self, request_id: UUID, request_hash: str) -> DiffPlanRunPayload | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM diff_plan_runs WHERE request_id=?", (str(request_id),)).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise DiffPlanError("DIFF_PLAN_RUN_IDEMPOTENCY_CONFLICT", "相同 request_id 已用于不同运行命令", status_code=409)
            return self._payload(connection, row)

    def get_run(self, run_id: UUID | str) -> DiffPlanRunPayload:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (str(run_id),)).fetchone()
            if row is None:
                raise DiffPlanError("DIFF_PLAN_RUN_NOT_FOUND", "计划运行不存在", status_code=404)
            return self._payload(connection, row)

    def list_runs(self, plan_id: UUID | str) -> DiffPlanRunListPayload:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM diff_plan_runs WHERE plan_id=? ORDER BY created_at DESC, run_id DESC", (str(plan_id),)
            ).fetchall()
            runs = [self._summary(self._payload(connection, row)) for row in rows]
        return DiffPlanRunListPayload(runs=runs, total=len(runs))

    def latest_run(self, plan_id: UUID | str) -> DiffPlanRunSummaryPayload | None:
        listed = self.list_runs(plan_id)
        return listed.runs[0] if listed.runs else None

    def claim_preparation(self) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM diff_plan_runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            connection.execute("UPDATE diff_plan_runs SET status='preparing',started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?", (now, now, row["run_id"]))
            return dict(row)

    def update_candidate(self, item_id: str, *, status: str, candidate_status: str,
                         source_exists: bool, target_exists: bool,
                         source_sha256: str | None, target_sha256: str | None,
                         error: dict[str, Any] | None = None) -> None:
        now = _now()
        terminal = status in TERMINAL_ITEM_STATUSES
        with self._connect() as connection:
            connection.execute(
                """UPDATE diff_plan_run_items SET status=?,candidate_status=?,source_exists=?,target_exists=?,
                   source_sha256=?,target_sha256=?,error_json=?,finished_at=?,updated_at=?
                   WHERE item_id=? AND status='queued'""",
                (status, candidate_status, int(source_exists), int(target_exists), source_sha256, target_sha256,
                 _canonical(error) if error else None, now if terminal else None, now, item_id),
            )

    def finish_preparation(self, run_id: str, error: dict[str, Any] | None = None) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in {"preparing", "cancelling"}:
                return
            if error:
                connection.execute(
                    "UPDATE diff_plan_runs SET status='failed',errors_json=?,finished_at=?,details_expires_at=?,updated_at=? WHERE run_id=?",
                    (_canonical([error]), now, _future(self.retention_days), now, run_id),
                )
                connection.execute(
                    "UPDATE diff_plan_run_items SET status='orchestration_failed',error_json=?,finished_at=?,updated_at=? WHERE run_id=? AND status='queued'",
                    (_canonical(error), now, now, run_id),
                )
            elif row["cancel_requested_at"]:
                connection.execute("UPDATE diff_plan_run_items SET status='cancelled',finished_at=?,updated_at=? WHERE run_id=? AND status='queued'", (now, now, run_id))
                self._finalize(connection, run_id)
            else:
                connection.execute("UPDATE diff_plan_runs SET status='running',updated_at=? WHERE run_id=?", (now, run_id))
                self._finalize(connection, run_id)

    def fail_active_run(
        self, run_id: str, error: dict[str, Any]
    ) -> bool:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM diff_plan_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return False
            connection.execute(
                """
                UPDATE diff_plan_runs
                SET status='failed',errors_json=?,finished_at=?,
                    details_expires_at=?,updated_at=?
                WHERE run_id=?
                """,
                (
                    _canonical([error]),
                    now,
                    _future(self.retention_days),
                    now,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE diff_plan_run_items
                SET status='orchestration_failed',error_json=?,finished_at=?,
                    updated_at=?,lease_token=NULL,lease_expires_at=NULL
                WHERE run_id=? AND status IN ('queued','running')
                """,
                (_canonical(error), now, now, run_id),
            )
            return True

    def runnable_run_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT r.run_id
                FROM diff_plan_runs r
                JOIN diff_plan_run_items i ON i.run_id=r.run_id
                WHERE r.status='running' AND r.cancel_requested_at IS NULL
                  AND i.status='queued'
                ORDER BY r.created_at, r.run_id
                """
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def claim_item(self, run_id: str | None = None) -> dict[str, Any] | None:
        now = _now()
        lease_expires = (
            datetime.now(timezone.utc) + timedelta(seconds=ITEM_LEASE_SECONDS)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.*,r.source_endpoint_id,r.source_revision,r.target_revisions_json
                FROM diff_plan_run_items i
                JOIN diff_plan_runs r ON r.run_id=i.run_id
                WHERE i.status='queued' AND r.status='running'
                  AND r.cancel_requested_at IS NULL
                  AND (? IS NULL OR i.run_id=?)
                ORDER BY r.created_at,i.ordinal
                LIMIT 1
                """,
                (run_id, run_id),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(24)
            updated = connection.execute(
                """
                UPDATE diff_plan_run_items
                SET status='running', lease_token=?, lease_expires_at=?,
                    attempt_count=attempt_count+1,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE item_id=? AND status='queued'
                """,
                (token, lease_expires, now, now, row["item_id"]),
            )
            if not updated.rowcount:
                return None
            result = dict(row)
            result["lease_token"] = token
            result["lease_expires_at"] = lease_expires
            return result

    def renew_lease(self, item_id: str, lease_token: str) -> bool:
        now = _now()
        lease_expires = (
            datetime.now(timezone.utc) + timedelta(seconds=ITEM_LEASE_SECONDS)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE diff_plan_run_items
                SET lease_expires_at=?, updated_at=?
                WHERE item_id=? AND status='running' AND lease_token=?
                """,
                (lease_expires, now, item_id, lease_token),
            )
            return bool(updated.rowcount)
    def complete_item(
        self,
        item_id: str,
        *,
        lease_token: str,
        status: str,
        diff_status: str | None = None,
        diff_error_count: int = 0,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.*,r.cancel_requested_at
                FROM diff_plan_run_items i
                JOIN diff_plan_runs r ON r.run_id=i.run_id
                WHERE i.item_id=? AND i.status='running' AND i.lease_token=?
                """,
                (item_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            actual = "cancelled" if row["cancel_requested_at"] else status
            updated = connection.execute(
                """
                UPDATE diff_plan_run_items
                SET status=?,diff_status=?,diff_error_count=?,result_ref=?,result_path=?,
                    error_json=?,finished_at=?,updated_at=?,lease_token=NULL,
                    lease_expires_at=NULL
                WHERE item_id=? AND status='running' AND lease_token=?
                """,
                (
                    actual,
                    diff_status if actual != "cancelled" else None,
                    diff_error_count if actual != "cancelled" else 0,
                    result.get("result_ref") if result and actual != "cancelled" else None,
                    result.get("result_path") if result and actual != "cancelled" else None,
                    _canonical(error) if error and actual != "cancelled" else None,
                    now,
                    now,
                    item_id,
                    lease_token,
                ),
            )
            if not updated.rowcount:
                return False
            self._finalize(connection, row["run_id"])
            return actual == status

    def recover_expired_leases(self) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """
                SELECT item_id,run_id,recovery_count
                FROM diff_plan_run_items
                WHERE status='running'
                  AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                """,
                (now,),
            ).fetchall()
            touched_runs: set[str] = set()
            for row in expired:
                touched_runs.add(str(row["run_id"]))
                if int(row["recovery_count"] or 0) < 1:
                    connection.execute(
                        """
                        UPDATE diff_plan_run_items
                        SET status='queued',lease_token=NULL,lease_expires_at=NULL,
                            recovery_count=recovery_count+1,updated_at=?
                        WHERE item_id=? AND status='running'
                        """,
                        (now, row["item_id"]),
                    )
                else:
                    error = _canonical({
                        "code": "DIFF_PLAN_ITEM_RECOVERY_EXHAUSTED",
                        "message": "单工作簿执行恢复次数已用尽",
                        "retryable": True,
                    })
                    connection.execute(
                        """
                        UPDATE diff_plan_run_items
                        SET status='orchestration_failed',error_json=?,finished_at=?,
                            updated_at=?,lease_token=NULL,lease_expires_at=NULL
                        WHERE item_id=? AND status='running'
                        """,
                        (error, now, now, row["item_id"]),
                    )
            for run_id in touched_runs:
                self._finalize(connection, run_id)
    def _finalize(self, connection: sqlite3.Connection, run_id: str) -> None:
        run = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (run_id,)).fetchone()
        pending = connection.execute("SELECT COUNT(*) FROM diff_plan_run_items WHERE run_id=? AND status NOT IN (%s)" % ",".join("?" * len(TERMINAL_ITEM_STATUSES)), (run_id, *TERMINAL_ITEM_STATUSES)).fetchone()[0]
        if pending:
            return
        rows = connection.execute("SELECT status FROM diff_plan_run_items WHERE run_id=?", (run_id,)).fetchall()
        statuses = {row["status"] for row in rows}
        if run["cancel_requested_at"]:
            status = "cancelled"
        elif statuses & {"read_failed", "business_failed", "orchestration_failed"}:
            status = "completed_with_failures"
        else:
            status = "completed"
        now = _now()
        connection.execute("UPDATE diff_plan_runs SET status=?,finished_at=?,details_expires_at=?,updated_at=? WHERE run_id=?", (status, now, _future(self.retention_days), now, run_id))

    def cancel(self, run_id: UUID | str, request_id: UUID) -> DiffPlanRunPayload:
        now = _now()
        request_hash = _hash({"run_id": str(run_id), "command": "cancel"})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute("SELECT request_hash,run_id FROM diff_plan_run_commands WHERE request_id=?", (str(request_id),)).fetchone()
            if replay:
                if replay["request_hash"] != request_hash:
                    raise DiffPlanError("DIFF_PLAN_RUN_IDEMPOTENCY_CONFLICT", "相同 request_id 已用于不同运行命令", status_code=409)
                row = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (replay["run_id"],)).fetchone()
                return self._payload(connection, row)
            row = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (str(run_id),)).fetchone()
            if row is None:
                raise DiffPlanError("DIFF_PLAN_RUN_NOT_FOUND", "计划运行不存在", status_code=404)
            if row["status"] in TERMINAL_RUN_STATUSES:
                raise DiffPlanError("DIFF_PLAN_RUN_NOT_CANCELLABLE", "终态运行不能取消", status_code=409)
            connection.execute("INSERT INTO diff_plan_run_commands VALUES (?,?,?,?,?)", (str(request_id), "cancel", request_hash, str(run_id), now))
            connection.execute("UPDATE diff_plan_runs SET status='cancelling',cancel_requested_at=?,updated_at=? WHERE run_id=?", (now, now, str(run_id)))
            connection.execute("UPDATE diff_plan_run_items SET status='cancelled',finished_at=?,updated_at=? WHERE run_id=? AND status='queued'", (now, now, str(run_id)))
            self._finalize(connection, str(run_id))
            updated = connection.execute("SELECT * FROM diff_plan_runs WHERE run_id=?", (str(run_id),)).fetchone()
            return self._payload(connection, updated)

    def write_result(self, run_id: str, item_id: str, content: bytes) -> dict[str, Any]:
        directory = self.results_directory / run_id
        directory.mkdir(parents=True, exist_ok=True)
        relative = Path(run_id) / f"{item_id}.json.gz"
        final = self.results_directory / relative
        temporary = final.with_suffix(".json.gz.tmp")
        compressed = gzip.compress(content, mtime=0)
        with temporary.open("wb") as handle:
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        return {"result_ref": "m4r_" + secrets.token_urlsafe(16), "result_path": relative.as_posix()}

    def remove_result(self, relative_path: str | None) -> None:
        if not relative_path:
            return
        path = (self.results_directory / relative_path).resolve()
        if self.results_directory.resolve() not in path.parents:
            return
        path.unlink(missing_ok=True)

    def cleanup_expired_results(self, *, now: str | None = None) -> dict[str, int]:
        cutoff = now or _now()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT i.item_id,i.result_path FROM diff_plan_run_items i
                   JOIN diff_plan_runs r ON r.run_id=i.run_id
                   WHERE r.status IN ('completed','completed_with_failures','cancelled','failed')
                     AND r.details_expires_at IS NOT NULL AND r.details_expires_at<=?
                     AND i.result_path IS NOT NULL""",
                (cutoff,),
            ).fetchall()

        removed_count = 0
        removed_bytes = 0
        cleared_item_ids: list[str] = []
        root = self.results_directory.resolve()
        for row in rows:
            path = (self.results_directory / row["result_path"]).resolve()
            if root not in path.parents:
                continue
            try:
                size = path.stat().st_size if path.is_file() else 0
                path.unlink(missing_ok=True)
            except OSError:
                continue
            removed_count += 1 if size else 0
            removed_bytes += size
            cleared_item_ids.append(row["item_id"])

        if cleared_item_ids:
            placeholders = ",".join("?" for _ in cleared_item_ids)
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE diff_plan_run_items SET result_path=NULL WHERE item_id IN ({placeholders})",
                    cleared_item_ids,
                )
        return {
            "expired_result_count": len(rows),
            "removed_file_count": removed_count,
            "removed_size_bytes": removed_bytes,
        }

    def load_result(self, result_ref: str) -> tuple[bytes, str]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT i.result_path,r.details_expires_at FROM diff_plan_run_items i
                   JOIN diff_plan_runs r ON r.run_id=i.run_id WHERE i.result_ref=?""", (result_ref,)
            ).fetchone()
        if row is None:
            raise DiffPlanError("DIFF_PLAN_RESULT_NOT_FOUND", "运行明细不存在", status_code=404)
        if row["details_expires_at"] and row["details_expires_at"] <= _now():
            raise DiffPlanError("DIFF_PLAN_RESULT_EXPIRED", "运行明细已过期，矩阵摘要仍可查看", status_code=410)
        if not row["result_path"]:
            raise DiffPlanError("DIFF_PLAN_RESULT_EXPIRED", "运行明细已过期，矩阵摘要仍可查看", status_code=410)
        path = (self.results_directory / row["result_path"]).resolve()
        if self.results_directory.resolve() not in path.parents or not path.is_file():
            raise DiffPlanError("DIFF_PLAN_RESULT_NOT_FOUND", "运行明细不存在", status_code=404)
        try:
            content = gzip.decompress(path.read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise DiffPlanError("DIFF_PLAN_RESULT_CORRUPT", "运行明细不可读取", status_code=500) from exc
        return content, hashlib.sha256(content).hexdigest()

    def recover(self) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE diff_plan_runs SET status='queued',updated_at=? WHERE status='preparing'",
                (now,),
            )
            cancelling = connection.execute(
                "SELECT run_id FROM diff_plan_runs WHERE status='cancelling'"
            ).fetchall()
            for row in cancelling:
                connection.execute(
                    """
                    UPDATE diff_plan_run_items
                    SET status='cancelled',finished_at=?,updated_at=?
                    WHERE run_id=? AND status='queued'
                    """,
                    (now, now, row["run_id"]),
                )
                self._finalize(connection, row["run_id"])
            active = connection.execute(
                "SELECT run_id FROM diff_plan_runs WHERE status='running'"
            ).fetchall()
            for row in active:
                self._finalize(connection, row["run_id"])
        self.recover_expired_leases()
