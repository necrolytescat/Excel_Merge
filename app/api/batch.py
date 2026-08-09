from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.batch import (
    BatchCancelRequestPayload,
    BatchCreateRequestPayload,
    BatchDeleteRequestPayload,
    BatchRetryRequestPayload,
    BatchTaskListPayload,
    BatchTaskManagementPayload,
    BatchTaskPayload,
    TaskStatus,
)
from app.services.batch_diff_service import BatchDiffService
from app.services.batch_store import BatchDiffError


router = APIRouter(prefix="/diff", tags=["diff"])
logger = logging.getLogger(__name__)


def get_batch_service(request: Request) -> BatchDiffService:
    service = getattr(request.app.state, "batch_diff_service", None)
    if service is None:
        raise BatchDiffError(
            "BATCH_SERVICE_UNAVAILABLE",
            "批量 Diff 服务尚未配置",
            status_code=500,
        )
    return service


def _task_response(
    task: BatchTaskPayload,
    *,
    status_code: int = 200,
    request: Request | None = None,
) -> Response:
    data = task.model_dump(mode="json")
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if request is not None and request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, status_code=status_code, headers={"ETag": etag})


def _list_response(payload: BatchTaskListPayload, request: Request) -> Response:
    data = payload.model_dump(mode="json")
    etag_data = {key: value for key, value in data.items() if key != "as_of"}
    canonical = json.dumps(etag_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, headers={"ETag": etag})


def _management_response(
    payload: BatchTaskManagementPayload,
    request: Request,
) -> Response:
    data = payload.model_dump(mode="json")
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, headers={"ETag": etag})

@router.post("/batches")
def create_batch(
    payload: BatchCreateRequestPayload,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    task, created = service.create_task(payload)
    logger.info(
        "批量任务%s task_id=%s request_id=%s",
        "已创建" if created else "幂等返回",
        task.task_id,
        payload.request_id,
        extra={
            "event": "batch.created" if created else "batch.idempotent",
            "task_id": str(task.task_id),
            "request_id": str(payload.request_id),
        },
    )
    return _task_response(task, status_code=202 if created else 200)


@router.get("/batches")
def list_batches(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=512),
    status: list[TaskStatus] | None = Query(None),
    q: str | None = Query(None, max_length=128),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    payload = service.list_tasks(
        limit=limit,
        cursor=cursor,
        statuses=status,
        query=q,
        created_from=created_from,
        created_to=created_to,
    )
    return _list_response(payload, request)


@router.get("/batches/{task_id}/management")
def get_batch_management(
    task_id: UUID,
    request: Request,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    return _management_response(service.get_task_management(task_id), request)


@router.get("/batches/{task_id}")
def get_batch(
    task_id: UUID,
    request: Request,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    return _task_response(service.get_task(task_id), request=request)


@router.delete("/batches/{task_id}")
def delete_batch(
    task_id: UUID,
    payload: BatchDeleteRequestPayload,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    result = service.delete_task(
        task_id,
        request_id=payload.request_id,
        reason=payload.reason,
    )
    logger.info(
        "批量任务已删除 task_id=%s request_id=%s",
        task_id,
        payload.request_id,
        extra={
            "event": "batch.deleted",
            "task_id": str(task_id),
            "request_id": str(payload.request_id),
        },
    )
    return JSONResponse(content=result.model_dump(mode="json"))


@router.post("/batches/{task_id}/cancel")
def cancel_batch(
    task_id: UUID,
    payload: BatchCancelRequestPayload,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    task = service.cancel_task(
        task_id,
        request_id=payload.request_id,
        reason=payload.reason,
    )
    logger.info(
        "批量任务已请求取消 task_id=%s request_id=%s",
        task_id,
        payload.request_id,
        extra={
            "event": "batch.cancel_requested",
            "task_id": str(task_id),
            "request_id": str(payload.request_id),
        },
    )
    return _task_response(task, status_code=202)


@router.post("/batches/{task_id}/retry")
def retry_batch(
    task_id: UUID,
    payload: BatchRetryRequestPayload,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    task, created = service.retry_task(task_id, payload)
    logger.info(
        "批量重试任务%s task_id=%s request_id=%s",
        "已创建" if created else "幂等返回",
        task.task_id,
        payload.request_id,
        extra={
            "event": "batch.retry_created" if created else "batch.retry_idempotent",
            "task_id": str(task.task_id),
            "request_id": str(payload.request_id),
        },
    )
    return _task_response(task, status_code=202 if created else 200)


@router.get("/batch-results/{result_ref}")
def get_batch_result(
    result_ref: str,
    request: Request,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    content, sha256 = service.load_result(result_ref)
    if request.headers.get("if-none-match", "").strip('"') == sha256:
        return Response(status_code=304, headers={"ETag": sha256})
    return Response(
        content=content,
        media_type="application/json",
        headers={"ETag": sha256},
    )
