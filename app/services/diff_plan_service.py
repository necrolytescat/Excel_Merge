"""M4 计划校验、TABLE 工作簿目录与计划应用服务。"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from app.schemas.diff_plan import (
    DiffPlanCommandRequestPayload,
    DiffPlanCreateRequestPayload,
    DiffPlanListPayload,
    DiffPlanPayload,
    DiffPlanUpdateRequestPayload,
    WorkbookCatalogItemPayload,
    WorkbookCatalogPayload,
    WorkbookCatalogRequestPayload,
)
from app.services.diff_plan_store import DiffPlanError, DiffPlanStore
from app.services.snapshot_service import EXCEL_EXTENSIONS, SnapshotService
from core.models import EndpointSpec
from core.svn_provider import SVNProvider, SVNProviderError, normalize_relative_path


class DiffPlanWorkbookCatalogService:
    def __init__(
        self,
        provider: SVNProvider,
        snapshot_service: SnapshotService,
        endpoint_registry: Callable[[], Sequence[Mapping[str, Any]]],
    ):
        self.provider = provider
        self.snapshot_service = snapshot_service
        self.endpoint_registry = endpoint_registry

    def load(self, payload: WorkbookCatalogRequestPayload) -> WorkbookCatalogPayload:
        records = SnapshotService.normalize_registry(
            [dict(record) for record in self.endpoint_registry()]
        )
        record = SnapshotService.record_map(records).get(payload.endpoint_id)
        if record is None:
            raise DiffPlanError("DIFF_PLAN_ENDPOINT_NOT_FOUND", "选择的分支不存在", status_code=404)
        if not bool(record.get("enabled", True)):
            raise DiffPlanError("DIFF_PLAN_ENDPOINT_DISABLED", "选择的分支已停用", status_code=422)
        revision = (
            self.snapshot_service.freeze_head(record)
            if payload.revision == "HEAD"
            else payload.revision
        )
        if not isinstance(revision, int):
            raise DiffPlanError("DIFF_PLAN_INVALID_REVISION", "分支没有返回有效 Revision", status_code=422)
        endpoint = EndpointSpec(
            url=str(record["url"]),
            revision=revision,
            label=str(record.get("label", payload.endpoint_id)),
        )
        entries = self.provider.list_tree(endpoint)
        physical = self.snapshot_service.resolve_scope_paths(
            record,
            revision,
            entries=entries,
        )
        table_path = normalize_relative_path(physical["TABLE"])
        prefix = table_path.casefold() + "/"
        workbooks = []
        for entry in entries:
            normalized = normalize_relative_path(entry.path)
            if entry.kind != "file" or not normalized.casefold().endswith(EXCEL_EXTENSIONS):
                continue
            if not normalized.casefold().startswith(prefix):
                continue
            relative = normalized[len(table_path) + 1 :]
            if not relative or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                continue
            svn_revision = int(entry.revision) if str(entry.revision).isdigit() else (entry.revision or None)
            workbooks.append(
                WorkbookCatalogItemPayload(
                    path=relative,
                    size_bytes=entry.size,
                    svn_revision=svn_revision,
                )
            )
        workbooks.sort(key=lambda item: (item.path.casefold(), item.path))
        return WorkbookCatalogPayload(
            endpoint_id=payload.endpoint_id,
            endpoint_label=str(record.get("label", payload.endpoint_id)),
            resolved_revision=revision,
            table_path=table_path,
            workbooks=workbooks,
            total=len(workbooks),
        )


class DiffPlanService:
    def __init__(
        self,
        store: DiffPlanStore,
        catalog: DiffPlanWorkbookCatalogService,
        endpoint_registry: Callable[[], Sequence[Mapping[str, Any]]],
        recent_run=None,
    ):
        self.store = store
        self.catalog = catalog
        self.endpoint_registry = endpoint_registry
        self.recent_run = recent_run

    def workbook_catalog(self, payload: WorkbookCatalogRequestPayload) -> WorkbookCatalogPayload:
        return self.catalog.load(payload)

    def _validate_definition(self, payload) -> None:
        records = {
            str(record.get("id", "")): record for record in self.endpoint_registry()
        }
        for endpoint_id in [payload.source_endpoint_id, *payload.target_endpoint_ids]:
            record = records.get(endpoint_id)
            if record is None:
                raise DiffPlanError("DIFF_PLAN_ENDPOINT_NOT_FOUND", "计划包含不存在的分支", status_code=404)
            if not bool(record.get("enabled", True)):
                raise DiffPlanError("DIFF_PLAN_ENDPOINT_DISABLED", "计划包含已停用的分支", status_code=422)
        catalog = self.workbook_catalog(
            WorkbookCatalogRequestPayload(
                schema_version="m4.workbook-catalog.request.v1",
                endpoint_id=payload.source_endpoint_id,
                revision="HEAD",
            )
        )
        available = {item.path.casefold(): item.path for item in catalog.workbooks}
        missing = [path for path in payload.workbook_paths if path.casefold() not in available]
        if missing:
            raise DiffPlanError(
                "DIFF_PLAN_WORKBOOK_NOT_IN_SOURCE",
                "选择的工作簿不在基准分支 TABLE 目录中",
                status_code=422,
            )

    def create(self, payload: DiffPlanCreateRequestPayload) -> tuple[DiffPlanPayload, bool]:
        self._validate_definition(payload)
        return self.store.create(payload)

    def update(self, plan_id, payload: DiffPlanUpdateRequestPayload) -> tuple[DiffPlanPayload, bool]:
        self._validate_definition(payload)
        return self.store.update(plan_id, payload)

    def get(self, plan_id) -> DiffPlanPayload:
        plan = self.store.get(plan_id)
        if self.recent_run is None:
            return plan
        return plan.model_copy(update={"recent_run": self.recent_run(plan.plan_id)})

    def list(self, *, archived: bool) -> DiffPlanListPayload:
        payload = self.store.list(archived=archived)
        if self.recent_run is None:
            return payload
        plans = [plan.model_copy(update={"recent_run": self.recent_run(plan.plan_id)}) for plan in payload.plans]
        return payload.model_copy(update={"plans": plans})

    def set_archived(self, plan_id, payload: DiffPlanCommandRequestPayload, *, archived: bool):
        return self.store.set_archived(plan_id, payload, archived=archived)
