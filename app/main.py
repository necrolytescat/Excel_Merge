"""M0 本地 Web 入口。

运行：
    python -m app.main
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.svn_provider import SVNProviderError, provider_from_config

from app.api import batch, diff, diff_plan, health, monitor, operations, replay, svn
from app.monitor_runner import build_runner
from app.services.branch_history_service import BranchHistoryService
from app.services.monitor_endpoint_catalog import MonitorEndpointCatalog
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
from app.services.monitor_store import MonitorStore
from app.services.monitor_task_service import MonitorTaskService
from app.services.monitor_web_service import MonitorWebError, MonitorWebService
from app.services.windows_scheduler import (
    MonitorSchedulerService,
    ScheduledMonitorTaskService,
    WindowsSchedulerGateway,
)
from app.services.workbook_dataset_service import (
    SVNWorkbookDatasetResolver,
    UnavailableWorkbookDatasetResolver,
    WorkbookCompareError,
    WorkbookDatasetResolver,
)
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from app.services.svn_service import SVNService
from app.services.snapshot_content_cache import PersistentSnapshotContentCache
from app.services.snapshot_service import SnapshotService
from app.services.config_service import ConfigStore
from app.services.batch_diff_service import (
    BatchDiffService,
    DefaultBatchWorkbookRunner,
    SnapshotBatchCandidateResolver,
)
from app.services.batch_store import BatchDiffError, BatchStore
from app.services.diff_plan_service import DiffPlanService, DiffPlanWorkbookCatalogService
from app.services.diff_plan_store import DiffPlanError, DiffPlanStore
from app.services.diff_plan_run_store import DiffPlanRunStore
from app.services.diff_plan_run_service import DiffPlanRunService
from app.services.workbook_execution_gate import WorkbookExecutionGate
from app.services.workbook_execution_scheduler import PersistentWorkbookExecutionScheduler
from app.services.offline_fixture import OfflineFixtureError, OfflineFixtureService
from app.services.operations_service import (
    OperationalLogService,
    OperationsError,
    SVNCacheService,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "config" / "settings.m0.example.json"
DEFAULT_ENDPOINT_REGISTRY: list[dict[str, Any]] = []
TASK_PATH_PATTERN = re.compile(
    r"/api/diff/batches/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})(?:/|$)",
    re.IGNORECASE,
)

DEFAULT_ENDPOINT_CATALOG = {
    "TC": {"display_name": "港台 TC", "trunk_branch": "Trunk_TC", "fix_pattern": "TC-fix-x.x.x.x"},
    "KR": {"display_name": "韩国 KR", "trunk_branch": "Trunk_KR", "fix_pattern": "KR-fix-x.x.x.x"},
    "BT": {"display_name": "折扣 BT", "trunk_branch": "", "fix_pattern": ""},
    "JP": {"display_name": "日本 JP", "trunk_branch": "", "fix_pattern": ""},
}


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def initialize_default_config() -> dict[str, Any]:
    store = ConfigStore(DEFAULT_CONFIG_PATH)
    store.initialize_from(DEFAULT_CONFIG_TEMPLATE_PATH)
    return store.read()


def create_app(
    *,
    config: dict[str, Any] | None = None,
    provider=None,
    workbook_dataset_resolver: WorkbookDatasetResolver | None = None,
    batch_diff_service: BatchDiffService | None = None,
    workbook_diff_service: WorkbookDiffService | None = None,
    monitor_web_service: MonitorWebService | None = None,
    diff_plan_service: DiffPlanService | None = None,
    diff_plan_run_service: DiffPlanRunService | None = None,
) -> FastAPI:
    config = config if config is not None else load_config()
    configured_provider = os.environ.get("EXCEL_MERGE_SVN_PROVIDER", "").strip()
    if configured_provider:
        config = dict(config)
        config["svn"] = {
            **(config.get("svn", {}) if isinstance(config.get("svn", {}), dict) else {}),
            "provider": configured_provider,
        }
    svn_config = config.get("svn", {}) if isinstance(config, dict) else {}
    web_config = config.get("web", {}) if isinstance(config, dict) else {}
    operations_config = config.get("operations", {}) if isinstance(config, dict) else {}
    logging_config = (
        operations_config.get("logging", {})
        if isinstance(operations_config.get("logging", {}), dict)
        else {}
    )
    provider = provider or provider_from_config(config)
    provider_name = str(svn_config.get("provider", "mock")).lower()
    credential_source = str(svn_config.get("credential_source", "svn_cli_cache"))
    allowed_schemes = tuple(svn_config.get("allowed_schemes", ("http", "https", "svn", "svn+ssh", "file")))
    preview_limit = int(svn_config.get("content_preview_max_bytes", 262144))
    max_workers = int(config.get("max_workers", 6)) if isinstance(config, dict) else 6
    snapshot_reuse_config = (
        config.get("snapshot_reuse", {})
        if isinstance(config.get("snapshot_reuse", {}), dict)
        else {}
    )
    manifest_parser_config = (
        config.get("manifest_parser", {})
        if isinstance(config.get("manifest_parser", {}), dict)
        else {}
    )
    ooxml_manifest_first = bool(
        manifest_parser_config.get("ooxml_first_enabled", True)
    )
    frozen_dataset_enabled = bool(
        snapshot_reuse_config.get("frozen_dataset_enabled", False)
    )
    cross_branch_csv_reuse_enabled = bool(
        snapshot_reuse_config.get("cross_branch_csv_reuse_enabled", False)
    )
    persistent_cache_config = (
        snapshot_reuse_config.get("persistent_cache", {})
        if isinstance(snapshot_reuse_config.get("persistent_cache", {}), dict)
        else {}
    )
    configured_snapshot_cache = (
        os.environ.get("EXCEL_MERGE_SNAPSHOT_CACHE_DIR")
        or str(persistent_cache_config.get("directory", ".cache/snapshot")).strip()
    )
    snapshot_cache_directory = (
        Path(configured_snapshot_cache) if configured_snapshot_cache else None
    )
    if (
        snapshot_cache_directory is not None
        and not snapshot_cache_directory.is_absolute()
    ):
        snapshot_cache_directory = PROJECT_ROOT / snapshot_cache_directory
    configured_persistent_enabled = persistent_cache_config.get("enabled")
    persistent_cache_enabled = (
        provider_name == "cli"
        if configured_persistent_enabled is None
        else bool(configured_persistent_enabled)
    )
    snapshot_content_cache = PersistentSnapshotContentCache(
        snapshot_cache_directory,
        enabled=persistent_cache_enabled,
        max_bytes=int(
            persistent_cache_config.get("max_bytes", 2 * 1024 * 1024 * 1024)
        ),
        max_file_entries=int(
            persistent_cache_config.get("max_file_entries", 20_000)
        ),
        max_tree_entries=int(
            persistent_cache_config.get("max_tree_entries", 256)
        ),
    )

    app = FastAPI(title="Excel Diff/Merge SVN 基座", version="0.1.0")
    app.state.provider = provider
    app.state.snapshot_content_cache = snapshot_content_cache
    app.state.provider_name = provider_name
    app.state.configured_provider = provider_name
    app.state.provider_locked = bool(configured_provider)
    app.state.credential_source = credential_source
    app.state.default_url = str(svn_config.get("server_url", ""))
    app.state.config_store = ConfigStore(DEFAULT_CONFIG_PATH)
    app.state.endpoint_catalog = svn_config.get("endpoint_catalog") or DEFAULT_ENDPOINT_CATALOG
    app.state.endpoint_registry = SnapshotService.normalize_registry(svn_config.get("endpoint_registry") or DEFAULT_ENDPOINT_REGISTRY)
    app.state.svn_service = SVNService(provider, allowed_schemes=allowed_schemes, preview_limit=preview_limit)
    app.state.snapshot_service = SnapshotService(
        provider,
        allowed_schemes=allowed_schemes,
        max_workers=max_workers,
        content_read_workers=int(
            snapshot_reuse_config.get("content_read_workers", 12)
        ),
        bulk_export_enabled=bool(
            snapshot_reuse_config.get("bulk_export_enabled", True)
        ),
        bulk_export_min_files=int(
            snapshot_reuse_config.get("bulk_export_min_files", 8)
        ),
        bulk_export_min_ratio=float(
            snapshot_reuse_config.get("bulk_export_min_ratio", 0.5)
        ),
        preview_limit=preview_limit,
        reuse_ttl_seconds=float(snapshot_reuse_config.get("ttl_seconds", 300)),
        reuse_max_entries=int(snapshot_reuse_config.get("max_entries", 8)),
        reuse_configuration={
            "dataset_layout": config.get("dataset_layout"),
            "manifest_parser": {
                "ooxml_first_enabled": ooxml_manifest_first,
            },
            "frozen_dataset": {
                "enabled": frozen_dataset_enabled,
                "cross_branch_csv_reuse_enabled": cross_branch_csv_reuse_enabled,
            },
        },
        persistent_content_cache=snapshot_content_cache,
        phase_timing_enabled=bool(logging_config.get("enabled", True)),
    )
    diff_plan_config = config.get("diff_plan", {}) if isinstance(config, dict) else {}
    configured_diff_plan_db = os.environ.get("EXCEL_MERGE_DIFF_PLAN_DB") or str(
        diff_plan_config.get("database_path", "var/m4-diff-plan/diff-plan.sqlite3")
    )
    diff_plan_database = Path(configured_diff_plan_db)
    if not diff_plan_database.is_absolute():
        diff_plan_database = PROJECT_ROOT / diff_plan_database
    diff_plan_store = diff_plan_service.store if diff_plan_service is not None else DiffPlanStore(diff_plan_database)
    app.state.diff_plan_service = diff_plan_service or DiffPlanService(
        diff_plan_store,
        DiffPlanWorkbookCatalogService(
            provider,
            app.state.snapshot_service,
            lambda: getattr(app.state, "endpoint_registry", []),
        ),
        lambda: getattr(app.state, "endpoint_registry", []),
    )
    app.state.monitor_endpoint_catalog = MonitorEndpointCatalog(
        app.state.svn_service,
        server_url=lambda: str(getattr(app.state, "default_url", "")),
        endpoint_catalog=lambda: getattr(app.state, "endpoint_catalog", {}),
        endpoint_registry=lambda: getattr(app.state, "endpoint_registry", []),
    )
    app.state.offline_fixture_service = (
        OfflineFixtureService() if bool(web_config.get("dev_mode", False)) else None
    )
    configured_log_dir = os.environ.get("EXCEL_MERGE_LOG_DIR") or str(
        logging_config.get("directory", "var/logs")
    ).strip()
    log_directory = Path(configured_log_dir or "var/logs")
    if not log_directory.is_absolute():
        log_directory = PROJECT_ROOT / log_directory
    app.state.operational_log_service = OperationalLogService(
        log_directory,
        max_bytes=int(logging_config.get("max_bytes", 5 * 1024 * 1024)),
        retention_days=int(logging_config.get("retention_days", 14)),
        max_files=int(logging_config.get("max_files", 200)),
        max_scan_bytes=int(logging_config.get("max_scan_bytes", 64 * 1024 * 1024)),
    )
    app.state.operations_logging_enabled = bool(logging_config.get("enabled", True))

    configured_cache_dir = os.environ.get("EXCEL_MERGE_SVN_CACHE_DIR")
    if configured_cache_dir is None:
        configured_cache_dir = svn_config.get("cache_dir", ".cache/svn")
    cache_directory = Path(str(configured_cache_dir)) if configured_cache_dir else None
    if cache_directory is not None and not cache_directory.is_absolute():
        cache_directory = PROJECT_ROOT / cache_directory
    svn_client = getattr(provider, "client", None)
    if svn_client is not None and cache_directory is not None:
        svn_client.cache_dir = str(cache_directory)
    app.state.svn_cache_service = SVNCacheService(
        cache_directory,
        client=svn_client,
        enabled=provider_name == "cli" and cache_directory is not None,
        allow_clear=bool(operations_config.get("allow_cache_clear", True)),
        excluded_roots=(
            PROJECT_ROOT / "var" / "m2-fixtures",
            PROJECT_ROOT / "var" / "m2-batch",
            diff_plan_database.parent,
        ),
    )
    dataset_layout = config.get("dataset_layout") if isinstance(config, dict) else None
    app.state.workbook_diff_service = workbook_diff_service or (
        WorkbookDiffService(
            DatasetLayout.from_config(dataset_layout),
            ooxml_first=ooxml_manifest_first,
        )
        if isinstance(dataset_layout, dict)
        else None
    )
    if workbook_dataset_resolver is not None:
        app.state.workbook_dataset_resolver = workbook_dataset_resolver
    elif isinstance(dataset_layout, dict):
        app.state.workbook_dataset_resolver = SVNWorkbookDatasetResolver(
            provider,
            lambda: getattr(app.state, "endpoint_registry", []),
            dataset_layout,
            allowed_schemes=allowed_schemes,
            snapshot_content_reader=app.state.snapshot_service.read_cached_snapshot_bytes,
            snapshot_content_lookup=(
                app.state.snapshot_service.lookup_cached_snapshot_file
                if frozen_dataset_enabled
                else None
            ),
            snapshot_service=app.state.snapshot_service if frozen_dataset_enabled else None,
            cross_branch_csv_reuse_enabled=cross_branch_csv_reuse_enabled,
            ooxml_first=ooxml_manifest_first,
        )
    else:
        app.state.workbook_dataset_resolver = UnavailableWorkbookDatasetResolver()

    workbook_execution_config = (
        config.get("workbook_execution", {})
        if isinstance(config.get("workbook_execution", {}), dict)
        else {}
    )
    four_way_concurrency_enabled = bool(
        workbook_execution_config.get("four_way_enabled", False)
    )
    workbook_concurrency = (
        int(workbook_execution_config.get("global_slots", 4))
        if four_way_concurrency_enabled
        else int(diff_plan_config.get("workbook_concurrency", 2))
    )
    workbook_execution_gate = WorkbookExecutionGate(workbook_concurrency)
    workbook_execution_scheduler = None
    if four_way_concurrency_enabled:
        execution_database = Path(
            str(
                workbook_execution_config.get(
                    "database_path",
                    "var/workbook-execution/execution.sqlite3",
                )
            )
        )
        if not execution_database.is_absolute():
            execution_database = PROJECT_ROOT / execution_database
        workbook_execution_scheduler = PersistentWorkbookExecutionScheduler(
            execution_database,
            workbook_execution_gate,
            global_limit=workbook_concurrency,
            per_flow_limit=int(
                workbook_execution_config.get("per_task_slots", 4)
            ),
            lease_seconds=float(
                workbook_execution_config.get("lease_seconds", 60)
            ),
        )
    app.state.workbook_execution_scheduler = workbook_execution_scheduler

    if batch_diff_service is not None:
        app.state.batch_diff_service = batch_diff_service
    elif (
        isinstance(dataset_layout, dict)
        and app.state.workbook_diff_service is not None
        and not isinstance(
            app.state.workbook_dataset_resolver,
            UnavailableWorkbookDatasetResolver,
        )
    ):
        batch_config = config.get("batch_diff", {}) if isinstance(config, dict) else {}
        configured_state = os.environ.get("EXCEL_MERGE_BATCH_STATE_DIR") or str(
            batch_config.get("state_directory", "")
        ).strip()
        state_directory = (
            Path(configured_state)
            if configured_state
            else PROJECT_ROOT / "var" / "m2-batch"
        )
        if not state_directory.is_absolute():
            state_directory = PROJECT_ROOT / state_directory
        candidate_resolver = SnapshotBatchCandidateResolver(
            app.state.snapshot_service,
            lambda: getattr(app.state, "endpoint_registry", []),
            dataset_preparer=(
                getattr(app.state.workbook_dataset_resolver, "prepare_frozen_pair", None)
                if frozen_dataset_enabled
                else None
            ),
        )
        workbook_runner = DefaultBatchWorkbookRunner(
            app.state.workbook_dataset_resolver,
            app.state.workbook_diff_service,
            None if workbook_execution_scheduler is not None else workbook_execution_gate,
        )
        app.state.batch_diff_service = BatchDiffService(
            BatchStore(
                state_directory,
                event_retention_days=int(
                    batch_config.get("event_retention_days", 90)
                ),
            ),
            candidate_resolver,
            workbook_runner,
            execution_scheduler=workbook_execution_scheduler,
            item_concurrency=workbook_concurrency,
        )
    else:
        app.state.batch_diff_service = None

    app.state.diff_plan_run_service = diff_plan_run_service
    if app.state.diff_plan_run_service is None and (
        isinstance(dataset_layout, dict)
        and app.state.workbook_diff_service is not None
        and not isinstance(app.state.workbook_dataset_resolver, UnavailableWorkbookDatasetResolver)
    ):
        results_directory = diff_plan_database.parent / "results"
        m4_runner = DefaultBatchWorkbookRunner(
            app.state.workbook_dataset_resolver,
            app.state.workbook_diff_service,
            None if workbook_execution_scheduler is not None else workbook_execution_gate,
        )
        m4_run_store = DiffPlanRunStore(
            diff_plan_database,
            results_directory,
            retention_days=int(diff_plan_config.get("detail_retention_days", 30)),
        )
        app.state.diff_plan_run_service = DiffPlanRunService(
            plan_store=diff_plan_store,
            run_store=m4_run_store,
            snapshot_service=app.state.snapshot_service,
            provider=provider,
            endpoint_registry=lambda: getattr(app.state, "endpoint_registry", []),
            workbook_runner=m4_runner,
            cleanup_interval_seconds=float(diff_plan_config.get("cleanup_interval_seconds", 3600)),
            execution_scheduler=workbook_execution_scheduler,
            item_concurrency=workbook_concurrency,
        )
        app.state.diff_plan_service.recent_run = m4_run_store.latest_run

    app.state.monitor_web_service = monitor_web_service
    if app.state.monitor_web_service is None and os.name == "nt" and hasattr(
        provider, "resolve_branch_identity"
    ):
        monitor_config = (
            config.get("monitor", {})
            if isinstance(config.get("monitor", {}), dict)
            else {}
        )
        configured_monitor_db = os.environ.get("EXCEL_MERGE_MONITOR_DB") or str(
            monitor_config.get("database_path", "var/m3-monitor/monitor.sqlite3")
        )
        monitor_database = Path(configured_monitor_db)
        if not monitor_database.is_absolute():
            monitor_database = PROJECT_ROOT / monitor_database
        try:
            monitor_store = MonitorStore(monitor_database)
            monitor_tasks = MonitorTaskService(monitor_store)
            monitor_scheduler = MonitorSchedulerService(
                monitor_store,
                WindowsSchedulerGateway(),
                database_path=monitor_database,
                working_directory=PROJECT_ROOT,
            )
            monitor_publisher = FileSystemMonitorReportPublisher(
                monitor_database.parent / "reports"
            )
            app.state.monitor_web_service = MonitorWebService(
                store=monitor_store,
                tasks=monitor_tasks,
                scheduled_tasks=ScheduledMonitorTaskService(
                    monitor_tasks, monitor_scheduler
                ),
                scheduler=monitor_scheduler,
                history=BranchHistoryService(provider),
                endpoint_registry=app.state.monitor_endpoint_catalog.records,
                dataset_layout=dataset_layout if isinstance(dataset_layout, dict) else None,
                runner=build_runner(
                    database_path=monitor_database,
                    config_path=DEFAULT_CONFIG_PATH,
                ),
                publisher=monitor_publisher,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            app.state.monitor_web_service = None

    def close_batch_service() -> None:
        service = getattr(app.state, "batch_diff_service", None)
        if service is not None:
            service.close()

    def close_diff_plan_run_service() -> None:
        service = getattr(app.state, "diff_plan_run_service", None)
        if service is not None:
            service.close()

    def close_workbook_execution_scheduler() -> None:
        scheduler = getattr(app.state, "workbook_execution_scheduler", None)
        if scheduler is not None:
            scheduler.close()

    def start_diff_plan_run_service() -> None:
        service = getattr(app.state, "diff_plan_run_service", None)
        if service is not None and hasattr(service, "start"):
            service.start()

    def start_operations_logging() -> None:
        if app.state.operations_logging_enabled:
            app.state.operational_log_service.start()

    def close_operations_logging() -> None:
        app.state.operational_log_service.close()

    def start_monitor_web_service() -> None:
        service = getattr(app.state, "monitor_web_service", None)
        if service is not None:
            service.recover_pending_commands()
            service.start_retry_dispatcher()

    def close_monitor_web_service() -> None:
        service = getattr(app.state, "monitor_web_service", None)
        if service is not None:
            service.close()

    app.add_event_handler("startup", start_operations_logging)
    app.add_event_handler("startup", start_monitor_web_service)
    app.add_event_handler("startup", start_diff_plan_run_service)
    app.add_event_handler("shutdown", close_batch_service)
    app.add_event_handler("shutdown", close_diff_plan_run_service)
    app.add_event_handler("shutdown", close_workbook_execution_scheduler)
    app.add_event_handler("shutdown", close_operations_logging)
    app.add_event_handler("shutdown", close_monitor_web_service)

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.middleware("http")
    async def operations_request_log(request: Request, call_next):
        started = time.perf_counter()
        try:
            request_id = UUID(request.headers.get("x-request-id", ""))
        except (ValueError, TypeError):
            request_id = uuid4()
        task_id = None
        match = TASK_PATH_PATTERN.search(request.url.path)
        candidate = match.group(1) if match else (
            request.query_params.get("task_id")
            if request.url.path == "/compare/results"
            else None
        )
        if candidate:
            try:
                task_id = UUID(candidate)
            except (ValueError, TypeError):
                task_id = None
        request.state.request_id = request_id
        request.state.task_id = task_id
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = str(request_id)
            return response
        except Exception:
            if request.url.path.startswith("/api/monitor/"):
                status_code = 500
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "code": "MONITOR_API_INTERNAL_ERROR",
                            "message": "版本监控服务内部错误",
                        }
                    },
                    headers={"X-Request-ID": str(request_id)},
                )
            raise
        finally:
            if not (
                request.method == "GET"
                and request.url.path.startswith("/api/operations/")
            ):
                app.state.operational_log_service.record_request(
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    request_id=request_id,
                    task_id=task_id,
                )

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "provider": provider_name,
                "configured_provider": str(getattr(request.app.state, "configured_provider", provider_name)),
                "provider_locked": bool(getattr(request.app.state, "provider_locked", False)),
                "credential_source": credential_source,
                "default_url": str(getattr(request.app.state, "default_url", "")),
                "endpoint_catalog": getattr(request.app.state, "endpoint_catalog", DEFAULT_ENDPOINT_CATALOG),
                "active_page": "settings",
            },
        )

    @app.get("/compare", include_in_schema=False)
    def compare_preview(request: Request):
        """M2 版本与快照入口；沿用 M1 快照接口，不执行语义 Diff。"""
        return templates.TemplateResponse(
            "compare.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "compare",
                "demo_mode": False,
            },
        )

    @app.get("/compare/history", include_in_schema=False)
    def compare_history(request: Request):
        """历史批量任务、实时进度和正式结果恢复入口。"""
        return templates.TemplateResponse(
            "history_tasks.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "history",
            },
        )

    @app.get("/diff-plans", include_in_schema=False)
    def diff_plan_index(request: Request):
        return templates.TemplateResponse(
            "diff_plans.html",
            {"request": request, "provider": provider_name, "active_page": "diff_plans"},
        )

    @app.get("/diff-plans/new", include_in_schema=False)
    def diff_plan_new(request: Request):
        return templates.TemplateResponse(
            "diff_plan_form.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "diff_plans",
                "plan_id": None,
            },
        )

    @app.get("/diff-plans/{plan_id}", include_in_schema=False)
    def diff_plan_detail(plan_id: UUID, request: Request):
        return templates.TemplateResponse(
            "diff_plan_detail.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "diff_plans",
                "plan_id": str(plan_id),
            },
        )

    @app.get("/diff-plans/{plan_id}/edit", include_in_schema=False)
    def diff_plan_edit(plan_id: UUID, request: Request):
        return templates.TemplateResponse(
            "diff_plan_form.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "diff_plans",
                "plan_id": str(plan_id),
            },
        )

    @app.get("/diff-plan-runs/{run_id}", include_in_schema=False)
    def diff_plan_run_result(run_id: UUID, request: Request):
        return templates.TemplateResponse(
            "compare_results.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "diff_plans",
                "run_id": str(run_id),
                "m4_run_id": str(run_id),
                "demo_mode": False,
                "replay_mode": False,
            },
        )

    @app.get("/monitor", include_in_schema=False)
    def monitor_overview(request: Request):
        return templates.TemplateResponse(
            "monitor.html",
            {"request": request, "provider": provider_name, "active_page": "monitor"},
        )

    @app.get("/monitor/tasks", include_in_schema=False)
    def monitor_tasks_page(request: Request):
        return templates.TemplateResponse(
            "monitor_tasks.html",
            {"request": request, "provider": provider_name, "active_page": "monitor"},
        )

    @app.get("/monitor/reports/{run_id}", include_in_schema=False)
    def monitor_report_page(run_id: UUID):
        return RedirectResponse(
            url=f"/api/monitor/runs/{run_id}/report", status_code=307
        )
    @app.get("/compare/demo", include_in_schema=False)
    def compare_demo(request: Request):
        """开发模式流程示例；不读取本地文件或生成真实语义 Diff。"""
        if not bool(web_config.get("dev_mode", False)):
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            "compare.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "compare",
                "demo_mode": True,
            },
        )

    @app.get("/compare/results", include_in_schema=False)
    def compare_results(request: Request):
        """独立差异结果页；当前只恢复前端任务上下文。"""
        return templates.TemplateResponse(
            "compare_results.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "compare",
                "demo_mode": False,
            },
        )

    @app.get("/compare/demo/results", include_in_schema=False)
    def compare_demo_results(request: Request):
        """开发模式独立结果页；只展示明确标注的 UI 假数据。"""
        if not bool(web_config.get("dev_mode", False)):
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            "compare_results.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "compare",
                "demo_mode": True,
            },
        )

    @app.get("/compare/replay", include_in_schema=False)
    def compare_replay(request: Request):
        """开发模式离线夹具回放；不访问 SVN 或批量数据库。"""
        if not bool(web_config.get("dev_mode", False)):
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            "compare_results.html",
            {
                "request": request,
                "provider": provider_name,
                "active_page": "compare",
                "demo_mode": False,
                "replay_mode": True,
            },
        )
    @app.exception_handler(SVNProviderError)
    async def svn_error_handler(_: Request, exc: SVNProviderError):
        return JSONResponse(status_code=400, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(WorkbookCompareError)
    async def workbook_compare_error_handler(_: Request, exc: WorkbookCompareError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(BatchDiffError)
    async def batch_diff_error_handler(_: Request, exc: BatchDiffError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(DiffPlanError)
    async def diff_plan_error_handler(_: Request, exc: DiffPlanError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )


    @app.exception_handler(OfflineFixtureError)
    async def offline_fixture_error_handler(_: Request, exc: OfflineFixtureError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    @app.exception_handler(OperationsError)
    async def operations_error_handler(_: Request, exc: OperationsError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    @app.exception_handler(MonitorWebError)
    async def monitor_web_error_handler(_: Request, exc: MonitorWebError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/diff-plans"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "DIFF_PLAN_INVALID_REQUEST",
                        "message": "表格计划对比请求无效",
                    }
                },
            )
        if request.url.path.startswith("/api/operations/"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "OPERATIONS_INVALID_REQUEST",
                        "message": "运维查询请求无效",
                    }
                },
            )
        if request.url.path.startswith("/api/monitor/"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "MONITOR_INVALID_REQUEST",
                        "message": "版本监控请求无效",
                    }
                },
            )
        if request.url.path.startswith(("/api/diff/batches", "/api/diff/batch-results")):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "BATCH_INVALID_REQUEST",
                        "message": "批量 Diff 请求无效",
                    }
                },
            )
        if request.url.path.startswith("/api/diff/"):
            invalid_path = any(
                error.get("loc", ())[-1:] == ("workbook_path",)
                for error in exc.errors()
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": (
                            "DIFF_INVALID_WORKBOOK_PATH"
                            if invalid_path
                            else "DIFF_INVALID_REQUEST"
                        ),
                        "message": (
                            "工作簿路径不合法"
                            if invalid_path
                            else "单工作簿比较请求无效"
                        ),
                    }
                },
            )
        return JSONResponse(status_code=422, content={"error": {"code": "SVN_INVALID_REQUEST", "message": "请求参数无效", "fields": exc.errors()}})

    app.include_router(health.router, prefix="/api")
    app.include_router(svn.router, prefix="/api")
    app.include_router(diff.router, prefix="/api")
    app.include_router(diff_plan.router, prefix="/api")
    app.include_router(batch.router, prefix="/api")
    app.include_router(operations.router, prefix="/api")
    app.include_router(monitor.router, prefix="/api")
    if bool(web_config.get("dev_mode", False)):
        app.include_router(replay.router, prefix="/api")
    return app


app = create_app(config=initialize_default_config())


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    web_config = config.get("web", {})
    uvicorn.run(app, host=str(web_config.get("host", "127.0.0.1")), port=int(web_config.get("port", 5566)))
