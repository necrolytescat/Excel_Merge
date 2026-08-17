"""M2/M4 shared persistent and fair workbook execution slots."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
import time
from uuid import uuid4

from app.services.workbook_execution_gate import WorkbookExecutionGate


@dataclass
class WorkbookExecutionLease:
    scheduler: "PersistentWorkbookExecutionScheduler"
    token: str
    flow_key: str
    slot_id: int
    _released: bool = field(default=False, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def renew(self) -> bool:
        with self._lock:
            if self._released:
                return False
            return self.scheduler.renew(self)

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self.scheduler.release(self)


class PersistentWorkbookExecutionScheduler:
    """Coordinates fair slots across M2/M4 schedulers and app processes."""

    def __init__(
        self,
        database_path: Path,
        execution_gate: WorkbookExecutionGate,
        *,
        global_limit: int = 4,
        per_flow_limit: int = 4,
        lease_seconds: float = 60.0,
        demand_seconds: float = 2.0,
    ):
        self.database_path = Path(database_path)
        self.execution_gate = execution_gate
        self.global_limit = max(1, int(global_limit))
        self.per_flow_limit = max(1, min(int(per_flow_limit), self.global_limit))
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.demand_seconds = max(0.5, float(demand_seconds))
        self.instance_id = uuid4().hex
        self._initialize_lock = Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database_path, timeout=30)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS workbook_execution_slots (
                        slot_id INTEGER PRIMARY KEY,
                        owner_token TEXT NOT NULL UNIQUE,
                        owner_instance_id TEXT NOT NULL,
                        flow_key TEXT NOT NULL,
                        acquired_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_workbook_slots_flow
                    ON workbook_execution_slots(flow_key, expires_at);
                    CREATE TABLE IF NOT EXISTS workbook_execution_queue (
                        flow_key TEXT PRIMARY KEY,
                        requested_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS workbook_execution_history (
                        flow_key TEXT PRIMARY KEY,
                        last_granted_at REAL NOT NULL
                    );
                    """
                )
                queue_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(workbook_execution_queue)"
                    ).fetchall()
                }
                if "expires_at" not in queue_columns:
                    connection.execute(
                        "ALTER TABLE workbook_execution_queue ADD COLUMN expires_at REAL NOT NULL DEFAULT 0"
                    )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    def sync_demands(self, namespace: str, flow_keys: list[str]) -> None:
        prefix = str(namespace).strip() + ":"
        normalized = {
            str(flow_key).strip()
            for flow_key in flow_keys
            if str(flow_key).strip().startswith(prefix)
        }
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM workbook_execution_queue WHERE expires_at<=?",
                (now,),
            )
            existing = connection.execute(
                "SELECT flow_key FROM workbook_execution_queue WHERE flow_key LIKE ?",
                (prefix + "%",),
            ).fetchall()
            stale = [str(row["flow_key"]) for row in existing if row["flow_key"] not in normalized]
            if stale:
                connection.executemany(
                    "DELETE FROM workbook_execution_queue WHERE flow_key=?",
                    ((flow_key,) for flow_key in stale),
                )
            connection.executemany(
                """
                INSERT INTO workbook_execution_queue(flow_key, requested_at, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(flow_key) DO UPDATE SET expires_at=excluded.expires_at
                """,
                (
                    (flow_key, now, now + self.demand_seconds)
                    for flow_key in sorted(normalized)
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _drop_slot(self, token: str) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM workbook_execution_slots WHERE owner_token=?",
                (token,),
            ).rowcount
            connection.commit()
            return bool(deleted)
        finally:
            connection.close()

    def try_acquire(self, flow_key: str) -> WorkbookExecutionLease | None:
        normalized = str(flow_key).strip()
        if not normalized:
            raise ValueError("flow_key must not be empty")
        now = time.time()
        token = secrets.token_urlsafe(24)
        connection = self._connect()
        slot_id: int | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM workbook_execution_slots WHERE expires_at<=?",
                (now,),
            )
            connection.execute(
                "DELETE FROM workbook_execution_queue WHERE expires_at<=?",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO workbook_execution_queue(flow_key, requested_at, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(flow_key) DO UPDATE SET expires_at=excluded.expires_at
                """,
                (normalized, now, now + self.demand_seconds),
            )
            active_total = connection.execute(
                "SELECT COUNT(*) FROM workbook_execution_slots"
            ).fetchone()[0]
            active_flow = connection.execute(
                "SELECT COUNT(*) FROM workbook_execution_slots WHERE flow_key=?",
                (normalized,),
            ).fetchone()[0]
            next_flow = connection.execute(
                """
                SELECT q.flow_key
                FROM workbook_execution_queue q
                LEFT JOIN workbook_execution_history h ON h.flow_key=q.flow_key
                ORDER BY CASE WHEN h.last_granted_at IS NULL THEN 0 ELSE 1 END,
                         h.last_granted_at, q.requested_at, q.flow_key
                LIMIT 1
                """
            ).fetchone()
            if (
                active_total >= self.global_limit
                or active_flow >= self.per_flow_limit
                or next_flow is None
                or next_flow["flow_key"] != normalized
            ):
                connection.commit()
                return None
            occupied = {
                int(row[0])
                for row in connection.execute(
                    "SELECT slot_id FROM workbook_execution_slots"
                ).fetchall()
            }
            slot_id = next(
                candidate
                for candidate in range(self.global_limit)
                if candidate not in occupied
            )
            connection.execute(
                """
                INSERT INTO workbook_execution_slots(
                    slot_id, owner_token, owner_instance_id, flow_key,
                    acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    slot_id,
                    token,
                    self.instance_id,
                    normalized,
                    now,
                    now + self.lease_seconds,
                ),
            )
            connection.execute(
                "DELETE FROM workbook_execution_queue WHERE flow_key=?",
                (normalized,),
            )
            connection.execute(
                """
                INSERT INTO workbook_execution_history(flow_key, last_granted_at)
                VALUES (?, ?)
                ON CONFLICT(flow_key) DO UPDATE SET last_granted_at=excluded.last_granted_at
                """,
                (normalized, now),
            )
            connection.commit()
        finally:
            connection.close()
        if not self.execution_gate.try_acquire():
            self._drop_slot(token)
            return None
        return WorkbookExecutionLease(self, token, normalized, int(slot_id))

    def renew(self, lease: WorkbookExecutionLease) -> bool:
        connection = self._connect()
        try:
            updated = connection.execute(
                """
                UPDATE workbook_execution_slots SET expires_at=?
                WHERE owner_token=? AND owner_instance_id=?
                """,
                (time.time() + self.lease_seconds, lease.token, self.instance_id),
            ).rowcount
            connection.commit()
            return bool(updated)
        finally:
            connection.close()

    def release(self, lease: WorkbookExecutionLease) -> None:
        self._drop_slot(lease.token)
        self.execution_gate.release()

    def close(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            released = connection.execute(
                "DELETE FROM workbook_execution_slots WHERE owner_instance_id=?",
                (self.instance_id,),
            ).rowcount
            connection.commit()
        finally:
            connection.close()
        for _ in range(released):
            self.execution_gate.release()
