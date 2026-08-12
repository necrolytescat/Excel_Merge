"""M4 DiffPlan 的 SQLite 持久化与幂等命令处理。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Iterator
from uuid import UUID, uuid4

from app.schemas.diff_plan import (
    DiffPlanCommandRequestPayload,
    DiffPlanCreateRequestPayload,
    DiffPlanListPayload,
    DiffPlanPayload,
    DiffPlanUpdateRequestPayload,
)


class DiffPlanError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class DiffPlanStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self._init_lock = Lock()
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
        with self._init_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database_path, timeout=30)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE IF NOT EXISTS diff_plans (
                        plan_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        source_endpoint_id TEXT NOT NULL,
                        target_endpoint_ids_json TEXT NOT NULL,
                        workbook_paths_json TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK(version >= 1),
                        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        archived_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS diff_plan_commands (
                        request_id TEXT PRIMARY KEY,
                        command_type TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        plan_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(plan_id) REFERENCES diff_plans(plan_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_diff_plans_archived_updated
                    ON diff_plans(archived, updated_at DESC, plan_id DESC);
                    """
                )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def _payload(row: sqlite3.Row) -> DiffPlanPayload:
        return DiffPlanPayload(
            plan_id=row["plan_id"],
            name=row["name"],
            source_endpoint_id=row["source_endpoint_id"],
            target_endpoint_ids=json.loads(row["target_endpoint_ids_json"]),
            workbook_paths=json.loads(row["workbook_paths_json"]),
            version=row["version"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
            recent_run=None,
        )

    @staticmethod
    def _definition(payload) -> dict:
        return {
            "name": payload.name,
            "source_endpoint_id": payload.source_endpoint_id,
            "target_endpoint_ids": payload.target_endpoint_ids,
            "workbook_paths": payload.workbook_paths,
        }

    @staticmethod
    def _command_replay(
        connection: sqlite3.Connection,
        *,
        request_id: UUID,
        command_type: str,
        request_hash: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT * FROM diff_plan_commands WHERE request_id=?",
            (str(request_id),),
        ).fetchone()
        if row is None:
            return None
        if row["command_type"] != command_type or row["request_hash"] != request_hash:
            raise DiffPlanError(
                "DIFF_PLAN_IDEMPOTENCY_CONFLICT",
                "相同 request_id 已用于不同的计划命令",
                status_code=409,
            )
        return str(row["plan_id"])

    @staticmethod
    def _record_command(
        connection: sqlite3.Connection,
        *,
        request_id: UUID,
        command_type: str,
        request_hash: str,
        plan_id: str,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO diff_plan_commands VALUES (?, ?, ?, ?, ?)",
            (str(request_id), command_type, request_hash, plan_id, created_at),
        )

    def create(self, payload: DiffPlanCreateRequestPayload) -> tuple[DiffPlanPayload, bool]:
        definition = self._definition(payload)
        request_hash = _hash(definition)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay_id = self._command_replay(
                connection,
                request_id=payload.request_id,
                command_type="create",
                request_hash=request_hash,
            )
            if replay_id is not None:
                row = connection.execute(
                    "SELECT * FROM diff_plans WHERE plan_id=?", (replay_id,)
                ).fetchone()
                return self._payload(row), False
            plan_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO diff_plans (
                    plan_id, name, source_endpoint_id, target_endpoint_ids_json,
                    workbook_paths_json, version, archived, created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, NULL)
                """,
                (
                    plan_id,
                    payload.name,
                    payload.source_endpoint_id,
                    _canonical(payload.target_endpoint_ids),
                    _canonical(payload.workbook_paths),
                    now,
                    now,
                ),
            )
            self._record_command(
                connection,
                request_id=payload.request_id,
                command_type="create",
                request_hash=request_hash,
                plan_id=plan_id,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM diff_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            return self._payload(row), True

    def get(self, plan_id: UUID | str) -> DiffPlanPayload:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM diff_plans WHERE plan_id=?", (str(plan_id),)
            ).fetchone()
        if row is None:
            raise DiffPlanError("DIFF_PLAN_NOT_FOUND", "表格对比计划不存在", status_code=404)
        return self._payload(row)

    def list(self, *, archived: bool) -> DiffPlanListPayload:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM diff_plans WHERE archived=? ORDER BY updated_at DESC, plan_id DESC",
                (1 if archived else 0,),
            ).fetchall()
        plans = [self._payload(row) for row in rows]
        return DiffPlanListPayload(plans=plans, total=len(plans))

    def update(self, plan_id: UUID | str, payload: DiffPlanUpdateRequestPayload) -> tuple[DiffPlanPayload, bool]:
        definition = self._definition(payload)
        request_hash = _hash({**definition, "expected_version": payload.expected_version})
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay_id = self._command_replay(
                connection,
                request_id=payload.request_id,
                command_type="update",
                request_hash=request_hash,
            )
            if replay_id is not None:
                row = connection.execute(
                    "SELECT * FROM diff_plans WHERE plan_id=?", (replay_id,)
                ).fetchone()
                return self._payload(row), False
            row = connection.execute(
                "SELECT * FROM diff_plans WHERE plan_id=?", (str(plan_id),)
            ).fetchone()
            if row is None:
                raise DiffPlanError("DIFF_PLAN_NOT_FOUND", "表格对比计划不存在", status_code=404)
            if row["archived"]:
                raise DiffPlanError("DIFF_PLAN_ARCHIVED", "归档计划不能直接编辑", status_code=409)
            if row["version"] != payload.expected_version:
                raise DiffPlanError(
                    "DIFF_PLAN_VERSION_CONFLICT",
                    "计划已被其他操作更新，请刷新后重试",
                    status_code=409,
                )
            connection.execute(
                """
                UPDATE diff_plans SET name=?, source_endpoint_id=?,
                    target_endpoint_ids_json=?, workbook_paths_json=?,
                    version=version+1, updated_at=? WHERE plan_id=?
                """,
                (
                    payload.name,
                    payload.source_endpoint_id,
                    _canonical(payload.target_endpoint_ids),
                    _canonical(payload.workbook_paths),
                    now,
                    str(plan_id),
                ),
            )
            self._record_command(
                connection,
                request_id=payload.request_id,
                command_type="update",
                request_hash=request_hash,
                plan_id=str(plan_id),
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM diff_plans WHERE plan_id=?", (str(plan_id),)
            ).fetchone()
            return self._payload(updated), True

    def set_archived(
        self,
        plan_id: UUID | str,
        payload: DiffPlanCommandRequestPayload,
        *,
        archived: bool,
    ) -> tuple[DiffPlanPayload, bool]:
        command_type = "archive" if archived else "restore"
        request_hash = _hash({"expected_version": payload.expected_version})
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay_id = self._command_replay(
                connection,
                request_id=payload.request_id,
                command_type=command_type,
                request_hash=request_hash,
            )
            if replay_id is not None:
                row = connection.execute(
                    "SELECT * FROM diff_plans WHERE plan_id=?", (replay_id,)
                ).fetchone()
                return self._payload(row), False
            row = connection.execute(
                "SELECT * FROM diff_plans WHERE plan_id=?", (str(plan_id),)
            ).fetchone()
            if row is None:
                raise DiffPlanError("DIFF_PLAN_NOT_FOUND", "表格对比计划不存在", status_code=404)
            if row["version"] != payload.expected_version:
                raise DiffPlanError(
                    "DIFF_PLAN_VERSION_CONFLICT",
                    "计划已被其他操作更新，请刷新后重试",
                    status_code=409,
                )
            if bool(row["archived"]) == archived:
                raise DiffPlanError(
                    "DIFF_PLAN_STATE_CONFLICT",
                    "计划已经处于目标状态",
                    status_code=409,
                )
            connection.execute(
                """
                UPDATE diff_plans SET archived=?, archived_at=?, version=version+1,
                    updated_at=? WHERE plan_id=?
                """,
                (1 if archived else 0, now if archived else None, now, str(plan_id)),
            )
            self._record_command(
                connection,
                request_id=payload.request_id,
                command_type=command_type,
                request_hash=request_hash,
                plan_id=str(plan_id),
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM diff_plans WHERE plan_id=?", (str(plan_id),)
            ).fetchone()
            return self._payload(updated), True

