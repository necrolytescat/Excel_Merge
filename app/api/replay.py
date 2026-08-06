from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.services.offline_fixture import (
    FixtureLimits,
    OfflineFixtureError,
    OfflineFixtureService,
)


router = APIRouter(prefix="/replay", tags=["replay"])


def _service(request: Request) -> OfflineFixtureService:
    service = getattr(request.app.state, "offline_fixture_service", None)
    if service is None:
        raise OfflineFixtureError(
            "FIXTURE_REPLAY_DISABLED",
            "离线夹具回放仅在开发模式开放",
            status_code=404,
        )
    return service


@router.post("/fixture")
async def load_fixture(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        raise OfflineFixtureError(
            "FIXTURE_CONTENT_TYPE_INVALID",
            "离线夹具必须使用 application/octet-stream 上传",
            status_code=415,
        )
    limit = FixtureLimits().max_archive_bytes
    content_length = request.headers.get("content-length")
    if content_length and not content_length.isdecimal():
        raise OfflineFixtureError(
            "FIXTURE_CONTENT_LENGTH_INVALID",
            "离线夹具 Content-Length 无效",
            status_code=400,
        )
    if content_length and int(content_length) > limit:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_TOO_LARGE",
            "离线夹具超过上传大小限制",
            status_code=413,
        )
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise OfflineFixtureError(
                "FIXTURE_ARCHIVE_TOO_LARGE",
                "离线夹具超过上传大小限制",
                status_code=413,
            )
        raw.extend(chunk)
    return JSONResponse(content=_service(request).load(bytes(raw)))


@router.get("/fixture")
def get_fixture_session(request: Request) -> JSONResponse:
    return JSONResponse(content=_service(request).session())


@router.post("/recompute")
def recompute_fixture(request: Request) -> JSONResponse:
    return JSONResponse(content=_service(request).recompute_all())


@router.post("/recompute/{item_id}")
def recompute_fixture_item(item_id: UUID, request: Request) -> JSONResponse:
    return JSONResponse(content=_service(request).recompute_item(item_id))


@router.get("/results/{item_id}")
def get_fixture_result(
    item_id: UUID,
    request: Request,
    mode: Literal["golden", "current"] = "golden",
) -> Response:
    content, digest, matches_golden = _service(request).result(item_id, mode=mode)
    headers = {"ETag": digest, "X-M2-Fixture-Mode": mode}
    if matches_golden is not None:
        headers["X-M2-Golden-Match"] = "true" if matches_golden else "false"
    return Response(content=content, media_type="application/json", headers=headers)
