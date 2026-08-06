"""Read M2 batch artifacts through SQLite's immutable read-only mode."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

from app.schemas.batch import BatchTaskPayload
from app.services.batch_store import BatchDiffError


class ReadOnlyBatchStore:
    """Expose the exporter's two reads without initializing or mutating SQLite."""

    def __init__(self, state_directory: Path):
        self.state_directory = Path(state_directory).resolve()
        self.database_path = self.state_directory / "batch.sqlite3"
        self.results_directory = (self.state_directory / "results").resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise BatchDiffError(
                "BATCH_TASK_NOT_FOUND",
                "批量任务数据库不存在",
                status_code=404,
            )
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def get_task(self, task_id: str) -> BatchTaskPayload:
        with self._connect() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise BatchDiffError(
                    "BATCH_TASK_NOT_FOUND",
                    "批量任务不存在",
                    status_code=404,
                )
            items = list(
                connection.execute(
                    "SELECT * FROM items WHERE task_id=? ORDER BY ordinal",
                    (task_id,),
                ).fetchall()
            )

        statuses = (
            "queued",
            "running",
            "succeeded",
            "business_failed",
            "orchestration_failed",
            "skipped",
            "cancelled",
        )
        counts = {
            status: sum(item["status"] == status for item in items)
            for status in statuses
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
        total = len(items) if task["candidate_status"] == "ready" else None
        progress = {
            "total_items": total,
            "queued_items": counts["queued"] if total is not None else 0,
            "running_items": counts["running"] if total is not None else 0,
            "succeeded_items": counts["succeeded"] if total is not None else 0,
            "business_failed_items": counts["business_failed"] if total is not None else 0,
            "orchestration_failed_items": counts["orchestration_failed"] if total is not None else 0,
            "skipped_items": counts["skipped"] if total is not None else 0,
            "cancelled_items": counts["cancelled"] if total is not None else 0,
            "processed_items": terminal if total is not None else 0,
            "ratio": None if total is None else (1.0 if total == 0 else terminal / total),
        }
        payload_items = []
        for item in items:
            payload_items.append(
                {
                    "item_id": item["item_id"],
                    "retry_of_item_id": item["retry_of_item_id"],
                    "ordinal": item["ordinal"],
                    "candidate": json.loads(item["candidate_json"]),
                    "status": item["status"],
                    "diff_status": item["diff_status"],
                    "diff_error_count": item["diff_error_count"],
                    "result_ref": item["result_ref"],
                    "result_sha256": item["result_sha256"],
                    "result_size_bytes": item["result_size_bytes"],
                    "result_expires_at": item["result_expires_at"],
                    "orchestration_error": (
                        json.loads(item["orchestration_error_json"])
                        if item["orchestration_error_json"]
                        else None
                    ),
                    "reason_code": item["reason_code"],
                    "attempt_count": item["attempt_count"],
                    "recovery_count": item["recovery_count"],
                    "created_at": item["created_at"],
                    "started_at": item["started_at"],
                    "finished_at": item["finished_at"],
                    "updated_at": item["updated_at"],
                }
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
                "progress": progress,
                "items": payload_items,
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

    def load_result(self, result_ref: str) -> tuple[bytes, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_path, result_sha256 FROM items WHERE result_ref=?",
                (result_ref,),
            ).fetchone()
        if row is None:
            raise BatchDiffError(
                "BATCH_RESULT_NOT_FOUND",
                "批量结果不存在",
                status_code=404,
            )
        path = (self.state_directory / row["result_path"]).resolve()
        if self.results_directory not in path.parents or not path.is_file():
            raise BatchDiffError(
                "BATCH_RESULT_NOT_FOUND",
                "批量结果不存在",
                status_code=404,
            )
        content = gzip.decompress(path.read_bytes())
        digest = hashlib.sha256(content).hexdigest()
        if digest != row["result_sha256"]:
            raise BatchDiffError(
                "BATCH_RESULT_CORRUPT",
                "批量结果校验失败",
                status_code=500,
            )
        return content, digest
