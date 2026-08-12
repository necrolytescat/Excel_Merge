"""M4 DiffPlan 计划管理 API。"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.diff_plan import (
    DiffPlanCommandRequestPayload,
    DiffPlanCreateRequestPayload,
    DiffPlanUpdateRequestPayload,
    DiffPlanRunCommandRequestPayload,
    DiffPlanRunRetryRequestPayload,
    DiffPlanRunStartRequestPayload,
    WorkbookCatalogRequestPayload,
)
from app.services.diff_plan_service import DiffPlanService
from app.services.diff_plan_run_service import DiffPlanRunService
from app.services.diff_plan_store import DiffPlanError


router = APIRouter(prefix="/diff-plans", tags=["diff-plans"])


def get_service(request: Request) -> DiffPlanService:
    service = getattr(request.app.state, "diff_plan_service", None)
    if service is None:
        raise DiffPlanError("DIFF_PLAN_SERVICE_UNAVAILABLE", "表格计划对比服务尚未配置", status_code=500)
    return service


def get_run_service(request: Request) -> DiffPlanRunService:
    service = getattr(request.app.state, "diff_plan_run_service", None)
    if service is None:
        raise DiffPlanError("DIFF_PLAN_RUN_SERVICE_UNAVAILABLE", "计划运行服务尚未配置", status_code=503)
    return service


def _response(payload, request: Request | None = None, *, status_code: int = 200) -> Response:
    data = payload.model_dump(mode="json")
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if request is not None and request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, status_code=status_code, headers={"ETag": etag})


@router.post("/workbook-catalog")
def workbook_catalog(
    payload: WorkbookCatalogRequestPayload,
    service: DiffPlanService = Depends(get_service),
) -> Response:
    return _response(service.workbook_catalog(payload))


@router.get("")
def list_plans(
    request: Request,
    archived: bool = Query(False),
    service: DiffPlanService = Depends(get_service),
) -> Response:
    return _response(service.list(archived=archived), request)


@router.post("")
def create_plan(
    payload: DiffPlanCreateRequestPayload,
    service: DiffPlanService = Depends(get_service),
) -> Response:
    plan, created = service.create(payload)
    return _response(plan, status_code=201 if created else 200)


@router.get("/{plan_id}")
def get_plan(
    plan_id: UUID,
    request: Request,
    service: DiffPlanService = Depends(get_service),
) -> Response:
    return _response(service.get(plan_id), request)


@router.put("/{plan_id}")
def update_plan(
    plan_id: UUID,
    payload: DiffPlanUpdateRequestPayload,
    service: DiffPlanService = Depends(get_service),
) -> Response:
    plan, _ = service.update(plan_id, payload)
    return _response(plan)


@router.post("/{plan_id}/archive")
def archive_plan(
    plan_id: UUID,
    payload: DiffPlanCommandRequestPayload,
    service: DiffPlanService = Depends(get_service),
) -> Response:
    plan, _ = service.set_archived(plan_id, payload, archived=True)
    return _response(plan)


@router.post("/{plan_id}/restore")
def restore_plan(
    plan_id: UUID,
    payload: DiffPlanCommandRequestPayload,
    service: DiffPlanService = Depends(get_service),
) -> Response:
    plan, _ = service.set_archived(plan_id, payload, archived=False)
    return _response(plan)


@router.get("/{plan_id}/runs")
def list_runs(
    plan_id: UUID,
    request: Request,
    service: DiffPlanRunService = Depends(get_run_service),
) -> Response:
    return _response(service.list_runs(plan_id), request)


@router.post("/{plan_id}/runs")
def start_run(
    plan_id: UUID,
    payload: DiffPlanRunStartRequestPayload,
    service: DiffPlanRunService = Depends(get_run_service),
) -> Response:
    run, created = service.start_run(plan_id, payload)
    return _response(run, status_code=202 if created else 200)


@router.get("/runs/{run_id}")
def get_run(
    run_id: UUID,
    request: Request,
    service: DiffPlanRunService = Depends(get_run_service),
) -> Response:
    return _response(service.get_run(run_id), request)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: UUID,
    payload: DiffPlanRunCommandRequestPayload,
    service: DiffPlanRunService = Depends(get_run_service),
) -> Response:
    return _response(service.cancel(run_id, payload))


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: UUID,
    payload: DiffPlanRunRetryRequestPayload,
    service: DiffPlanRunService = Depends(get_run_service),
) -> Response:
    run, created = service.retry(run_id, payload)
    return _response(run, status_code=202 if created else 200)


@router.get("/run-results/{result_ref}")
def run_result(
    result_ref: str,
    request: Request,
    service: DiffPlanRunService = Depends(get_run_service),
) -> Response:
    content, etag = service.load_result(result_ref)
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=content, media_type="application/json", headers={"ETag": etag})
