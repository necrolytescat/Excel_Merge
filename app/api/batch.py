from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.batch import (
    BatchCancelRequestPayload,
    BatchCreateRequestPayload,
    BatchRetryRequestPayload,
    BatchTaskPayload,
)
from app.services.batch_diff_service import BatchDiffService
from app.services.batch_store import BatchDiffError


router = APIRouter(prefix="/diff", tags=["diff"])


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


@router.post("/batches")
def create_batch(
    payload: BatchCreateRequestPayload,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    task, created = service.create_task(payload)
    return _task_response(task, status_code=202 if created else 200)


@router.get("/batches/{task_id}")
def get_batch(
    task_id: UUID,
    request: Request,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    return _task_response(service.get_task(task_id), request=request)


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
    return _task_response(task, status_code=202)


@router.post("/batches/{task_id}/retry")
def retry_batch(
    task_id: UUID,
    payload: BatchRetryRequestPayload,
    service: BatchDiffService = Depends(get_batch_service),
) -> Response:
    task, created = service.retry_task(task_id, payload)
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
