"""SQLite 元数据与独立 gzip 结果文件组成的 M2 批量任务存储。"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from typing import Any, Iterable
from uuid import UUID, uuid4

from app.schemas.batch import (
    BatchCandidatePayload,
    BatchEndpointPayload,
    BatchItemPayload,
    BatchOrchestrationErrorPayload,
    BatchProgressPayload,
    BatchResultSummaryPayload,
    BatchTaskDeleteResultPayload,
    BatchTaskEventPayload,
    BatchTaskListPayload,
    BatchTaskManagementPayload,
    BatchTaskSummaryPayload,
    BatchTaskPayload,
)


TERMINAL_TASK_STATUSES = {
    "completed",
    "completed_with_failures",
    "cancelled",
    "failed",
}
TERMINAL_ITEM_STATUSES = {
    "succeeded",
    "business_failed",
    "orchestration_failed",
    "skipped",
    "cancelled",
}
ACTIVE_ITEM_STATUSES = {"queued", "running"}
RETENTION_DAYS = 30
TOMBSTONE_DAYS = 7
EVENT_RETENTION_DAYS = 90
LEASE_SECONDS = 60


class BatchDiffError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class BatchStore:
    def __init__(
        self,
        state_directory: Path,
        *,
        event_retention_days: int = EVENT_RETENTION_DAYS,
    ):
        self.state_directory = Path(state_directory)
        self.database_path = self.state_directory / "batch.sqlite3"
        self.results_directory = self.state_directory / "results"
        self.event_retention_days = max(1, int(event_retention_days))
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.state_directory.mkdir(parents=True, exist_ok=True)
            self.results_directory.mkdir(parents=True, exist_ok=True)
            with self._connect_raw() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        owner_scope TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        retry_of_task_id TEXT,
                        retry_selection_json TEXT,
                        status TEXT NOT NULL,
                        source_endpoint_id TEXT NOT NULL,
                        source_revision INTEGER NOT NULL,
                        target_endpoint_id TEXT NOT NULL,
                        target_revision INTEGER NOT NULL,
                        candidate_scope TEXT NOT NULL,
                        candidate_status TEXT NOT NULL,
                        manifest_sha256 TEXT,
                        errors_json TEXT NOT NULL,
                        cancel_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        preparation_started_at TEXT,
                        prepared_at TEXT,
                        started_at TEXT,
                        cancel_requested_at TEXT,
                        finished_at TEXT,
                        expires_at TEXT,
                        UNIQUE(owner_scope, request_id)
                    );
                    CREATE TABLE IF NOT EXISTS items (
                        item_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        retry_of_item_id TEXT,
                        ordinal INTEGER NOT NULL,
                        candidate_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        diff_status TEXT,
                        diff_error_count INTEGER,
                        result_ref TEXT UNIQUE,
                        result_sha256 TEXT,
                        result_size_bytes INTEGER,
                        result_path TEXT,
                        result_expires_at TEXT,
                        orchestration_error_json TEXT,
                        reason_code TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        recovery_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        updated_at TEXT NOT NULL,
                        lease_token TEXT,
                        lease_expires_at TEXT,
                        UNIQUE(task_id, ordinal)
                    );
                    CREATE INDEX IF NOT EXISTS idx_items_claim
                        ON items(status, lease_expires_at, task_id, ordinal);
                    CREATE INDEX IF NOT EXISTS idx_tasks_schedule
                        ON tasks(status, created_at);
                    CREATE INDEX IF NOT EXISTS idx_tasks_history
                        ON tasks(owner_scope, created_at DESC, task_id DESC);
                    CREATE TABLE IF NOT EXISTS commands (
                        owner_scope TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        command_type TEXT NOT NULL,
                        target_task_id TEXT NOT NULL,
                        command_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_scope, request_id)
                    );
                    CREATE TABLE IF NOT EXISTS tombstones (
                        resource_type TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        PRIMARY KEY(resource_type, resource_id)
                    );
                    CREATE TABLE IF NOT EXISTS task_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL,
                        details_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_task_events_history
                        ON task_events(task_id, created_at, event_id);
                    CREATE INDEX IF NOT EXISTS idx_task_events_retention
                        ON task_events(created_at);
                    CREATE TABLE IF NOT EXISTS manual_deletions (
                        task_id TEXT PRIMARY KEY,
                        owner_scope TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        reason TEXT,
                        deleted_at TEXT NOT NULL,
                        result_count INTEGER NOT NULL,
                        result_size_bytes INTEGER NOT NULL,
                        tombstone_expires_at TEXT NOT NULL,
                        UNIQUE(owner_scope, request_id)
                    );
                    """
                )
            self._initialized = True

    def _connect_raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect_raw()

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        event_type: str,
        message: str,
        created_at: str,
        level: str = "info",
        details: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events (
                task_id, event_type, level, message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                event_type,
                level,
                message[:512],
                canonical_json(details or {}),
                created_at,
            ),
        )

    def _ensure_baseline_events(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
    ) -> None:
        created_exists = connection.execute(
            """
            SELECT 1 FROM task_events
            WHERE task_id=? AND event_type='task.created'
            LIMIT 1
            """,
            (task["task_id"],),
        ).fetchone()
        if created_exists is None:
            self._append_event(
                connection,
                task_id=task["task_id"],
                event_type="task.created",
                message="任务已创建",
                created_at=task["created_at"],
                details={"status": "queued"},
            )
        if task["finished_at"] and task["status"] in TERMINAL_TASK_STATUSES:
            terminal_type = f"status.{task['status']}"
            terminal_exists = connection.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id=? AND event_type=?
                LIMIT 1
                """,
                (task["task_id"], terminal_type),
            ).fetchone()
            if terminal_exists is None:
                level = "error" if task["status"] == "failed" else "info"
                self._append_event(
                    connection,
                    task_id=task["task_id"],
                    event_type=terminal_type,
                    message={
                        "completed": "任务已完成",
                        "completed_with_failures": "任务已完成，部分工作簿失败",
                        "cancelled": "任务已取消",
                        "failed": "任务失败",
                    }[task["status"]],
                    created_at=task["finished_at"],
                    level=level,
                    details={"status": task["status"]},
                )

    def create_task(
        self,
        *,
        request_id: UUID,
        request_hash: str,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        candidate_scope: str = "all",
        retry_of_task_id: UUID | None = None,
        retry_selection: list[dict[str, Any]] | None = None,
        owner_scope: str = "local",
    ) -> tuple[str, bool]:
        now = isoformat()
        with self._connect() as connection:
            self._begin(connection)
            existing = connection.execute(
                "SELECT task_id, request_hash FROM tasks WHERE owner_scope=? AND request_id=?",
                (owner_scope, str(request_id)),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise BatchDiffError(
                        "BATCH_IDEMPOTENCY_CONFLICT",
                        "request_id 已用于不同的批量请求",
                        status_code=409,
                    )
                return str(existing["task_id"]), False
            request_tombstone = connection.execute(
                "SELECT data_json FROM tombstones WHERE resource_type='request' AND resource_id=?",
                (f"{owner_scope}:{request_id}",),
            ).fetchone()
            if request_tombstone is not None:
                data = json.loads(request_tombstone["data_json"])
                if data.get("request_hash") != request_hash:
                    raise BatchDiffError(
                        "BATCH_IDEMPOTENCY_CONFLICT",
                        "request_id 已用于不同的批量请求",
                        status_code=409,
                    )
                raise BatchDiffError(
                    "BATCH_TASK_EXPIRED",
                    "该幂等请求对应的批量任务已过期",
                    status_code=410,
                )
            task_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, owner_scope, request_id, request_hash,
                    retry_of_task_id, retry_selection_json, status,
                    source_endpoint_id, source_revision,
                    target_endpoint_id, target_revision,
                    candidate_scope, candidate_status, manifest_sha256,
                    errors_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, 'pending', NULL, '[]', ?, ?)
                """,
                (
                    task_id,
                    owner_scope,
                    str(request_id),
                    request_hash,
                    str(retry_of_task_id) if retry_of_task_id else None,
                    canonical_json(retry_selection) if retry_selection else None,
                    source.endpoint_id,
                    source.revision,
                    target.endpoint_id,
                    target.revision,
                    candidate_scope,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                task_id=task_id,
                event_type="task.created",
                message="任务已创建",
                created_at=now,
                details={
                    "status": "queued",
                    "retry_of_task_id": (
                        str(retry_of_task_id) if retry_of_task_id else None
                    ),
                },
            )
            if retry_of_task_id is not None:
                self._append_event(
                    connection,
                    task_id=str(retry_of_task_id),
                    event_type="command.retry-created",
                    message="已创建重试子任务",
                    created_at=now,
                    details={"child_task_id": task_id},
                )
            return task_id, True

    def _task_row(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        deletion = connection.execute(
            "SELECT 1 FROM manual_deletions WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if deletion is not None:
            raise BatchDiffError(
                "BATCH_TASK_DELETED",
                "批量任务已删除",
                status_code=410,
            )
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is not None:
            if (
                row["status"] in TERMINAL_TASK_STATUSES
                and row["expires_at"]
                and row["expires_at"] <= isoformat()
            ):
                raise BatchDiffError(
                    "BATCH_TASK_EXPIRED",
                    "批量任务已过期",
                    status_code=410,
                )
            return row
        tombstone = connection.execute(
            "SELECT 1 FROM tombstones WHERE resource_type='task' AND resource_id=?",
            (task_id,),
        ).fetchone()
        if tombstone is not None:
            raise BatchDiffError(
                "BATCH_TASK_EXPIRED",
                "批量任务已过期",
                status_code=410,
            )
        raise BatchDiffError(
            "BATCH_TASK_NOT_FOUND",
            "批量任务不存在",
            status_code=404,
        )

    def get_task_record(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._task_row(connection, task_id)
            data = dict(row)
            data["retry_selection"] = (
                json.loads(data["retry_selection_json"])
                if data.get("retry_selection_json")
                else None
            )
            return data

    def _progress(
        self,
        task: sqlite3.Row,
        items: list[sqlite3.Row],
    ) -> BatchProgressPayload:
        counts = {
            status: sum(item["status"] == status for item in items)
            for status in (
                "queued",
                "running",
                "succeeded",
                "business_failed",
                "orchestration_failed",
                "skipped",
                "cancelled",
            )
        }
        terminal = sum(
            counts[status]
            for status in (
                "succeeded",
                "business_failed",
                "orchestration_failed",
                "skipped",
                "cancelled",
            )
        )
        if task["candidate_status"] != "ready":
            return BatchProgressPayload(total_items=None, processed_items=0, ratio=None)
        total = len(items)
        ratio = 1.0 if total == 0 else terminal / total
        return BatchProgressPayload(
            total_items=total,
            queued_items=counts["queued"],
            running_items=counts["running"],
            succeeded_items=counts["succeeded"],
            business_failed_items=counts["business_failed"],
            orchestration_failed_items=counts["orchestration_failed"],
            skipped_items=counts["skipped"],
            cancelled_items=counts["cancelled"],
            processed_items=terminal,
            ratio=ratio,
        )

    @staticmethod
    def _item_payload(row: sqlite3.Row) -> BatchItemPayload:
        error = (
            json.loads(row["orchestration_error_json"])
            if row["orchestration_error_json"]
            else None
        )
        return BatchItemPayload.model_validate(
            {
                "item_id": row["item_id"],
                "retry_of_item_id": row["retry_of_item_id"],
                "ordinal": row["ordinal"],
                "candidate": json.loads(row["candidate_json"]),
                "status": row["status"],
                "diff_status": row["diff_status"],
                "diff_error_count": row["diff_error_count"],
                "result_ref": row["result_ref"],
                "result_sha256": row["result_sha256"],
                "result_size_bytes": row["result_size_bytes"],
                "result_expires_at": row["result_expires_at"],
                "orchestration_error": error,
                "reason_code": row["reason_code"],
                "attempt_count": row["attempt_count"],
                "recovery_count": row["recovery_count"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "updated_at": row["updated_at"],
            }
        )

    def get_task(self, task_id: str) -> BatchTaskPayload:
        with self._connect() as connection:
            connection.execute("BEGIN")
            task = self._task_row(connection, task_id)
            items = list(
                connection.execute(
                    "SELECT * FROM items WHERE task_id=? ORDER BY ordinal",
                    (task_id,),
                ).fetchall()
            )
            return BatchTaskPayload.model_validate(
                {
                    "task_id": task["task_id"],
                    "request_id": task["request_id"],
                    "retry_of_task_id": task["retry_of_task_id"],
                    "status": task["status"],
                    "source": {
                        "endpoint_id": task["source_endpoint_id"],
                        "revision": task["source_revision"],
                    },
                    "target": {
                        "endpoint_id": task["target_endpoint_id"],
                        "revision": task["target_revision"],
                    },
                    "candidate_source": {
                        "scope": task["candidate_scope"],
                        "status": task["candidate_status"],
                        "manifest_sha256": task["manifest_sha256"],
                    },
                    "progress": self._progress(task, items),
                    "items": [self._item_payload(item) for item in items],
                    "errors": json.loads(task["errors_json"]),
                    "created_at": task["created_at"],
                    "updated_at": task["updated_at"],
                    "preparation_started_at": task["preparation_started_at"],
                    "prepared_at": task["prepared_at"],
                    "started_at": task["started_at"],
                    "cancel_requested_at": task["cancel_requested_at"],
                    "finished_at": task["finished_at"],
                    "expires_at": task["expires_at"],
                }
            )

    def get_task_management(self, task_id: str) -> BatchTaskManagementPayload:
        with self._connect() as connection:
            task = self._task_row(connection, task_id)
            self._ensure_baseline_events(connection, task)
            result_row = connection.execute(
                """
                SELECT COUNT(*) AS result_count,
                       COALESCE(SUM(result_size_bytes), 0) AS result_size_bytes
                FROM items
                WHERE task_id=? AND result_ref IS NOT NULL
                """,
                (task_id,),
            ).fetchone()
            child_rows = connection.execute(
                """
                SELECT t.task_id
                FROM tasks t
                WHERE t.retry_of_task_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM manual_deletions d WHERE d.task_id=t.task_id
                  )
                ORDER BY t.created_at, t.task_id
                """,
                (task_id,),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id=?
                ORDER BY created_at, event_id
                """,
                (task_id,),
            ).fetchall()
            return BatchTaskManagementPayload(
                task_id=task["task_id"],
                status=task["status"],
                retry_of_task_id=task["retry_of_task_id"],
                retry_child_task_ids=[row["task_id"] for row in child_rows],
                results=BatchResultSummaryPayload(
                    count=int(result_row["result_count"]),
                    size_bytes=int(result_row["result_size_bytes"]),
                    expires_at=task["expires_at"],
                ),
                events=[
                    BatchTaskEventPayload(
                        event_id=row["event_id"],
                        event_type=row["event_type"],
                        level=row["level"],
                        message=row["message"],
                        details=json.loads(row["details_json"]),
                        created_at=row["created_at"],
                    )
                    for row in event_rows
                ],
                can_delete=task["status"] in TERMINAL_TASK_STATUSES,
            )

    @staticmethod
    def _encode_list_cursor(created_at: str, task_id: str) -> str:
        raw = canonical_json({"created_at": created_at, "task_id": task_id}).encode(
            "utf-8"
        )
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_list_cursor(cursor: str) -> tuple[str, str]:
        try:
            allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            if not cursor or any(character not in allowed for character in cursor):
                raise ValueError
            padded = cursor + "=" * (-len(cursor) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if not isinstance(data, dict) or set(data) != {"created_at", "task_id"}:
                raise ValueError
            task_id = str(UUID(str(data["task_id"])))
            parsed = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            return isoformat(parsed.astimezone(timezone.utc)), task_id
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            raise BatchDiffError(
                "BATCH_INVALID_CURSOR",
                "历史任务游标无效",
                status_code=400,
            ) from None

    @staticmethod
    def _summary_progress(row: sqlite3.Row) -> BatchProgressPayload:
        if row["candidate_status"] != "ready":
            return BatchProgressPayload(total_items=None, processed_items=0, ratio=None)
        counts = {
            name: int(row[name] or 0)
            for name in (
                "queued_items",
                "running_items",
                "succeeded_items",
                "business_failed_items",
                "orchestration_failed_items",
                "skipped_items",
                "cancelled_items",
            )
        }
        processed = sum(
            counts[name]
            for name in (
                "succeeded_items",
                "business_failed_items",
                "orchestration_failed_items",
                "skipped_items",
                "cancelled_items",
            )
        )
        total = int(row["item_count"] or 0)
        return BatchProgressPayload(
            total_items=total,
            processed_items=processed,
            ratio=1.0 if total == 0 else processed / total,
            **counts,
        )

    def list_tasks(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        statuses: Iterable[str] | None = None,
        query: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        owner_scope: str = "local",
    ) -> BatchTaskListPayload:
        as_of = isoformat()
        where = [
            "t.owner_scope=?",
            "(t.status NOT IN ('completed','completed_with_failures','cancelled','failed') "
            "OR t.expires_at>?)",
            "NOT EXISTS (SELECT 1 FROM manual_deletions d WHERE d.task_id=t.task_id)",
        ]
        parameters: list[Any] = [owner_scope, as_of]
        selected_statuses = sorted(set(statuses or []))
        if selected_statuses:
            placeholders = ",".join("?" for _ in selected_statuses)
            where.append(f"t.status IN ({placeholders})")
            parameters.extend(selected_statuses)
        normalized_query = (query or "").strip().casefold()
        if normalized_query:
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            where.append(
                "(LOWER(t.task_id) LIKE ? ESCAPE '\\' "
                "OR LOWER(t.source_endpoint_id) LIKE ? ESCAPE '\\' "
                "OR LOWER(t.target_endpoint_id) LIKE ? ESCAPE '\\')"
            )
            parameters.extend(
                [escaped + "%", "%" + escaped + "%", "%" + escaped + "%"]
            )
        if created_from:
            where.append("t.created_at>=?")
            parameters.append(created_from)
        if created_to:
            where.append("t.created_at<=?")
            parameters.append(created_to)
        if cursor:
            cursor_created_at, cursor_task_id = self._decode_list_cursor(cursor)
            where.append("(t.created_at<? OR (t.created_at=? AND t.task_id<?))")
            parameters.extend([cursor_created_at, cursor_created_at, cursor_task_id])

        statement = f"""
            SELECT t.*,
                COUNT(i.item_id) AS item_count,
                SUM(CASE WHEN i.status='queued' THEN 1 ELSE 0 END) AS queued_items,
                SUM(CASE WHEN i.status='running' THEN 1 ELSE 0 END) AS running_items,
                SUM(CASE WHEN i.status='succeeded' THEN 1 ELSE 0 END) AS succeeded_items,
                SUM(CASE WHEN i.status='business_failed' THEN 1 ELSE 0 END) AS business_failed_items,
                SUM(CASE WHEN i.status='orchestration_failed' THEN 1 ELSE 0 END) AS orchestration_failed_items,
                SUM(CASE WHEN i.status='skipped' THEN 1 ELSE 0 END) AS skipped_items,
                SUM(CASE WHEN i.status='cancelled' THEN 1 ELSE 0 END) AS cancelled_items
            FROM tasks t
            LEFT JOIN items i ON i.task_id=t.task_id
            WHERE {' AND '.join(where)}
            GROUP BY t.task_id
            ORDER BY t.created_at DESC, t.task_id DESC
            LIMIT ?
        """
        parameters.append(limit + 1)
        with self._connect() as connection:
            rows = list(connection.execute(statement, parameters).fetchall())

        has_more = len(rows) > limit
        rows = rows[:limit]
        summaries: list[BatchTaskSummaryPayload] = []
        for row in rows:
            summaries.append(
                BatchTaskSummaryPayload.model_validate(
                    {
                        "task_id": row["task_id"],
                        "retry_of_task_id": row["retry_of_task_id"],
                        "status": row["status"],
                        "source": {
                            "endpoint_id": row["source_endpoint_id"],
                            "revision": row["source_revision"],
                        },
                        "target": {
                            "endpoint_id": row["target_endpoint_id"],
                            "revision": row["target_revision"],
                        },
                        "progress": self._summary_progress(row),
                        "task_error_count": len(json.loads(row["errors_json"])),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "finished_at": row["finished_at"],
                        "expires_at": row["expires_at"],
                    }
                )
            )
        next_cursor = None
        if has_more and rows:
            next_cursor = self._encode_list_cursor(
                rows[-1]["created_at"], rows[-1]["task_id"]
            )
        return BatchTaskListPayload(
            items=summaries,
            next_cursor=next_cursor,
            has_more=has_more,
            as_of=as_of,
        )

    def claim_preparation(self) -> dict[str, Any] | None:
        now = isoformat()
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE tasks
                SET status='preparing', candidate_status='preparing',
                    preparation_started_at=COALESCE(preparation_started_at, ?),
                    updated_at=?
                WHERE task_id=? AND status='queued'
                """,
                (now, now, row["task_id"]),
            )
            if updated.rowcount != 1:
                return None
            self._append_event(
                connection,
                task_id=row["task_id"],
                event_type="status.preparing",
                message="正在准备候选清单",
                created_at=now,
                details={"status": "preparing"},
            )
            data = dict(row)
            data["status"] = "preparing"
            data["candidate_status"] = "preparing"
            data["retry_selection"] = (
                json.loads(data["retry_selection_json"])
                if data.get("retry_selection_json")
                else None
            )
            return data

    @staticmethod
    def _reason_for_candidate(status: str) -> str | None:
        return {
            "left_only": "BATCH_CANDIDATE_LEFT_ONLY",
            "right_only": "BATCH_CANDIDATE_RIGHT_ONLY",
            "read_error": "BATCH_CANDIDATE_READ_ERROR",
        }.get(status)

    def complete_preparation(
        self,
        task_id: str,
        prepared: Iterable[
            tuple[
                BatchCandidatePayload,
                str | None,
                BatchOrchestrationErrorPayload | None,
            ]
        ],
    ) -> None:
        candidates = list(prepared)
        now = isoformat()
        manifest = json_hash(
            [candidate.model_dump(mode="json") for candidate, _, _ in candidates]
        )
        with self._connect() as connection:
            self._begin(connection)
            task = self._task_row(connection, task_id)
            if task["status"] in TERMINAL_TASK_STATUSES:
                return
            if task["status"] == "cancelling" or task["cancel_requested_at"]:
                self._finish_cancelled(connection, task_id, now)
                return
            if task["status"] != "preparing":
                return
            for ordinal, (candidate, retry_of_item_id, initial_error) in enumerate(candidates):
                item_id = str(uuid4())
                if initial_error is not None:
                    status = "orchestration_failed"
                    reason = None
                    finished_at = now
                    error_json = canonical_json(initial_error.model_dump(mode="json"))
                elif candidate.status == "modified":
                    status = "queued"
                    reason = None
                    finished_at = None
                    error_json = None
                else:
                    status = "skipped"
                    reason = self._reason_for_candidate(candidate.status)
                    finished_at = now
                    error_json = None
                connection.execute(
                    """
                    INSERT INTO items (
                        item_id, task_id, retry_of_item_id, ordinal,
                        candidate_json, status, orchestration_error_json,
                        reason_code, attempt_count, recovery_count,
                        created_at, finished_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (
                        item_id,
                        task_id,
                        retry_of_item_id,
                        ordinal,
                        canonical_json(candidate.model_dump(mode="json")),
                        status,
                        error_json,
                        reason,
                        now,
                        finished_at,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE tasks
                SET status='running', candidate_status='ready', manifest_sha256=?,
                    prepared_at=?, updated_at=?
                WHERE task_id=?
                """,
                (manifest, now, now, task_id),
            )
            self._append_event(
                connection,
                task_id=task_id,
                event_type="status.running",
                message="候选清单已冻结，任务开始执行",
                created_at=now,
                details={"status": "running", "candidate_count": len(candidates)},
            )
            self._finalize_if_done(connection, task_id, now)

    def fail_preparation(
        self,
        task_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        now_value = utc_now()
        now = isoformat(now_value)
        expires = isoformat(now_value + timedelta(days=RETENTION_DAYS))
        error = canonical_json(
            [{"code": code, "message": message, "retryable": retryable}]
        )
        with self._connect() as connection:
            self._begin(connection)
            task = self._task_row(connection, task_id)
            if task["status"] in TERMINAL_TASK_STATUSES:
                return
            if task["cancel_requested_at"]:
                self._finish_cancelled(connection, task_id, now)
                return
            connection.execute(
                """
                UPDATE tasks
                SET status='failed', candidate_status='failed', errors_json=?,
                    finished_at=?, expires_at=?, updated_at=?
                WHERE task_id=?
                """,
                (error, now, expires, now, task_id),
            )
            self._append_event(
                connection,
                task_id=task_id,
                event_type="task.error",
                message=message,
                created_at=now,
                level="error",
                details={"code": code, "retryable": retryable},
            )
            self._append_event(
                connection,
                task_id=task_id,
                event_type="status.failed",
                message="任务失败",
                created_at=now,
                level="error",
                details={"status": "failed"},
            )

    def claim_next_item(self) -> dict[str, Any] | None:
        now_value = utc_now()
        now = isoformat(now_value)
        lease_expires = isoformat(now_value + timedelta(seconds=LEASE_SECONDS))
        with self._connect() as connection:
            self._begin(connection)
            live_running = connection.execute(
                "SELECT COUNT(*) FROM items WHERE status='running' AND lease_expires_at>?",
                (now,),
            ).fetchone()[0]
            if live_running >= 2:
                return None
            row = connection.execute(
                """
                SELECT i.*, t.source_endpoint_id, t.source_revision,
                       t.target_endpoint_id, t.target_revision
                FROM items i
                JOIN tasks t ON t.task_id=i.task_id
                WHERE i.status='queued' AND t.status='running'
                  AND t.cancel_requested_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM items active
                    WHERE active.task_id=i.task_id AND active.status='running'
                  )
                ORDER BY t.created_at, i.ordinal
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(24)
            updated = connection.execute(
                """
                UPDATE items
                SET status='running', lease_token=?, lease_expires_at=?,
                    attempt_count=attempt_count+1,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE item_id=? AND status='queued'
                """,
                (token, lease_expires, now, now, row["item_id"]),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                "UPDATE tasks SET started_at=COALESCE(started_at, ?), updated_at=? WHERE task_id=?",
                (now, now, row["task_id"]),
            )
            data = dict(row)
            data["lease_token"] = token
            data["lease_expires_at"] = lease_expires
            data["candidate"] = json.loads(data["candidate_json"])
            return data

    def renew_lease(self, item_id: str, lease_token: str) -> bool:
        now_value = utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE items SET lease_expires_at=?, updated_at=?
                WHERE item_id=? AND status='running' AND lease_token=?
                """,
                (
                    isoformat(now_value + timedelta(seconds=LEASE_SECONDS)),
                    isoformat(now_value),
                    item_id,
                    lease_token,
                ),
            )
            return updated.rowcount == 1

    def write_result_blob(
        self,
        task_id: str,
        item_id: str,
        content: bytes,
    ) -> dict[str, Any]:
        self.initialize()
        result_directory = self.results_directory / task_id
        result_directory.mkdir(parents=True, exist_ok=True)
        relative_path = Path("results") / task_id / f"{item_id}.json.gz"
        final_path = self.state_directory / relative_path
        temporary_path = final_path.with_suffix(".json.gz.tmp")
        compressed = gzip.compress(content, mtime=0)
        with temporary_path.open("wb") as handle:
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
        return {
            "result_ref": "m2r_" + secrets.token_urlsafe(16),
            "result_sha256": hashlib.sha256(content).hexdigest(),
            "result_size_bytes": len(content),
            "result_path": relative_path.as_posix(),
        }

    def remove_result_blob(self, relative_path: str | None) -> None:
        if not relative_path:
            return
        path = (self.state_directory / relative_path).resolve()
        root = self.results_directory.resolve()
        if root not in path.parents:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def complete_item_result(
        self,
        *,
        item_id: str,
        lease_token: str,
        diff_status: str,
        diff_error_count: int,
        result: dict[str, Any],
    ) -> bool:
        item_status = (
            "succeeded"
            if diff_status in {"unchanged", "modified"}
            else "business_failed"
        )
        now = isoformat()
        provisional_expires = isoformat(utc_now() + timedelta(days=RETENTION_DAYS))
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT task_id FROM items WHERE item_id=? AND status='running' AND lease_token=?",
                (item_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE items
                SET status=?, diff_status=?, diff_error_count=?,
                    result_ref=?, result_sha256=?, result_size_bytes=?, result_path=?, result_expires_at=?,
                    finished_at=?, updated_at=?, lease_token=NULL, lease_expires_at=NULL
                WHERE item_id=? AND status='running' AND lease_token=?
                """,
                (
                    item_status,
                    diff_status,
                    diff_error_count,
                    result["result_ref"],
                    result["result_sha256"],
                    result["result_size_bytes"],
                    result["result_path"],
                    provisional_expires,
                    now,
                    now,
                    item_id,
                    lease_token,
                ),
            )
            self._finalize_if_done(connection, row["task_id"], now)
            return True

    def fail_item(
        self,
        *,
        item_id: str,
        lease_token: str,
        code: str,
        message: str,
        retryable: bool,
    ) -> bool:
        now = isoformat()
        error = canonical_json(
            {"code": code, "message": message, "retryable": retryable}
        )
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT task_id FROM items WHERE item_id=? AND status='running' AND lease_token=?",
                (item_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE items
                SET status='orchestration_failed', orchestration_error_json=?,
                    finished_at=?, updated_at=?, lease_token=NULL, lease_expires_at=NULL
                WHERE item_id=? AND status='running' AND lease_token=?
                """,
                (error, now, now, item_id, lease_token),
            )
            self._append_event(
                connection,
                task_id=row["task_id"],
                event_type="item.error",
                message=message,
                created_at=now,
                level="error",
                details={
                    "code": code,
                    "item_id": item_id,
                    "retryable": retryable,
                },
            )
            self._finalize_if_done(connection, row["task_id"], now)
            return True

    def _finalize_if_done(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: str,
    ) -> None:
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task is None or task["status"] in TERMINAL_TASK_STATUSES:
            return
        active = connection.execute(
            "SELECT COUNT(*) FROM items WHERE task_id=? AND status IN ('queued','running')",
            (task_id,),
        ).fetchone()[0]
        if active:
            return
        if task["cancel_requested_at"] or task["status"] == "cancelling":
            status = "cancelled"
        else:
            failures = connection.execute(
                """
                SELECT COUNT(*) FROM items
                WHERE task_id=? AND status IN ('business_failed','orchestration_failed')
                """,
                (task_id,),
            ).fetchone()[0]
            status = "completed_with_failures" if failures else "completed"
        now_value = datetime.fromisoformat(now.replace("Z", "+00:00"))
        expires = isoformat(now_value + timedelta(days=RETENTION_DAYS))
        connection.execute(
            """
            UPDATE tasks
            SET status=?, finished_at=?, expires_at=?, updated_at=?
            WHERE task_id=?
            """,
            (status, now, expires, now, task_id),
        )
        connection.execute(
            "UPDATE items SET result_expires_at=? WHERE task_id=? AND result_ref IS NOT NULL",
            (expires, task_id),
        )
        self._append_event(
            connection,
            task_id=task_id,
            event_type=f"status.{status}",
            message=(
                "任务已完成，部分工作簿失败"
                if status == "completed_with_failures"
                else "任务已完成"
            ),
            created_at=now,
            level="warning" if status == "completed_with_failures" else "info",
            details={"status": status},
        )

    def _finish_cancelled(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: str,
    ) -> None:
        now_value = datetime.fromisoformat(now.replace("Z", "+00:00"))
        expires = isoformat(now_value + timedelta(days=RETENTION_DAYS))
        connection.execute(
            """
            UPDATE tasks
            SET status='cancelled', finished_at=?, expires_at=?, updated_at=?
            WHERE task_id=?
            """,
            (now, expires, now, task_id),
        )
        connection.execute(
            "UPDATE items SET result_expires_at=? WHERE task_id=? AND result_ref IS NOT NULL",
            (expires, task_id),
        )
        self._append_event(
            connection,
            task_id=task_id,
            event_type="status.cancelled",
            message="任务已取消",
            created_at=now,
            level="warning",
            details={"status": "cancelled"},
        )

    def cancel_task(
        self,
        *,
        task_id: str,
        request_id: UUID,
        reason: str | None,
        owner_scope: str = "local",
    ) -> None:
        now = isoformat()
        command_hash = json_hash({"task_id": task_id, "reason": reason})
        with self._connect() as connection:
            self._begin(connection)
            existing = connection.execute(
                "SELECT * FROM commands WHERE owner_scope=? AND request_id=?",
                (owner_scope, str(request_id)),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_type"] != "cancel"
                    or existing["target_task_id"] != task_id
                    or existing["command_hash"] != command_hash
                ):
                    raise BatchDiffError(
                        "BATCH_IDEMPOTENCY_CONFLICT",
                        "request_id 已用于不同的批量命令",
                        status_code=409,
                    )
                return
            task = self._task_row(connection, task_id)
            if task["status"] in TERMINAL_TASK_STATUSES:
                raise BatchDiffError(
                    "BATCH_TASK_NOT_CANCELLABLE",
                    "当前批量任务不能取消",
                    status_code=409,
                )
            connection.execute(
                "INSERT INTO commands VALUES (?, ?, 'cancel', ?, ?, ?)",
                (owner_scope, str(request_id), task_id, command_hash, now),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status='cancelling', cancel_requested_at=?, cancel_reason=?, updated_at=?
                WHERE task_id=?
                """,
                (now, reason, now, task_id),
            )
            self._append_event(
                connection,
                task_id=task_id,
                event_type="command.cancel",
                message="已请求取消任务",
                created_at=now,
                level="warning",
                details={"request_id": str(request_id)},
            )
            connection.execute(
                """
                UPDATE items
                SET status='cancelled', reason_code='BATCH_CANCELLED_BEFORE_START',
                    finished_at=?, updated_at=?
                WHERE task_id=? AND status='queued'
                """,
                (now, now, task_id),
            )
            running = connection.execute(
                "SELECT COUNT(*) FROM items WHERE task_id=? AND status='running'",
                (task_id,),
            ).fetchone()[0]
            if not running:
                self._finish_cancelled(connection, task_id, now)

    @staticmethod
    def _delete_result_payload(row: sqlite3.Row) -> BatchTaskDeleteResultPayload:
        return BatchTaskDeleteResultPayload(
            task_id=row["task_id"],
            deleted_at=row["deleted_at"],
            deleted_result_count=row["result_count"],
            deleted_result_size_bytes=row["result_size_bytes"],
            tombstone_expires_at=row["tombstone_expires_at"],
        )

    def delete_task(
        self,
        *,
        task_id: str,
        request_id: UUID,
        reason: str | None,
        owner_scope: str = "local",
    ) -> BatchTaskDeleteResultPayload:
        now_value = utc_now()
        now = isoformat(now_value)
        tombstone_expires = isoformat(now_value + timedelta(days=TOMBSTONE_DAYS))
        command_hash = json_hash({"task_id": task_id, "reason": reason})
        result_paths: list[str] = []
        with self._connect() as connection:
            self._begin(connection)
            existing_command = connection.execute(
                "SELECT * FROM commands WHERE owner_scope=? AND request_id=?",
                (owner_scope, str(request_id)),
            ).fetchone()
            if existing_command is not None:
                if (
                    existing_command["command_type"] != "delete"
                    or existing_command["target_task_id"] != task_id
                    or existing_command["command_hash"] != command_hash
                ):
                    raise BatchDiffError(
                        "BATCH_IDEMPOTENCY_CONFLICT",
                        "request_id 已用于不同的批量命令",
                        status_code=409,
                    )
                deletion = connection.execute(
                    "SELECT * FROM manual_deletions WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if deletion is None:
                    raise BatchDiffError(
                        "BATCH_TASK_DELETED",
                        "批量任务已删除",
                        status_code=410,
                    )
                return self._delete_result_payload(deletion)

            deletion = connection.execute(
                "SELECT 1 FROM manual_deletions WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if deletion is not None:
                raise BatchDiffError(
                    "BATCH_TASK_DELETED",
                    "批量任务已删除",
                    status_code=410,
                )
            task = self._task_row(connection, task_id)
            if task["status"] not in TERMINAL_TASK_STATUSES:
                raise BatchDiffError(
                    "BATCH_TASK_NOT_DELETABLE",
                    "仅终态批量任务可以删除",
                    status_code=409,
                )

            items = connection.execute(
                """
                SELECT result_ref, result_path, result_size_bytes
                FROM items WHERE task_id=?
                """,
                (task_id,),
            ).fetchall()
            result_items = [item for item in items if item["result_ref"]]
            result_count = len(result_items)
            result_size_bytes = sum(
                int(item["result_size_bytes"] or 0) for item in result_items
            )
            result_paths = [
                str(item["result_path"]) for item in result_items if item["result_path"]
            ]

            connection.execute(
                "INSERT INTO commands VALUES (?, ?, 'delete', ?, ?, ?)",
                (owner_scope, str(request_id), task_id, command_hash, now),
            )
            for item in result_items:
                connection.execute(
                    "INSERT OR REPLACE INTO tombstones VALUES ('result', ?, '{}', ?)",
                    (item["result_ref"], tombstone_expires),
                )
            connection.execute(
                "INSERT OR REPLACE INTO tombstones VALUES ('task', ?, '{}', ?)",
                (task_id, tombstone_expires),
            )
            connection.execute(
                "INSERT OR REPLACE INTO tombstones VALUES ('request', ?, ?, ?)",
                (
                    f"{task['owner_scope']}:{task['request_id']}",
                    canonical_json(
                        {
                            "request_hash": task["request_hash"],
                            "task_id": task_id,
                        }
                    ),
                    tombstone_expires,
                ),
            )
            connection.execute(
                """
                INSERT INTO manual_deletions (
                    task_id, owner_scope, request_id, reason, deleted_at,
                    result_count, result_size_bytes, tombstone_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    owner_scope,
                    str(request_id),
                    reason,
                    now,
                    result_count,
                    result_size_bytes,
                    tombstone_expires,
                ),
            )
            self._append_event(
                connection,
                task_id=task_id,
                event_type="command.delete",
                message="任务及其正式结果已删除",
                created_at=now,
                level="warning",
                details={
                    "request_id": str(request_id),
                    "result_count": result_count,
                    "result_size_bytes": result_size_bytes,
                },
            )
            connection.execute(
                "UPDATE tasks SET expires_at=?, updated_at=? WHERE task_id=?",
                (now, now, task_id),
            )
            connection.execute(
                """
                UPDATE items
                SET result_path=NULL, result_expires_at=?, updated_at=?
                WHERE task_id=? AND result_ref IS NOT NULL
                """,
                (now, now, task_id),
            )
            deletion_row = connection.execute(
                "SELECT * FROM manual_deletions WHERE task_id=?",
                (task_id,),
            ).fetchone()

        for relative_path in result_paths:
            self.remove_result_blob(relative_path)
        return self._delete_result_payload(deletion_row)

    def recover(self) -> None:
        self.initialize()
        now = isoformat()
        with self._connect() as connection:
            self._begin(connection)
            preparing = connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE status='preparing' AND prepared_at IS NULL
                """
            ).fetchall()
            connection.execute(
                """
                UPDATE tasks
                SET status='queued', candidate_status='pending', updated_at=?
                WHERE status='preparing' AND prepared_at IS NULL
                """,
                (now,),
            )
            for task in preparing:
                self._append_event(
                    connection,
                    task_id=task["task_id"],
                    event_type="task.recovered",
                    message="服务重启后已恢复候选准备",
                    created_at=now,
                    level="warning",
                    details={"phase": "preparing"},
                )
        self.recover_expired_leases(force=False)
        self._remove_orphan_results()

    def recover_expired_leases(self, *, force: bool = False) -> None:
        now = isoformat()
        with self._connect() as connection:
            self._begin(connection)
            if force:
                rows = connection.execute(
                    "SELECT * FROM items WHERE status='running'"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM items WHERE status='running' AND lease_expires_at<=?",
                    (now,),
                ).fetchall()
            affected_tasks: set[str] = set()
            for row in rows:
                affected_tasks.add(row["task_id"])
                if row["recovery_count"] < 1:
                    connection.execute(
                        """
                        UPDATE items
                        SET status='queued', recovery_count=recovery_count+1,
                            lease_token=NULL, lease_expires_at=NULL, updated_at=?
                        WHERE item_id=? AND status='running'
                        """,
                        (now, row["item_id"]),
                    )
                    self._append_event(
                        connection,
                        task_id=row["task_id"],
                        event_type="item.recovered",
                        message="服务重启后已重新排队工作簿",
                        created_at=now,
                        level="warning",
                        details={"item_id": row["item_id"]},
                    )
                else:
                    error = canonical_json(
                        {
                            "code": "BATCH_ITEM_RECOVERY_EXHAUSTED",
                            "message": "单工作簿进程恢复次数已用尽",
                            "retryable": True,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE items
                        SET status='orchestration_failed', orchestration_error_json=?,
                            finished_at=?, updated_at=?, lease_token=NULL, lease_expires_at=NULL
                        WHERE item_id=? AND status='running'
                        """,
                        (error, now, now, row["item_id"]),
                    )
                    self._append_event(
                        connection,
                        task_id=row["task_id"],
                        event_type="item.error",
                        message="单工作簿进程恢复次数已用尽",
                        created_at=now,
                        level="error",
                        details={
                            "code": "BATCH_ITEM_RECOVERY_EXHAUSTED",
                            "item_id": row["item_id"],
                            "retryable": True,
                        },
                    )
            cancelling = connection.execute(
                "SELECT task_id FROM tasks WHERE status='cancelling'"
            ).fetchall()
            for task in cancelling:
                connection.execute(
                    """
                    UPDATE items
                    SET status='cancelled', reason_code='BATCH_CANCELLED_BEFORE_START',
                        finished_at=?, updated_at=?
                    WHERE task_id=? AND status='queued'
                    """,
                    (now, now, task["task_id"]),
                )
                affected_tasks.add(task["task_id"])
            for task_id in affected_tasks:
                self._finalize_if_done(connection, task_id, now)

    def load_result(self, result_ref: str) -> tuple[bytes, str]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT i.result_path, i.result_sha256, t.expires_at, t.task_id
                FROM items i JOIN tasks t ON t.task_id=i.task_id
                WHERE i.result_ref=?
                """,
                (result_ref,),
            ).fetchone()
            if row is None:
                tombstone = connection.execute(
                    "SELECT 1 FROM tombstones WHERE resource_type='result' AND resource_id=?",
                    (result_ref,),
                ).fetchone()
                if tombstone is not None:
                    raise BatchDiffError(
                        "BATCH_RESULT_EXPIRED",
                        "批量结果已过期",
                        status_code=410,
                    )
                raise BatchDiffError(
                    "BATCH_RESULT_NOT_FOUND",
                    "批量结果不存在",
                    status_code=404,
                )
            deleted = connection.execute(
                "SELECT 1 FROM manual_deletions WHERE task_id=?",
                (row["task_id"],),
            ).fetchone()
            if deleted is not None:
                raise BatchDiffError(
                    "BATCH_RESULT_EXPIRED",
                    "批量结果已删除",
                    status_code=410,
                )
            if row["expires_at"] and row["expires_at"] <= isoformat():
                raise BatchDiffError(
                    "BATCH_RESULT_EXPIRED",
                    "批量结果已过期",
                    status_code=410,
                )
            path = (self.state_directory / row["result_path"]).resolve()
            root = self.results_directory.resolve()
            if root not in path.parents or not path.is_file():
                raise BatchDiffError(
                    "BATCH_RESULT_NOT_FOUND",
                    "批量结果不存在",
                    status_code=404,
                )
            content = gzip.decompress(path.read_bytes())
            if hashlib.sha256(content).hexdigest() != row["result_sha256"]:
                raise BatchDiffError(
                    "BATCH_RESULT_CORRUPT",
                    "批量结果校验失败",
                    status_code=500,
                )
            return content, row["result_sha256"]

    def cleanup_expired(self) -> int:
        now_value = utc_now()
        now = isoformat(now_value)
        tombstone_expires = isoformat(now_value + timedelta(days=TOMBSTONE_DAYS))
        event_cutoff = isoformat(
            now_value - timedelta(days=self.event_retention_days)
        )
        result_paths: list[str] = []
        removed = 0
        with self._connect() as connection:
            self._begin(connection)
            tasks = connection.execute(
                "SELECT * FROM tasks WHERE status IN ('completed','completed_with_failures','cancelled','failed') AND expires_at<=?",
                (now,),
            ).fetchall()
            for task in tasks:
                items = connection.execute(
                    "SELECT result_ref, result_path FROM items WHERE task_id=?",
                    (task["task_id"],),
                ).fetchall()
                for item in items:
                    if item["result_ref"]:
                        connection.execute(
                            "INSERT OR REPLACE INTO tombstones VALUES ('result', ?, '{}', ?)",
                            (item["result_ref"], tombstone_expires),
                        )
                    if item["result_path"]:
                        result_paths.append(item["result_path"])
                connection.execute(
                    "INSERT OR REPLACE INTO tombstones VALUES ('task', ?, '{}', ?)",
                    (task["task_id"], tombstone_expires),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO tombstones VALUES ('request', ?, ?, ?)",
                    (
                        f"{task['owner_scope']}:{task['request_id']}",
                        canonical_json(
                            {
                                "request_hash": task["request_hash"],
                                "task_id": task["task_id"],
                            }
                        ),
                        tombstone_expires,
                    ),
                )
                connection.execute("DELETE FROM tasks WHERE task_id=?", (task["task_id"],))
                removed += 1
            connection.execute("DELETE FROM tombstones WHERE expires_at<=?", (now,))
            connection.execute(
                "DELETE FROM manual_deletions WHERE tombstone_expires_at<=?",
                (now,),
            )
            connection.execute(
                "DELETE FROM task_events WHERE created_at<=?",
                (event_cutoff,),
            )
        for relative_path in result_paths:
            self.remove_result_blob(relative_path)
        self._remove_orphan_results()
        return removed

    def _remove_orphan_results(self) -> None:
        if not self.results_directory.exists():
            return
        with self._connect() as connection:
            referenced = {
                str(row[0])
                for row in connection.execute(
                    "SELECT result_path FROM items WHERE result_path IS NOT NULL"
                ).fetchall()
            }
        for path in self.results_directory.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.state_directory).as_posix()
            if path.suffix == ".tmp" or relative not in referenced:
                try:
                    path.unlink()
                except OSError:
                    pass
