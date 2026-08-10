from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.monitor import (
    MonitorCommandRequestPayload,
    MonitorRunRetryRequestPayload,
    MonitorTaskCreateRequestPayload,
    MonitorTaskPatchRequestPayload,
    MonitorTaskStatus,
)
from app.services.monitor_api_contract import etag_matches, response_etag
from app.services.monitor_web_service import MonitorWebError, MonitorWebService


router = APIRouter(prefix="/monitor", tags=["monitor"])


def get_monitor_service(request: Request) -> MonitorWebService:
    service = getattr(request.app.state, "monitor_web_service", None)
    if service is None:
        raise MonitorWebError(
            "MONITOR_SERVICE_UNAVAILABLE",
            "版本监控服务尚未配置",
            503,
        )
    return service


def _json_payload(
    payload,
    request: Request | None = None,
    *,
    status_code: int = 200,
    exclude_as_of: bool = False,
) -> Response:
    data = payload.model_dump(mode="json")
    etag = response_etag(data, exclude_as_of=exclude_as_of)
    if request is not None and etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, status_code=status_code, headers={"ETag": etag})


def _report_response(
    content: bytes, sha256: str, request: Request
) -> Response:
    etag = f'"{sha256}"'
    headers = {
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
    }
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type="text/html; charset=utf-8", headers=headers)


@router.get("/endpoint-options")
def endpoint_options(
    request: Request,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    return _json_payload(service.endpoint_options(), request)


@router.post("/tasks")
def create_task(
    payload: MonitorTaskCreateRequestPayload,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    task, status_code = service.create_task(payload)
    return _json_payload(task, status_code=status_code)


@router.get("/tasks")
def list_tasks(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=512),
    status: list[MonitorTaskStatus] | None = Query(None),
    q: str | None = Query(None, max_length=128),
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    payload = service.list_tasks(
        limit=limit,
        cursor=cursor,
        statuses=[item.value for item in status] if status else None,
        query=q,
    )
    return _json_payload(payload, request, exclude_as_of=True)


@router.get("/tasks/{task_id}")
def get_task(
    task_id: UUID,
    request: Request,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    return _json_payload(service.get_task(task_id), request)


@router.patch("/tasks/{task_id}")
def patch_task(
    task_id: UUID,
    payload: MonitorTaskPatchRequestPayload,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    task, status_code = service.patch_task(task_id, payload)
    return _json_payload(task, status_code=status_code)


def _task_command(
    task_id: UUID,
    command: str,
    payload: MonitorCommandRequestPayload,
    service: MonitorWebService,
) -> Response:
    task, status_code = service.task_command(task_id, command, payload.request_id)
    return _json_payload(task, status_code=status_code)


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: UUID, payload: MonitorCommandRequestPayload, service=Depends(get_monitor_service)):
    return _task_command(task_id, "pause", payload, service)


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: UUID, payload: MonitorCommandRequestPayload, service=Depends(get_monitor_service)):
    return _task_command(task_id, "resume", payload, service)


@router.post("/tasks/{task_id}/end")
def end_task(task_id: UUID, payload: MonitorCommandRequestPayload, service=Depends(get_monitor_service)):
    return _task_command(task_id, "end", payload, service)


@router.post("/tasks/{task_id}/archive")
def archive_task(task_id: UUID, payload: MonitorCommandRequestPayload, service=Depends(get_monitor_service)):
    return _task_command(task_id, "archive", payload, service)


@router.post("/tasks/{task_id}/scheduler-sync")
def scheduler_sync(task_id: UUID, payload: MonitorCommandRequestPayload, service=Depends(get_monitor_service)):
    return _task_command(task_id, "scheduler-sync", payload, service)


@router.get("/tasks/{task_id}/runs")
def list_runs(
    task_id: UUID,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=512),
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    payload = service.list_runs(task_id, limit=limit, cursor=cursor)
    return _json_payload(payload, request, exclude_as_of=True)


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: UUID,
    payload: MonitorRunRetryRequestPayload,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    accepted, status_code = service.accept_retry(run_id, payload.request_id)
    return _json_payload(accepted, status_code=status_code)


@router.get("/runs/{run_id}/report")
def run_report(
    run_id: UUID,
    request: Request,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    return _report_response(*service.load_run_report(run_id), request)


@router.get("/tasks/{task_id}/latest-report")
def latest_report(
    task_id: UUID,
    request: Request,
    service: MonitorWebService = Depends(get_monitor_service),
) -> Response:
    return _report_response(*service.load_latest_report(task_id), request)
