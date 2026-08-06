from __future__ import annotations

import logging
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Request, Response

from app.schemas.diff import serialize_diff_json
from app.schemas.workbook_compare import WorkbookCompareRequestPayload
from app.services.workbook_dataset_service import (
    WorkbookCompareError,
    WorkbookDatasetResolver,
)
from app.services.workbook_diff_service import WorkbookDiffService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/diff", tags=["diff"])


def get_dataset_resolver(request: Request) -> WorkbookDatasetResolver:
    return request.app.state.workbook_dataset_resolver


def get_workbook_diff_service(request: Request) -> WorkbookDiffService | None:
    return request.app.state.workbook_diff_service


@router.post("/workbooks/compare")
def compare_workbook(
    payload: WorkbookCompareRequestPayload,
    resolver: WorkbookDatasetResolver = Depends(get_dataset_resolver),
    service: WorkbookDiffService | None = Depends(get_workbook_diff_service),
) -> Response:
    if service is None:
        raise WorkbookCompareError(
            "DIFF_SERVICE_UNAVAILABLE",
            "单工作簿 Diff 服务尚未配置",
            status_code=500,
        )

    workbook_name = PurePosixPath(payload.workbook_path).name
    try:
        with resolver.resolve(payload) as dataset:
            result = service.compare_local(
                dataset.source_directory,
                dataset.target_directory,
                workbook_name,
            )
            content = serialize_diff_json(result)
    except WorkbookCompareError:
        raise
    except Exception as exc:
        logger.exception("单工作簿 Diff 编排失败 request_id=%s", payload.request_id)
        raise WorkbookCompareError(
            "DIFF_ORCHESTRATION_FAILED",
            "单工作簿差异比对失败",
            status_code=500,
        ) from exc

    return Response(content=content, media_type="application/json")
