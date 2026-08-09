from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.operations import SVNCacheClearRequestPayload
from app.services.operations_service import OperationalLogService, SVNCacheService


router = APIRouter(prefix="/operations", tags=["operations"])
logger = logging.getLogger(__name__)


def get_log_service(request: Request) -> OperationalLogService:
    return request.app.state.operational_log_service


def get_cache_service(request: Request) -> SVNCacheService:
    return request.app.state.svn_cache_service


def _response(payload, request: Request, *, exclude_as_of: bool = False) -> Response:
    data = payload.model_dump(mode="json")
    etag_data = {key: value for key, value in data.items() if not (exclude_as_of and key == "as_of")}
    canonical = json.dumps(etag_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, headers={"ETag": etag})


@router.get("/logs")
def list_logs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, min_length=1, max_length=512),
    level: Literal["debug", "info", "warning", "error"] | None = Query(None),
    q: str | None = Query(None, max_length=128),
    task_id: UUID | None = Query(None),
    request_id: UUID | None = Query(None),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    service: OperationalLogService = Depends(get_log_service),
) -> Response:
    payload = service.list_logs(
        limit=limit,
        cursor=cursor,
        level=level,
        query=q,
        task_id=task_id,
        request_id=request_id,
        created_from=created_from,
        created_to=created_to,
    )
    return _response(payload, request, exclude_as_of=True)


@router.get("/svn-cache")
def get_svn_cache_status(
    request: Request,
    service: SVNCacheService = Depends(get_cache_service),
) -> Response:
    return _response(service.status(), request)


@router.post("/svn-cache/clear")
def clear_svn_cache(
    payload: SVNCacheClearRequestPayload,
    service: SVNCacheService = Depends(get_cache_service),
) -> Response:
    result = service.clear(payload.request_id)
    logger.info(
        "SVN 全局缓存已清理 request_id=%s files=%s bytes=%s",
        payload.request_id,
        result.removed_file_count,
        result.removed_size_bytes,
        extra={
            "event": "svn_cache.cleared",
            "request_id": str(payload.request_id),
        },
    )
    return JSONResponse(content=result.model_dump(mode="json"))
