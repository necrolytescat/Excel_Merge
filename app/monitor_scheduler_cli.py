"""Explicit install, removal and diagnostics for M3 Windows scheduler tasks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from uuid import UUID

from app.services.monitor_store import DEFAULT_DATABASE_PATH, MonitorStore
from app.services.windows_scheduler import (
    MAINTENANCE_TASK_NAME,
    TEST_TASK_PREFIX,
    MonitorSchedulerService,
    SchedulerGatewayError,
    WindowsSchedulerGateway,
)


def _service(database: str, workdir: str) -> MonitorSchedulerService:
    path = Path(database).resolve()
    return MonitorSchedulerService(
        MonitorStore(path),
        WindowsSchedulerGateway(),
        database_path=path,
        working_directory=Path(workdir).resolve(),
        python_executable=Path(sys.executable).resolve(),
    )


def _output(**values: object) -> None:
    print(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _isolated_database(test_id: str | UUID) -> Path:
    canonical = str(UUID(str(test_id))).lower()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    state_root = (temporary_root / f"{TEST_TASK_PREFIX}{canonical}").resolve()
    if temporary_root not in state_root.parents:
        raise ValueError("isolated test state escaped the system temp root")
    return state_root / "monitor.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage M3 Windows scheduler tasks")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--workdir", default=str(Path.cwd()))
    commands = parser.add_subparsers(dest="command", required=True)
    maintenance = commands.add_parser("maintenance")
    maintenance.add_argument("operation", choices=("ensure", "inspect", "delete"))
    sync = commands.add_parser("sync-task")
    sync.add_argument("--task-id", required=True)
    sync.add_argument("--generation", required=True, type=int)
    isolated = commands.add_parser("isolated-test")
    isolated.add_argument("operation", choices=("ensure-run", "inspect", "delete"))
    isolated.add_argument("--test-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "maintenance":
            service = _service(args.database, args.workdir)
            if args.operation == "ensure":
                result = service.ensure_maintenance()
                _output(name=MAINTENANCE_TASK_NAME, valid=result.valid)
                return 0
            if args.operation == "inspect":
                result = service.inspect_maintenance()
                _output(
                    name=MAINTENANCE_TASK_NAME,
                    valid=result.valid,
                    drift_fields=result.drift_fields,
                )
                return 0 if result.valid else 1
            actual = service.delete_maintenance()
            _output(name=MAINTENANCE_TASK_NAME, exists=actual.exists)
            return 0 if not actual.exists else 1
        if args.command == "sync-task":
            service = _service(args.database, args.workdir)
            task_id = str(UUID(args.task_id))
            if args.generation <= 0:
                parser.error("--generation must be positive")
            result = service.sync_task(
                task_id, expected_generation=args.generation
            )
            _output(
                task_id=task_id,
                generation=result.generation,
                status=result.status,
                stale=result.stale,
            )
            return 0 if result.status not in {"error", "stale"} else 1

        test_id = str(UUID(args.test_id)).lower()
        name = f"{TEST_TASK_PREFIX}{test_id}"
        test_database = _isolated_database(test_id)
        service = _service(str(test_database), str(PROJECT_ROOT))
        if args.operation == "inspect":
            actual = service.gateway.inspect(name)
            validation = (
                service.gateway.validate(
                    service.maintenance_expected(name=name), actual
                )
                if actual.exists
                else None
            )
            _output(
                name=name,
                exists=actual.exists,
                valid=validation.valid if validation is not None else False,
            )
            return 0
        if args.operation == "delete":
            remaining = service.delete_maintenance(name=name)
            _output(name=name, exists=remaining.exists)
            return 0 if not remaining.exists else 1
        before = service.gateway.inspect(name)
        if before.exists:
            raise SchedulerGatewayError("isolated test task already exists")
        try:
            installed = service.ensure_maintenance(name=name)
            service.gateway.run_now(name)
            _output(name=name, installed=installed.valid, triggered=True)
            return 0
        finally:
            service.delete_maintenance(name=name)
            remaining = service.gateway.inspect(name)
            if remaining.exists:
                raise SchedulerGatewayError("isolated scheduler cleanup failed")
    except (SchedulerGatewayError, OSError, ValueError):
        _output(error="MONITOR_SCHEDULER_SYNC_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
