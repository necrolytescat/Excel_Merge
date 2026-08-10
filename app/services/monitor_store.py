"""SQLite fact store for M3 monitor tasks, boundaries, runs and attempts."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Callable, Iterator, Literal
from uuid import UUID, uuid4

from app.schemas.monitor import MonitorPublicErrorPayload, MonitorRunSummaryPayload
from app.services.monitor_schedule import BoundarySpec, BoundaryType, SHANGHAI, require_utc


DEFAULT_DATABASE_PATH = Path("var/m3-monitor/monitor.sqlite3")
SCHEMA_VERSION = 6


class MonitorLeaseLost(RuntimeError):
    pass


class MonitorStateConflict(RuntimeError):
    pass


class MonitorIdempotencyConflict(RuntimeError):
    pass


def _timestamp(value: datetime) -> str:
    return require_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _errors_json(errors: list[MonitorPublicErrorPayload]) -> str:
    return json.dumps(
        [error.model_dump(mode="json") for error in errors],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _errors(value: str | None) -> list[MonitorPublicErrorPayload]:
    return [MonitorPublicErrorPayload.model_validate(item) for item in json.loads(value or "[]")]


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    name: str
    lifecycle: str
    endpoint_id: str
    branch_label: str
    repository_uuid: str
    canonical_url: str
    repository_relative_path: str
    bound_revision: int
    copy_boundary_revision: int
    effective_at: datetime
    schedule_effective_at: datetime
    end_at: datetime | None
    daily_trigger_time: str
    generation: int
    scheduler_desired_state: str
    scheduler_sync_status: str
    windows_task_name: str
    scheduler_last_synced_at: datetime | None
    scheduler_error: MonitorPublicErrorPayload | None
    last_runner_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None
    ended_at: datetime | None
    ended_reason: str | None
    archived_at: datetime | None


@dataclass(frozen=True)
class BoundaryRecord:
    boundary_id: str
    task_id: str
    boundary_at: datetime
    boundary_type: BoundaryType
    generation: int
    reason: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_id: str
    boundary_id: str
    generation: int
    start_at: datetime
    end_at: datetime
    boundary_type: BoundaryType
    status: str
    attempt_count: int
    start_revision: int | None
    end_revision: int | None
    summary: dict[str, Any] | None
    report_ref: str | None
    report_sha256: str | None
    report_expires_at: datetime | None
    errors: list[MonitorPublicErrorPayload]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class TaskRunOverview:
    latest_run: RunRecord | None
    latest_report_run: RunRecord | None
    pending_run_count: int
    latest_boundary_at: datetime


@dataclass(frozen=True)
class LeaseClaim:
    run: RunRecord
    lease_token: str
    attempt: int
    trigger: Literal["scheduled", "automatic_retry", "manual_retry"]


@dataclass(frozen=True)
class PublicationRecord:
    run_id: str
    task_id: str
    state: Literal["prepared", "activated"]
    status: Literal["succeeded", "partial"]
    start_revision: int
    end_revision: int
    summary: dict[str, Any]
    errors: list[MonitorPublicErrorPayload]
    report_ref: str
    json_sha256: str
    html_sha256: str
    report_expires_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CommandRecord:
    request_id: str
    method: str
    target: str
    payload_hash: str
    payload_json: str
    state: Literal["pending", "completed"]
    response_status: int | None
    response_json: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RetryIntentRecord:
    request_id: str
    task_id: str
    run_id: str
    state: Literal["pending", "dispatching", "dispatched"]
    dispatch_count: int
    lease_token: str | None
    lease_expires_at: datetime | None


MIGRATION_1 = (
    """CREATE TABLE monitor_tasks (
        task_id TEXT PRIMARY KEY, name TEXT NOT NULL, lifecycle TEXT NOT NULL,
        endpoint_id TEXT NOT NULL, branch_label TEXT NOT NULL,
        repository_uuid TEXT NOT NULL, canonical_url TEXT NOT NULL,
        repository_relative_path TEXT NOT NULL,
        bound_revision INTEGER NOT NULL, copy_boundary_revision INTEGER NOT NULL,
        effective_at TEXT NOT NULL, schedule_effective_at TEXT NOT NULL,
        end_at TEXT, daily_trigger_time TEXT NOT NULL,
        timezone TEXT NOT NULL CHECK (timezone = 'Asia/Shanghai'),
        generation INTEGER NOT NULL CHECK (generation > 0),
        scheduler_desired_state TEXT NOT NULL,
        scheduler_sync_status TEXT NOT NULL,
        last_runner_heartbeat_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        paused_at TEXT, ended_at TEXT, archived_at TEXT
    )""",
    """CREATE TABLE monitor_boundaries (
        boundary_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
        boundary_at TEXT NOT NULL, boundary_type TEXT NOT NULL,
        local_display_at TEXT NOT NULL, generation INTEGER NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES monitor_tasks(task_id) ON DELETE CASCADE,
        UNIQUE(task_id, boundary_at, boundary_type)
    )""",
    """CREATE TABLE monitor_runs (
        run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, boundary_id TEXT NOT NULL UNIQUE,
        generation INTEGER NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
        logical_cutoff_at TEXT NOT NULL, boundary_type TEXT NOT NULL, status TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires_at TEXT,
        start_revision INTEGER, end_revision INTEGER, summary_json TEXT,
        report_ref TEXT, report_sha256 TEXT, report_expires_at TEXT, errors_json TEXT NOT NULL,
        created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES monitor_tasks(task_id) ON DELETE CASCADE,
        FOREIGN KEY(boundary_id) REFERENCES monitor_boundaries(boundary_id) ON DELETE CASCADE,
        UNIQUE(task_id, logical_cutoff_at)
    )""",
    """CREATE TABLE monitor_run_attempts (
        run_id TEXT NOT NULL, attempt INTEGER NOT NULL, trigger TEXT NOT NULL,
        status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        errors_json TEXT NOT NULL,
        PRIMARY KEY(run_id, attempt),
        FOREIGN KEY(run_id) REFERENCES monitor_runs(run_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX monitor_runs_due_idx ON monitor_runs(task_id, status, end_at)",
    "CREATE INDEX monitor_boundaries_task_time_idx ON monitor_boundaries(task_id, boundary_at)",
)

MIGRATION_2 = (
    "ALTER TABLE monitor_tasks ADD COLUMN ended_reason TEXT",
)

MIGRATION_3 = (
    """CREATE TABLE monitor_run_publications (
        run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('prepared','activated')),
        status TEXT NOT NULL CHECK (status IN ('succeeded','partial')),
        start_revision INTEGER NOT NULL, end_revision INTEGER NOT NULL,
        summary_json TEXT NOT NULL, errors_json TEXT NOT NULL,
        report_ref TEXT NOT NULL, json_sha256 TEXT NOT NULL, html_sha256 TEXT NOT NULL,
        report_expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES monitor_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(task_id) REFERENCES monitor_tasks(task_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX monitor_publications_task_state_idx "
    "ON monitor_run_publications(task_id, state)",
)

MIGRATION_4 = (
    "ALTER TABLE monitor_tasks ADD COLUMN windows_task_name TEXT",
    "ALTER TABLE monitor_tasks ADD COLUMN scheduler_last_synced_at TEXT",
    "ALTER TABLE monitor_tasks ADD COLUMN scheduler_error_json TEXT",
)

MIGRATION_5 = (
    """CREATE TABLE monitor_commands (
        request_id TEXT PRIMARY KEY, method TEXT NOT NULL, target TEXT NOT NULL,
        payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','completed')),
        response_status INTEGER, response_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX monitor_commands_target_state_idx ON monitor_commands(target,state)",
    """CREATE TABLE monitor_retry_outbox (
        request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','dispatching','dispatched')),
        dispatch_count INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT, lease_expires_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(request_id) REFERENCES monitor_commands(request_id) ON DELETE CASCADE,
        FOREIGN KEY(task_id) REFERENCES monitor_tasks(task_id) ON DELETE CASCADE,
        FOREIGN KEY(run_id) REFERENCES monitor_runs(run_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX monitor_retry_outbox_state_idx ON monitor_retry_outbox(state,lease_expires_at)",
)

MIGRATION_6 = (
    """UPDATE monitor_retry_outbox AS current
       SET state='dispatched',lease_token=NULL,lease_expires_at=NULL
       WHERE current.state IN ('pending','dispatching')
         AND EXISTS (
             SELECT 1 FROM monitor_retry_outbox AS winner
             WHERE winner.run_id=current.run_id
               AND winner.state IN ('pending','dispatching')
               AND (
                   winner.created_at < current.created_at
                   OR (
                       winner.created_at=current.created_at
                       AND winner.request_id < current.request_id
                   )
               )
         )""",
    """CREATE UNIQUE INDEX monitor_retry_outbox_run_active_idx
       ON monitor_retry_outbox(run_id)
       WHERE state IN ('pending','dispatching')""",
)


class MonitorStore:
    def __init__(self, database_path: str | Path = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS monitor_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            versions = {
                row[0] for row in connection.execute("SELECT version FROM monitor_schema_migrations")
            }
            if any(version > SCHEMA_VERSION for version in versions):
                raise RuntimeError("monitor database schema is newer than this application")
            if 1 not in versions:
                for statement in MIGRATION_1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _timestamp(datetime.now(timezone.utc))),
                )
            if 2 not in versions:
                for statement in MIGRATION_2:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _timestamp(datetime.now(timezone.utc))),
                )
            if 3 not in versions:
                for statement in MIGRATION_3:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, _timestamp(datetime.now(timezone.utc))),
                )
            if 4 not in versions:
                for row in connection.execute("SELECT task_id FROM monitor_tasks"):
                    try:
                        canonical_task_id = str(UUID(row[0]))
                    except (TypeError, ValueError) as error:
                        raise RuntimeError(
                            "monitor database contains an invalid task identity"
                        ) from error
                    if canonical_task_id != row[0]:
                        raise RuntimeError(
                            "monitor database contains a non-canonical task identity"
                        )
                for statement in MIGRATION_4:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE monitor_tasks SET windows_task_name = "
                    "'ExcelMerge-M3-Monitor-' || lower(task_id) "
                    "WHERE windows_task_name IS NULL"
                )
                connection.execute(
                    "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, _timestamp(datetime.now(timezone.utc))),
                )
            if 5 not in versions:
                for statement in MIGRATION_5:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, _timestamp(datetime.now(timezone.utc))),
                )
            if 6 not in versions:
                for statement in MIGRATION_6:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (6, _timestamp(datetime.now(timezone.utc))),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"], name=row["name"], lifecycle=row["lifecycle"],
            endpoint_id=row["endpoint_id"], branch_label=row["branch_label"],
            repository_uuid=row["repository_uuid"], canonical_url=row["canonical_url"],
            repository_relative_path=row["repository_relative_path"],
            bound_revision=row["bound_revision"], copy_boundary_revision=row["copy_boundary_revision"],
            effective_at=_datetime(row["effective_at"]),
            schedule_effective_at=_datetime(row["schedule_effective_at"]),
            end_at=_datetime(row["end_at"]),
            daily_trigger_time=row["daily_trigger_time"], generation=row["generation"],
            scheduler_desired_state=row["scheduler_desired_state"],
            scheduler_sync_status=row["scheduler_sync_status"],
            windows_task_name=row["windows_task_name"],
            scheduler_last_synced_at=_datetime(row["scheduler_last_synced_at"]),
            scheduler_error=(
                MonitorPublicErrorPayload.model_validate(
                    json.loads(row["scheduler_error_json"])
                )
                if row["scheduler_error_json"]
                else None
            ),
            last_runner_heartbeat_at=_datetime(row["last_runner_heartbeat_at"]),
            created_at=_datetime(row["created_at"]), updated_at=_datetime(row["updated_at"]),
            paused_at=_datetime(row["paused_at"]), ended_at=_datetime(row["ended_at"]),
            ended_reason=row["ended_reason"],
            archived_at=_datetime(row["archived_at"]),
        )

    @staticmethod
    def _boundary(row: sqlite3.Row) -> BoundaryRecord:
        return BoundaryRecord(
            row["boundary_id"], row["task_id"], _datetime(row["boundary_at"]),
            BoundaryType(row["boundary_type"]), row["generation"], row["reason"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"], task_id=row["task_id"], boundary_id=row["boundary_id"],
            generation=row["generation"], start_at=_datetime(row["start_at"]),
            end_at=_datetime(row["end_at"]), boundary_type=BoundaryType(row["boundary_type"]),
            status=row["status"], attempt_count=row["attempt_count"],
            start_revision=row["start_revision"], end_revision=row["end_revision"],
            summary=json.loads(row["summary_json"]) if row["summary_json"] else None,
            report_ref=row["report_ref"], report_sha256=row["report_sha256"],
            report_expires_at=_datetime(row["report_expires_at"]), errors=_errors(row["errors_json"]),
            created_at=_datetime(row["created_at"]), started_at=_datetime(row["started_at"]),
            finished_at=_datetime(row["finished_at"]), updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _publication(row: sqlite3.Row) -> PublicationRecord:
        return PublicationRecord(
            run_id=row["run_id"],
            task_id=row["task_id"],
            state=row["state"],
            status=row["status"],
            start_revision=row["start_revision"],
            end_revision=row["end_revision"],
            summary=json.loads(row["summary_json"]),
            errors=_errors(row["errors_json"]),
            report_ref=row["report_ref"],
            json_sha256=row["json_sha256"],
            html_sha256=row["html_sha256"],
            report_expires_at=_datetime(row["report_expires_at"]),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )

    def create_task(self, values: dict[str, Any], start: BoundarySpec) -> TaskRecord:
        now = values["created_at"]
        if str(UUID(values["task_id"])) != values["task_id"]:
            raise ValueError("monitor task_id must be a canonical UUID")
        with self._transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO monitor_tasks (
                    task_id,name,lifecycle,endpoint_id,branch_label,repository_uuid,canonical_url,
                    repository_relative_path,bound_revision,copy_boundary_revision,effective_at,
                    schedule_effective_at,end_at,
                    daily_trigger_time,timezone,generation,scheduler_desired_state,
                    scheduler_sync_status,windows_task_name,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["task_id"], values["name"], "active", values["endpoint_id"],
                    values["branch_label"], values["repository_uuid"], values["canonical_url"],
                    values["repository_relative_path"], values["bound_revision"],
                    values["copy_boundary_revision"], _timestamp(values["effective_at"]),
                    _timestamp(values["effective_at"]),
                    _timestamp(values["end_at"]) if values.get("end_at") else None,
                    values["daily_trigger_time"], "Asia/Shanghai", 1, "enabled", "pending",
                    f"ExcelMerge-M3-Monitor-{values['task_id'].lower()}",
                    _timestamp(now), _timestamp(now),
                ),
            )
            self._insert_boundary(connection, values["task_id"], start, now)
            row = connection.execute(
                "SELECT * FROM monitor_tasks WHERE task_id=?", (values["task_id"],)
            ).fetchone()
        return self._task(row)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row) if row is not None else None

    def list_tasks(self) -> list[TaskRecord]:
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM monitor_tasks ORDER BY created_at, task_id").fetchall()
        return [self._task(row) for row in rows]

    def list_task_page(
        self,
        *,
        limit: int,
        statuses: list[str] | None = None,
        query: str | None = None,
        before_created_at: datetime | None = None,
        before_task_id: str | None = None,
    ) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("task page limit must be positive")
        where: list[str] = []
        parameters: list[Any] = []
        normalized_statuses = sorted(set(statuses or []))
        if normalized_statuses:
            public_status_predicates = {
                "active": "(lifecycle='active' AND scheduler_sync_status='synced')",
                "syncing": "(lifecycle='active' AND scheduler_sync_status='pending')",
                "scheduler_error": (
                    "(lifecycle='active' AND scheduler_sync_status "
                    "IN ('drifted','error','not_present'))"
                ),
                "paused": "lifecycle='paused'",
                "ended": "lifecycle='ended'",
                "archived": "lifecycle='archived'",
            }
            try:
                predicates = [
                    public_status_predicates[status]
                    for status in normalized_statuses
                ]
            except KeyError as error:
                raise ValueError("unknown public monitor task status") from error
            where.append("(" + " OR ".join(predicates) + ")")
        else:
            where.append("lifecycle<>'archived'")
        needle = (query or "").strip().lower()
        if needle:
            where.append(
                "(instr(lower(name),?)>0 OR instr(lower(endpoint_id),?)>0 "
                "OR instr(lower(branch_label),?)>0)"
            )
            parameters.extend((needle, needle, needle))
        if before_created_at is not None or before_task_id is not None:
            if before_created_at is None or before_task_id is None:
                raise ValueError("task cursor requires both sort values")
            cursor_time = _timestamp(before_created_at)
            cursor_id = str(UUID(before_task_id))
            where.append("(created_at<? OR (created_at=? AND task_id<?))")
            parameters.extend((cursor_time, cursor_time, cursor_id))
        sql = "SELECT * FROM monitor_tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC,task_id DESC LIMIT ?"
        parameters.append(limit)
        with self._transaction() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._task(row) for row in rows]

    def task_run_overviews(
        self, task_ids: list[str], *, now: datetime
    ) -> dict[str, TaskRunOverview]:
        normalized = [str(UUID(task_id)) for task_id in dict.fromkeys(task_ids)]
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        latest: dict[str, RunRecord] = {}
        published: dict[str, RunRecord] = {}
        pending: dict[str, int] = {}
        boundaries: dict[str, datetime] = {}
        with self._transaction() as connection:
            latest_rows = connection.execute(
                f"""WITH ranked AS (
                    SELECT monitor_runs.*,
                           row_number() OVER (
                               PARTITION BY task_id ORDER BY end_at DESC,run_id DESC
                           ) AS row_number
                    FROM monitor_runs WHERE task_id IN ({placeholders})
                ) SELECT * FROM ranked WHERE row_number=1""",
                normalized,
            ).fetchall()
            published_rows = connection.execute(
                f"""WITH ranked AS (
                    SELECT monitor_runs.*,
                           row_number() OVER (
                               PARTITION BY task_id ORDER BY end_at DESC,run_id DESC
                           ) AS row_number
                    FROM monitor_runs
                    WHERE task_id IN ({placeholders})
                      AND status IN ('succeeded','partial')
                ) SELECT * FROM ranked WHERE row_number=1""",
                normalized,
            ).fetchall()
            pending_rows = connection.execute(
                f"""SELECT task_id,count(*) AS pending_count FROM monitor_runs
                    WHERE task_id IN ({placeholders}) AND end_at<=?
                      AND status IN ('queued','running') GROUP BY task_id""",
                [*normalized, _timestamp(now)],
            ).fetchall()
            boundary_rows = connection.execute(
                f"""SELECT task_id,max(boundary_at) AS latest_boundary_at
                    FROM monitor_boundaries WHERE task_id IN ({placeholders})
                    GROUP BY task_id""",
                normalized,
            ).fetchall()
        for row in latest_rows:
            latest[row["task_id"]] = self._run(row)
        for row in published_rows:
            published[row["task_id"]] = self._run(row)
        for row in pending_rows:
            pending[row["task_id"]] = int(row["pending_count"])
        for row in boundary_rows:
            boundaries[row["task_id"]] = _datetime(row["latest_boundary_at"])
        return {
            task_id: TaskRunOverview(
                latest_run=latest.get(task_id),
                latest_report_run=published.get(task_id),
                pending_run_count=pending.get(task_id, 0),
                latest_boundary_at=boundaries[task_id],
            )
            for task_id in normalized
        }

    @staticmethod
    def _command(row: sqlite3.Row) -> CommandRecord:
        return CommandRecord(
            request_id=row["request_id"],
            method=row["method"],
            target=row["target"],
            payload_hash=row["payload_hash"],
            payload_json=row["payload_json"],
            state=row["state"],
            response_status=row["response_status"],
            response_json=row["response_json"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )

    def claim_command(
        self,
        *,
        request_id: str,
        method: str,
        target: str,
        payload_hash: str,
        payload_json: str,
        now: datetime,
        allow_parallel_target: bool = False,
    ) -> CommandRecord:
        request_id = str(UUID(request_id))
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM monitor_commands WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["method"] != method
                    or existing["target"] != target
                    or existing["payload_hash"] != payload_hash
                ):
                    raise MonitorIdempotencyConflict(
                        "request_id already belongs to another monitor command"
                    )
                return self._command(existing)
            if not allow_parallel_target:
                pending = connection.execute(
                    "SELECT request_id FROM monitor_commands WHERE target=? AND state='pending'",
                    (target,),
                ).fetchone()
                if pending is not None:
                    raise MonitorStateConflict("another monitor command is still pending")
            timestamp = _timestamp(now)
            connection.execute(
                """INSERT INTO monitor_commands
                   (request_id,method,target,payload_hash,payload_json,state,created_at,updated_at)
                   VALUES (?,?,?,?,?,'pending',?,?)""",
                (request_id, method, target, payload_hash, payload_json, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM monitor_commands WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._command(row)

    def list_pending_commands(self) -> list[CommandRecord]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_commands WHERE state='pending' ORDER BY created_at,request_id"
            ).fetchall()
        return [self._command(row) for row in rows]

    def complete_command(
        self,
        request_id: str,
        *,
        response_status: int,
        response_json: str,
        now: datetime,
    ) -> CommandRecord:
        request_id = str(UUID(request_id))
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM monitor_commands WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row["state"] == "completed":
                return self._command(row)
            connection.execute(
                """UPDATE monitor_commands
                   SET state='completed',response_status=?,response_json=?,updated_at=?
                   WHERE request_id=? AND state='pending'""",
                (response_status, response_json, _timestamp(now), request_id),
            )
            completed = connection.execute(
                "SELECT * FROM monitor_commands WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._command(completed)

    def discard_pending_command(self, request_id: str) -> None:
        with self._transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM monitor_commands WHERE request_id=? AND state='pending'",
                (str(UUID(request_id)),),
            )

    @staticmethod
    def _retry_intent(row: sqlite3.Row) -> RetryIntentRecord:
        return RetryIntentRecord(
            request_id=row["request_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            state=row["state"],
            dispatch_count=row["dispatch_count"],
            lease_token=row["lease_token"],
            lease_expires_at=_datetime(row["lease_expires_at"]),
        )

    def accept_retry_intent(
        self,
        *,
        request_id: str,
        run_id: str,
        method: str,
        target: str,
        payload_hash: str,
        payload_json: str,
        accepted_response_json: Callable[[str], str],
        not_found_response_json: str,
        conflict_response_json: str,
        now: datetime,
    ) -> CommandRecord:
        request_id = str(UUID(request_id))
        run_id = str(UUID(run_id))
        command = self.claim_command(
            request_id=request_id,
            method=method,
            target=target,
            payload_hash=payload_hash,
            payload_json=payload_json,
            now=now,
            allow_parallel_target=True,
        )
        if command.state == "completed":
            return command
        timestamp = _timestamp(now)
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM monitor_commands WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(request_id)
            if existing["state"] == "completed":
                return self._command(existing)
            run = connection.execute(
                """SELECT r.run_id,r.task_id,r.status,t.lifecycle
                   FROM monitor_runs r JOIN monitor_tasks t ON t.task_id=r.task_id
                   WHERE r.run_id=?""",
                (run_id,),
            ).fetchone()
            if run is None:
                connection.execute(
                    """UPDATE monitor_commands
                       SET state='completed',response_status=404,response_json=?,updated_at=?
                       WHERE request_id=? AND state='pending'""",
                    (not_found_response_json, timestamp, request_id),
                )
                completed = connection.execute(
                    "SELECT * FROM monitor_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                return self._command(completed)
            if run["status"] != "failed" or run["lifecycle"] == "archived":
                connection.execute(
                    """UPDATE monitor_commands
                       SET state='completed',response_status=409,response_json=?,updated_at=?
                       WHERE request_id=? AND state='pending'""",
                    (conflict_response_json, timestamp, request_id),
                )
                completed = connection.execute(
                    "SELECT * FROM monitor_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                return self._command(completed)
            active = connection.execute(
                """SELECT request_id FROM monitor_retry_outbox
                   WHERE run_id=? AND state IN ('pending','dispatching')""",
                (run_id,),
            ).fetchone()
            if active is not None:
                connection.execute(
                    """UPDATE monitor_commands
                       SET state='completed',response_status=409,response_json=?,updated_at=?
                       WHERE request_id=? AND state='pending'""",
                    (conflict_response_json, timestamp, request_id),
                )
                denied = connection.execute(
                    "SELECT * FROM monitor_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                return self._command(denied)
            response_json = accepted_response_json(run["task_id"])
            connection.execute(
                """UPDATE monitor_commands
                   SET state='completed',response_status=202,response_json=?,updated_at=?
                   WHERE request_id=? AND state='pending'""",
                (response_json, timestamp, request_id),
            )
            try:
                connection.execute(
                    """INSERT INTO monitor_retry_outbox
                       (request_id,task_id,run_id,state,created_at,updated_at)
                       VALUES (?,?,?,'pending',?,?)""",
                    (request_id, run["task_id"], run_id, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as error:
                if (
                    getattr(error, "sqlite_errorcode", None)
                    != sqlite3.SQLITE_CONSTRAINT_UNIQUE
                ):
                    raise
                connection.execute(
                    """UPDATE monitor_commands
                       SET response_status=409,response_json=?,updated_at=?
                       WHERE request_id=?""",
                    (conflict_response_json, timestamp, request_id),
                )
            completed = connection.execute(
                "SELECT * FROM monitor_commands WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._command(completed)

    def claim_retry_intents(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int = 20,
    ) -> list[RetryIntentRecord]:
        current = require_utc(now)
        claimed: list[RetryIntentRecord] = []
        with self._transaction(write=True) as connection:
            rows = connection.execute(
                """SELECT * FROM monitor_retry_outbox
                   WHERE state='pending'
                      OR (state='dispatching' AND lease_expires_at<=?)
                   ORDER BY created_at,request_id LIMIT ?""",
                (_timestamp(current), limit),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                connection.execute(
                    """UPDATE monitor_retry_outbox
                       SET state='dispatching',dispatch_count=dispatch_count+1,
                           lease_token=?,lease_expires_at=?,updated_at=?
                       WHERE request_id=?""",
                    (
                        token,
                        _timestamp(current + lease_for),
                        _timestamp(current),
                        row["request_id"],
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM monitor_retry_outbox WHERE request_id=?",
                    (row["request_id"],),
                ).fetchone()
                claimed.append(self._retry_intent(updated))
        return claimed

    def finish_retry_intent(
        self,
        request_id: str,
        lease_token: str,
        *,
        now: datetime,
    ) -> bool:
        with self._transaction(write=True) as connection:
            cursor = connection.execute(
                """UPDATE monitor_retry_outbox
                   SET state='dispatched',lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE request_id=? AND state='dispatching' AND lease_token=?""",
                (_timestamp(now), str(UUID(request_id)), lease_token),
            )
        return cursor.rowcount == 1

    def next_retry_intent_wakeup(self, *, now: datetime) -> datetime | None:
        current = require_utc(now)
        with self._transaction() as connection:
            pending = connection.execute(
                "SELECT 1 FROM monitor_retry_outbox WHERE state='pending' LIMIT 1"
            ).fetchone()
            if pending is not None:
                return current
            row = connection.execute(
                """SELECT MIN(lease_expires_at) AS wakeup_at
                   FROM monitor_retry_outbox WHERE state='dispatching'"""
            ).fetchone()
        return _datetime(row["wakeup_at"])

    def archive_task(
        self,
        task_id: str,
        *,
        expected_generation: int,
        now: datetime,
    ) -> TaskRecord:
        task_id = str(UUID(task_id))
        with self._transaction(write=True) as connection:
            self._assert_task_state(
                connection,
                task_id,
                expected_generation=expected_generation,
                expected_lifecycle="ended",
            )
            pending_run = connection.execute(
                """SELECT 1 FROM monitor_runs
                   WHERE task_id=? AND status IN ('queued','running') LIMIT 1""",
                (task_id,),
            ).fetchone()
            if pending_run is not None:
                raise MonitorStateConflict("monitor task still has pending runs")
            active_retry = connection.execute(
                """SELECT 1 FROM monitor_retry_outbox
                   WHERE task_id=? AND state IN ('pending','dispatching') LIMIT 1""",
                (task_id,),
            ).fetchone()
            if active_retry is not None:
                raise MonitorStateConflict("monitor task still has an active retry")
            cursor = connection.execute(
                """UPDATE monitor_tasks
                   SET lifecycle='archived',generation=generation+1,
                       scheduler_desired_state='removed',scheduler_sync_status='pending',
                       scheduler_error_json=NULL,archived_at=?,updated_at=?
                   WHERE task_id=? AND lifecycle='ended' AND generation=?""",
                (_timestamp(now), _timestamp(now), task_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise MonitorStateConflict(
                    "monitor task state changed during transition"
                )
            row = connection.execute(
                "SELECT * FROM monitor_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)

    def _insert_boundary(
        self, connection: sqlite3.Connection, task_id: str, spec: BoundarySpec, now: datetime
    ) -> str | None:
        boundary_id = str(uuid4())
        cursor = connection.execute(
            """INSERT OR IGNORE INTO monitor_boundaries
               (boundary_id,task_id,boundary_at,boundary_type,local_display_at,generation,reason,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                boundary_id, task_id, _timestamp(spec.boundary_at), spec.boundary_type.value,
                spec.boundary_at.astimezone(SHANGHAI).isoformat(),
                spec.generation, spec.reason, _timestamp(now),
            ),
        )
        if cursor.rowcount == 0:
            return None
        if spec.boundary_type in {BoundaryType.SCHEDULED, BoundaryType.PAUSE, BoundaryType.END}:
            previous = connection.execute(
                """SELECT boundary_at FROM monitor_boundaries
                   WHERE task_id=? AND boundary_at < ? ORDER BY boundary_at DESC LIMIT 1""",
                (task_id, _timestamp(spec.boundary_at)),
            ).fetchone()
            if previous is None:
                raise ValueError("report boundary has no preceding logical boundary")
            connection.execute(
                """INSERT OR IGNORE INTO monitor_runs
                   (run_id,task_id,boundary_id,generation,start_at,end_at,logical_cutoff_at,
                    boundary_type,status,errors_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), task_id, boundary_id, spec.generation, previous["boundary_at"],
                    _timestamp(spec.boundary_at), _timestamp(spec.boundary_at), spec.boundary_type.value,
                    "queued", "[]", _timestamp(now), _timestamp(now),
                ),
            )
        return boundary_id

    def append_boundaries(
        self,
        task_id: str,
        specs: list[BoundarySpec],
        now: datetime,
        *,
        expected_generation: int | None = None,
        expected_lifecycle: str | None = None,
    ) -> list[BoundaryRecord]:
        inserted: list[str] = []
        with self._transaction(write=True) as connection:
            self._assert_task_state(
                connection,
                task_id,
                expected_generation=expected_generation,
                expected_lifecycle=expected_lifecycle,
            )
            for spec in sorted(specs, key=lambda item: item.boundary_at):
                boundary_id = self._insert_boundary(connection, task_id, spec, now)
                if boundary_id is not None:
                    inserted.append(boundary_id)
            if not inserted:
                return []
            placeholders = ",".join("?" for _ in inserted)
            rows = connection.execute(
                f"SELECT * FROM monitor_boundaries WHERE boundary_id IN ({placeholders}) ORDER BY boundary_at",
                inserted,
            ).fetchall()
        return [self._boundary(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        now: datetime,
        *,
        expected_generation: int | None = None,
        expected_lifecycle: str | None = None,
        expected_scheduler_sync_status: str | None = None,
    ) -> TaskRecord:
        allowed = {
            "lifecycle", "end_at", "daily_trigger_time", "schedule_effective_at", "generation",
            "scheduler_desired_state", "scheduler_sync_status", "paused_at", "ended_at",
            "ended_reason",
            "archived_at", "last_runner_heartbeat_at", "scheduler_last_synced_at",
            "scheduler_error",
        }
        if not updates or not set(updates) <= allowed:
            raise ValueError("invalid monitor task update")
        serialized = {}
        for key, value in updates.items():
            if isinstance(value, datetime):
                serialized[key] = _timestamp(value)
            elif key == "scheduler_error":
                key = "scheduler_error_json"
                serialized[key] = (
                    json.dumps(
                        value.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if value is not None
                    else None
                )
            else:
                serialized[key] = value
        assignments = ",".join(f"{key}=?" for key in serialized)
        with self._transaction(write=True) as connection:
            self._assert_task_state(
                connection,
                task_id,
                expected_generation=expected_generation,
                expected_lifecycle=expected_lifecycle,
                expected_scheduler_sync_status=expected_scheduler_sync_status,
            )
            cursor = connection.execute(
                f"UPDATE monitor_tasks SET {assignments},updated_at=? WHERE task_id=?",
                (*serialized.values(), _timestamp(now), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
            row = connection.execute("SELECT * FROM monitor_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task(row)

    def transition_task(
        self,
        task_id: str,
        *,
        boundaries: list[BoundarySpec],
        updates: dict[str, Any],
        now: datetime,
        expected_generation: int | None = None,
        expected_lifecycle: str | None = None,
        expected_scheduler_sync_status: str | None = None,
    ) -> TaskRecord:
        """Atomically append lifecycle boundaries and persist the new task expectation."""
        allowed = {
            "lifecycle", "end_at", "daily_trigger_time", "schedule_effective_at", "generation",
            "scheduler_desired_state", "scheduler_sync_status", "paused_at", "ended_at",
            "ended_reason",
            "archived_at", "last_runner_heartbeat_at", "scheduler_last_synced_at",
            "scheduler_error",
        }
        if not updates or not set(updates) <= allowed:
            raise ValueError("invalid monitor task transition")
        serialized = {}
        for key, value in updates.items():
            if isinstance(value, datetime):
                serialized[key] = _timestamp(value)
            elif key == "scheduler_error":
                key = "scheduler_error_json"
                serialized[key] = (
                    json.dumps(
                        value.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if value is not None
                    else None
                )
            else:
                serialized[key] = value
        assignments = ",".join(f"{key}=?" for key in serialized)
        with self._transaction(write=True) as connection:
            self._assert_task_state(
                connection,
                task_id,
                expected_generation=expected_generation,
                expected_lifecycle=expected_lifecycle,
                expected_scheduler_sync_status=expected_scheduler_sync_status,
            )
            for spec in sorted(boundaries, key=lambda item: item.boundary_at):
                self._insert_boundary(connection, task_id, spec, now)
            cursor = connection.execute(
                f"UPDATE monitor_tasks SET {assignments},updated_at=? WHERE task_id=?",
                (*serialized.values(), _timestamp(now), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
            row = connection.execute(
                "SELECT * FROM monitor_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row)

    @staticmethod
    def _assert_task_state(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        expected_generation: int | None,
        expected_lifecycle: str | None,
        expected_scheduler_sync_status: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT generation,lifecycle,scheduler_sync_status "
            "FROM monitor_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        if (
            expected_generation is not None and row["generation"] != expected_generation
        ) or (
            expected_lifecycle is not None and row["lifecycle"] != expected_lifecycle
        ) or (
            expected_scheduler_sync_status is not None
            and row["scheduler_sync_status"] != expected_scheduler_sync_status
        ):
            raise MonitorStateConflict("monitor task state changed during transition")

    def latest_boundary(self, task_id: str) -> BoundaryRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_boundaries WHERE task_id=? ORDER BY boundary_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._boundary(row)

    def list_boundaries(self, task_id: str) -> list[BoundaryRecord]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_boundaries WHERE task_id=? ORDER BY boundary_at,boundary_type",
                (task_id,),
            ).fetchall()
        return [self._boundary(row) for row in rows]

    def list_runs(self, task_id: str) -> list[RunRecord]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_runs WHERE task_id=? ORDER BY end_at", (task_id,)
            ).fetchall()
        return [self._run(row) for row in rows]

    def list_run_page(
        self,
        task_id: str,
        *,
        limit: int,
        before_end_at: datetime | None = None,
        before_run_id: str | None = None,
    ) -> list[RunRecord]:
        if limit < 1:
            raise ValueError("run page limit must be positive")
        parameters: list[Any] = [str(UUID(task_id))]
        cursor_sql = ""
        if before_end_at is not None or before_run_id is not None:
            if before_end_at is None or before_run_id is None:
                raise ValueError("run cursor requires both sort values")
            cursor_time = _timestamp(before_end_at)
            cursor_sql = " AND (end_at<? OR (end_at=? AND run_id<?))"
            parameters.extend(
                (cursor_time, cursor_time, str(UUID(before_run_id)))
            )
        parameters.append(limit)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_runs WHERE task_id=?"
                + cursor_sql
                + " ORDER BY end_at DESC,run_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._run(row) for row in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run(row) if row is not None else None

    def get_publication(self, run_id: str) -> PublicationRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_run_publications WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._publication(row) if row is not None else None

    def prepare_publication(
        self,
        run_id: str,
        lease_token: str,
        *,
        now: datetime,
        status: Literal["succeeded", "partial"],
        start_revision: int,
        end_revision: int,
        summary: dict[str, Any],
        errors: list[MonitorPublicErrorPayload],
        report_ref: str,
        json_sha256: str,
        html_sha256: str,
        report_expires_at: datetime,
    ) -> PublicationRecord:
        current = require_utc(now)
        summary_payload = MonitorRunSummaryPayload.model_validate(summary)
        if status == "succeeded" and errors:
            raise ValueError("succeeded publication cannot contain public errors")
        if summary_payload.error_count != len(errors):
            raise ValueError("publication summary error_count must match errors")
        values = {
            "status": status,
            "start_revision": start_revision,
            "end_revision": end_revision,
            "summary_json": json.dumps(
                summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
            "errors_json": _errors_json(errors),
            "report_ref": report_ref,
            "json_sha256": json_sha256,
            "html_sha256": html_sha256,
            "report_expires_at": _timestamp(report_expires_at),
        }
        with self._transaction(write=True) as connection:
            run = connection.execute(
                "SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            expires = _datetime(run["lease_expires_at"]) if run is not None else None
            if (
                run is None
                or run["status"] != "running"
                or run["lease_token"] != lease_token
                or expires is None
                or expires <= current
            ):
                raise MonitorLeaseLost("monitor run lease was lost")
            existing = connection.execute(
                "SELECT * FROM monitor_run_publications WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO monitor_run_publications (
                       run_id,task_id,state,status,start_revision,end_revision,
                       summary_json,errors_json,report_ref,json_sha256,html_sha256,
                       report_expires_at,created_at,updated_at
                       ) VALUES (?,?, 'prepared', ?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        run["task_id"],
                        values["status"],
                        values["start_revision"],
                        values["end_revision"],
                        values["summary_json"],
                        values["errors_json"],
                        values["report_ref"],
                        values["json_sha256"],
                        values["html_sha256"],
                        values["report_expires_at"],
                        _timestamp(current),
                        _timestamp(current),
                    ),
                )
            else:
                comparable = {
                    key: existing[key]
                    for key in values
                }
                if comparable != values:
                    raise MonitorStateConflict(
                        "prepared report publication does not match the existing manifest"
                    )
            prepared = connection.execute(
                "SELECT * FROM monitor_run_publications WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._publication(prepared)

    def list_due_runs(self, task_id: str, now: datetime) -> list[RunRecord]:
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM monitor_runs WHERE task_id=? AND end_at<=?
                   AND status IN ('queued','running','failed') ORDER BY end_at""",
                (task_id, _timestamp(now)),
            ).fetchall()
        return [self._run(row) for row in rows]

    def automatic_retry_count(self, run_id: str) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT COUNT(*) FROM monitor_run_attempts
                   WHERE run_id=? AND trigger='automatic_retry'""",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def attempts(self, run_id: str) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_run_attempts WHERE run_id=? ORDER BY attempt", (run_id,)
            ).fetchall()
        return [
            {
                "attempt": row["attempt"], "trigger": row["trigger"], "status": row["status"],
                "started_at": _datetime(row["started_at"]), "finished_at": _datetime(row["finished_at"]),
                "errors": _errors(row["errors_json"]),
            }
            for row in rows
        ]

    def attempts_for_runs(
        self, run_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        normalized = [str(UUID(run_id)) for run_id in dict.fromkeys(run_ids)]
        result = {run_id: [] for run_id in normalized}
        if not normalized:
            return result
        placeholders = ",".join("?" for _ in normalized)
        with self._transaction() as connection:
            rows = connection.execute(
                f"""SELECT * FROM monitor_run_attempts
                    WHERE run_id IN ({placeholders}) ORDER BY run_id,attempt""",
                normalized,
            ).fetchall()
        for row in rows:
            result[row["run_id"]].append(
                {
                    "attempt": row["attempt"],
                    "trigger": row["trigger"],
                    "status": row["status"],
                    "started_at": _datetime(row["started_at"]),
                    "finished_at": _datetime(row["finished_at"]),
                    "errors": _errors(row["errors_json"]),
                }
            )
        return result

    def claim_run(
        self,
        run_id: str,
        *,
        now: datetime,
        lease_for: timedelta,
        trigger: Literal["scheduled", "automatic_retry", "manual_retry"],
    ) -> LeaseClaim | None:
        current = require_utc(now)
        with self._transaction(write=True) as connection:
            row = connection.execute(
                """SELECT r.*,t.lifecycle AS task_lifecycle
                   FROM monitor_runs r JOIN monitor_tasks t ON t.task_id=r.task_id
                   WHERE r.run_id=?""",
                (run_id,),
            ).fetchone()
            if row is None or row["status"] in {"succeeded", "partial"}:
                return None
            if row["task_lifecycle"] == "archived":
                return None
            actual_trigger = trigger
            if row["status"] == "running":
                expires = _datetime(row["lease_expires_at"])
                if expires is not None and expires > current:
                    return None
                crash_error = MonitorPublicErrorPayload(
                    code="MONITOR_INTERNAL_ERROR", stage="report_publish",
                    message="上一次执行租约已过期，可安全重试", retryable=True,
                )
                connection.execute(
                    """UPDATE monitor_run_attempts SET status='failed',finished_at=?,errors_json=?
                       WHERE run_id=? AND attempt=? AND status='running'""",
                    (_timestamp(current), _errors_json([crash_error]), run_id, row["attempt_count"]),
                )
                automatic_count = connection.execute(
                    """SELECT COUNT(*) FROM monitor_run_attempts
                       WHERE run_id=? AND trigger='automatic_retry'""",
                    (run_id,),
                ).fetchone()[0]
                if automatic_count >= 3:
                    connection.execute(
                        """UPDATE monitor_runs SET status='failed',lease_token=NULL,
                           lease_expires_at=NULL,errors_json=?,finished_at=?,updated_at=?
                           WHERE run_id=?""",
                        (
                            _errors_json([crash_error]), _timestamp(current),
                            _timestamp(current), run_id,
                        ),
                    )
                    return None
                actual_trigger = "automatic_retry"
            elif row["status"] == "failed" and trigger == "scheduled":
                return None

            if actual_trigger == "automatic_retry":
                retryable = row["status"] == "running" or any(
                    error.retryable for error in _errors(row["errors_json"])
                )
                automatic_count = connection.execute(
                    """SELECT COUNT(*) FROM monitor_run_attempts
                       WHERE run_id=? AND trigger='automatic_retry'""",
                    (run_id,),
                ).fetchone()[0]
                if not retryable or automatic_count >= 3:
                    return None

            attempt = row["attempt_count"] + 1
            token = secrets.token_urlsafe(32)
            connection.execute(
                """UPDATE monitor_runs SET status='running',attempt_count=?,lease_token=?,
                   lease_expires_at=?,started_at=COALESCE(started_at,?),finished_at=NULL,
                   errors_json='[]',updated_at=? WHERE run_id=?""",
                (
                    attempt, token, _timestamp(current + lease_for), _timestamp(current),
                    _timestamp(current), run_id,
                ),
            )
            connection.execute(
                """INSERT INTO monitor_run_attempts
                   (run_id,attempt,trigger,status,started_at,errors_json)
                   VALUES (?,?,?,'running',?,'[]')""",
                (run_id, attempt, actual_trigger, _timestamp(current)),
            )
            claimed = connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        return LeaseClaim(self._run(claimed), token, attempt, actual_trigger)

    def renew_lease(
        self, run_id: str, lease_token: str, *, now: datetime, lease_for: timedelta
    ) -> bool:
        current = require_utc(now)
        with self._transaction(write=True) as connection:
            cursor = connection.execute(
                """UPDATE monitor_runs SET lease_expires_at=?,updated_at=?
                   WHERE run_id=? AND status='running' AND lease_token=?
                   AND lease_expires_at>?""",
                (
                    _timestamp(current + lease_for),
                    _timestamp(current),
                    run_id,
                    lease_token,
                    _timestamp(current),
                ),
            )
        return cursor.rowcount == 1

    def finish_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        now: datetime,
        status: Literal["succeeded", "partial", "failed"],
        errors: list[MonitorPublicErrorPayload],
        start_revision: int | None = None,
        end_revision: int | None = None,
        summary: dict[str, Any] | None = None,
        report_ref: str | None = None,
        report_sha256: str | None = None,
        report_expires_at: datetime | None = None,
    ) -> RunRecord:
        published = status in {"succeeded", "partial"}
        metadata = (
            start_revision, end_revision, summary,
            report_ref, report_sha256, report_expires_at,
        )
        if published and not all(value is not None for value in metadata):
            raise ValueError("published result metadata must be complete")
        if not published and any(value is not None for value in metadata):
            raise ValueError("unpublished result cannot contain report metadata")
        if status == "succeeded" and errors:
            raise ValueError("succeeded result cannot contain public errors")
        if status == "failed" and not errors:
            raise ValueError("failed result requires public errors")
        if published:
            summary_payload = MonitorRunSummaryPayload.model_validate(summary)
            if summary_payload.error_count != len(errors):
                raise ValueError("published summary error_count must match public errors")
        with self._transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
            expires = _datetime(row["lease_expires_at"]) if row is not None else None
            current = require_utc(now)
            if (
                row is None
                or row["status"] != "running"
                or row["lease_token"] != lease_token
                or expires is None
                or expires <= current
            ):
                raise MonitorLeaseLost("monitor run lease was lost")
            connection.execute(
                """UPDATE monitor_run_attempts SET status=?,finished_at=?,errors_json=?
                   WHERE run_id=? AND attempt=? AND status='running'""",
                (status, _timestamp(now), _errors_json(errors), run_id, row["attempt_count"]),
            )
            connection.execute(
                """UPDATE monitor_runs SET status=?,lease_token=NULL,lease_expires_at=NULL,
                   start_revision=?,end_revision=?,summary_json=?,report_ref=?,report_sha256=?,
                   report_expires_at=?,errors_json=?,finished_at=?,updated_at=? WHERE run_id=?""",
                (
                    status, start_revision, end_revision,
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")) if summary else None,
                    report_ref, report_sha256,
                    _timestamp(report_expires_at) if report_expires_at else None,
                    _errors_json(errors), _timestamp(now), _timestamp(now), run_id,
                ),
            )
            finished = connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run(finished)

    def finalize_publication(
        self,
        run_id: str,
        lease_token: str,
        *,
        now: datetime,
    ) -> RunRecord:
        """Atomically activate a prepared manifest and its public Run metadata."""
        current = require_utc(now)
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            expires = _datetime(row["lease_expires_at"]) if row is not None else None
            if (
                row is None
                or row["status"] != "running"
                or row["lease_token"] != lease_token
                or expires is None
                or expires <= current
            ):
                raise MonitorLeaseLost("monitor run lease was lost")
            manifest_row = connection.execute(
                "SELECT * FROM monitor_run_publications WHERE run_id=?", (run_id,)
            ).fetchone()
            if manifest_row is None or manifest_row["state"] != "prepared":
                raise MonitorStateConflict("prepared report publication is unavailable")
            manifest = self._publication(manifest_row)
            connection.execute(
                """UPDATE monitor_run_attempts SET status=?,finished_at=?,errors_json=?
                   WHERE run_id=? AND attempt=? AND status='running'""",
                (
                    manifest.status,
                    _timestamp(current),
                    _errors_json(manifest.errors),
                    run_id,
                    row["attempt_count"],
                ),
            )
            connection.execute(
                """UPDATE monitor_runs SET status=?,lease_token=NULL,lease_expires_at=NULL,
                   start_revision=?,end_revision=?,summary_json=?,report_ref=?,report_sha256=?,
                   report_expires_at=?,errors_json=?,finished_at=?,updated_at=? WHERE run_id=?""",
                (
                    manifest.status,
                    manifest.start_revision,
                    manifest.end_revision,
                    json.dumps(
                        manifest.summary,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    manifest.report_ref,
                    manifest.json_sha256,
                    _timestamp(manifest.report_expires_at),
                    _errors_json(manifest.errors),
                    _timestamp(current),
                    _timestamp(current),
                    run_id,
                ),
            )
            connection.execute(
                """UPDATE monitor_run_publications SET state='activated',updated_at=?
                   WHERE run_id=? AND state='prepared'""",
                (_timestamp(current), run_id),
            )
            finished = connection.execute(
                "SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run(finished)

    def heartbeat(self, task_id: str, now: datetime) -> None:
        self.update_task(task_id, {"last_runner_heartbeat_at": now}, now)
