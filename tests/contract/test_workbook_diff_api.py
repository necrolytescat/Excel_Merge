from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.schemas.diff import (
    DiffDirectionPayload,
    DiffErrorPayload,
    DiffResultPayload,
    ErrorStage,
    SheetDiffPayload,
    SheetStatus,
    SheetSummaryPayload,
    WorkbookDiffPayload,
    WorkbookStatus,
    WorkbookSummaryPayload,
    serialize_diff_json,
)
from app.services.workbook_dataset_service import BoundWorkbookDatasetResolver
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.svn_provider import MockSVNProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
SOURCE_ENDPOINT_ID = "KR_FIX_KR-Fix-1.0.0.0"
TARGET_ENDPOINT_ID = "KR_FIX_KR-Fix-1.0.1.0"
SOURCE_REVISION = 123456
TARGET_REVISION = 123789
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


def request_payload(*, workbook_path: str = "AtlasConfig.xlsm") -> dict:
    return {
        "schema_version": "m2.workbook-compare.request.v1",
        "request_id": "a7e47a49-3308-4d10-936c-bbb80e4547b3",
        "source": {
            "endpoint_id": SOURCE_ENDPOINT_ID,
            "revision": SOURCE_REVISION,
        },
        "target": {
            "endpoint_id": TARGET_ENDPOINT_ID,
            "revision": TARGET_REVISION,
        },
        "workbook_path": workbook_path,
    }


def bound_resolver(
    *,
    workbook_path: str = "AtlasConfig.xlsm",
    candidate_status: str = "modified",
) -> BoundWorkbookDatasetResolver:
    return BoundWorkbookDatasetResolver(
        source_endpoint_id=SOURCE_ENDPOINT_ID,
        source_revision=SOURCE_REVISION,
        target_endpoint_id=TARGET_ENDPOINT_ID,
        target_revision=TARGET_REVISION,
        workbook_path=workbook_path,
        source_directory=SOURCE_DIR,
        target_directory=TARGET_DIR,
        candidate_status=candidate_status,
    )


def api_client(*, resolver=None, service=None) -> TestClient:
    return TestClient(
        create_app(
            config={
                "svn": {"provider": "mock"},
                "dataset_layout": CONFIG["dataset_layout"],
            },
            provider=MockSVNProvider(),
            workbook_dataset_resolver=resolver or bound_resolver(),
            workbook_diff_service=service,
        )
    )


def result_for_status(status: WorkbookStatus) -> DiffResultPayload:
    error = DiffErrorPayload(
        code="M2_CSV_MISSING",
        stage=ErrorStage.CSV_READ,
        side="target",
        workbook="AtlasConfig.xlsm",
        sheet_name="Broken",
        file="AtlasConfig_Broken.csv",
        message="main 清单对应的 CSV 不存在或无法读取",
    )
    sheets = []
    errors = []
    summary = WorkbookSummaryPayload()
    if status == WorkbookStatus.PARTIAL:
        sheets = [
            SheetDiffPayload(
                sheet_name="Ready",
                status=SheetStatus.MODIFIED,
                summary=SheetSummaryPayload(modified_rows=1, modified_fields=1),
            ),
            SheetDiffPayload(
                sheet_name="Broken",
                status=SheetStatus.FAILED,
                errors=[error],
            ),
        ]
        errors = [error]
        summary = WorkbookSummaryPayload(
            total_sheets=2,
            modified_sheets=1,
            failed_sheets=1,
            modified_rows=1,
            modified_fields=1,
            error_count=1,
        )
    elif status == WorkbookStatus.FAILED:
        errors = [error]
        summary = WorkbookSummaryPayload(error_count=1)

    return DiffResultPayload(
        direction=DiffDirectionPayload(source="left", target="right"),
        workbook=WorkbookDiffPayload(
            name="AtlasConfig.xlsm",
            status=status,
            source_sha256="a" * 64,
            target_sha256="b" * 64,
        ),
        summary=summary,
        sheets=sheets,
        errors=errors,
    )


class StaticDiffService:
    def __init__(self, result: DiffResultPayload):
        self.result = result
        self.calls = []

    def compare_local(self, source_directory, target_directory, workbook_name):
        self.calls.append((source_directory, target_directory, workbook_name))
        return self.result


class RaisingDiffService:
    def compare_local(self, source_directory, target_directory, workbook_name):
        raise RuntimeError("internal path must not leak")


def test_compare_returns_canonical_atlas_m2_diff_v1_without_wrapper():
    layout = DatasetLayout.from_config(CONFIG["dataset_layout"])
    expected = WorkbookDiffService(layout).compare_local(
        SOURCE_DIR,
        TARGET_DIR,
        "AtlasConfig.xlsm",
    )

    response = api_client().post(
        "/api/diff/workbooks/compare",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == serialize_diff_json(expected)
    assert response.json()["schema_version"] == "m2.diff.v1"
    assert response.json()["direction"] == {"source": "left", "target": "right"}
    assert response.json()["workbook"]["status"] == "modified"
    assert response.json()["summary"]["modified_fields"] == 375
    assert "data" not in response.json()
    assert "result" not in response.json()
    assert "request_id" not in response.json()


@pytest.mark.parametrize(
    "status",
    [WorkbookStatus.UNCHANGED, WorkbookStatus.PARTIAL, WorkbookStatus.FAILED],
)
def test_engine_business_statuses_remain_http_200(status):
    result = result_for_status(status)
    response = api_client(service=StaticDiffService(result)).post(
        "/api/diff/workbooks/compare",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert DiffResultPayload.model_validate(response.json()) == result
    if status == WorkbookStatus.PARTIAL:
        assert [sheet["sheet_name"] for sheet in response.json()["sheets"]] == [
            "Ready",
            "Broken",
        ]


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda payload: payload.update(
                schema_version="m2.workbook-compare.request.v0"
            ),
            "DIFF_INVALID_REQUEST",
        ),
        (
            lambda payload: payload["source"].update(revision="HEAD"),
            "DIFF_INVALID_REQUEST",
        ),
        (
            lambda payload: payload.update(unknown=True),
            "DIFF_INVALID_REQUEST",
        ),
        (
            lambda payload: payload.update(workbook_path="../AtlasConfig.xlsm"),
            "DIFF_INVALID_WORKBOOK_PATH",
        ),
        (
            lambda payload: payload.update(workbook_path="D:/AtlasConfig.xlsm"),
            "DIFF_INVALID_WORKBOOK_PATH",
        ),
        (
            lambda payload: payload.update(
                workbook_path="https://example.test/AtlasConfig.xlsm"
            ),
            "DIFF_INVALID_WORKBOOK_PATH",
        ),
        (
            lambda payload: payload.update(workbook_path=r"nested\AtlasConfig.xlsm"),
            "DIFF_INVALID_WORKBOOK_PATH",
        ),
    ],
)
def test_request_contract_rejects_invalid_versions_revisions_fields_and_paths(
    mutate,
    expected_code,
):
    payload = deepcopy(request_payload())
    mutate(payload)

    response = api_client().post("/api/diff/workbooks/compare", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code


def test_invalid_json_uses_diff_orchestration_error_shape():
    response = api_client().post(
        "/api/diff/workbooks/compare",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "DIFF_INVALID_REQUEST",
            "message": "单工作簿比较请求无效",
        }
    }


@pytest.mark.parametrize(
    ("payload", "resolver", "status_code", "error_code"),
    [
        (
            {
                **request_payload(),
                "source": {"endpoint_id": "UNKNOWN", "revision": SOURCE_REVISION},
            },
            bound_resolver(),
            404,
            "DIFF_ENDPOINT_NOT_FOUND",
        ),
        (
            {
                **request_payload(),
                "source": {
                    "endpoint_id": SOURCE_ENDPOINT_ID,
                    "revision": SOURCE_REVISION + 1,
                },
            },
            bound_resolver(),
            409,
            "DIFF_SNAPSHOT_CONTEXT_MISMATCH",
        ),
        (
            request_payload(workbook_path="Other.xlsm"),
            bound_resolver(),
            404,
            "DIFF_WORKBOOK_NOT_FOUND",
        ),
        (
            request_payload(),
            bound_resolver(candidate_status="left_only"),
            422,
            "DIFF_CANDIDATE_NOT_COMPARABLE",
        ),
    ],
)
def test_dataset_resolution_errors_are_http_orchestration_errors(
    payload,
    resolver,
    status_code,
    error_code,
):
    response = api_client(resolver=resolver).post(
        "/api/diff/workbooks/compare",
        json=payload,
    )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == error_code


def test_subdirectory_path_is_reduced_to_pure_filename_for_diff_service():
    service = StaticDiffService(result_for_status(WorkbookStatus.UNCHANGED))
    resolver = bound_resolver(workbook_path="nested/AtlasConfig.xlsm")

    response = api_client(resolver=resolver, service=service).post(
        "/api/diff/workbooks/compare",
        json=request_payload(workbook_path="nested/AtlasConfig.xlsm"),
    )

    assert response.status_code == 200
    assert service.calls == [(SOURCE_DIR, TARGET_DIR, "AtlasConfig.xlsm")]


def test_unexpected_orchestration_failure_is_stable_500_without_internal_details():
    response = api_client(service=RaisingDiffService()).post(
        "/api/diff/workbooks/compare",
        json=request_payload(),
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "DIFF_ORCHESTRATION_FAILED",
            "message": "单工作簿差异比对失败",
        }
    }
    assert "internal path" not in response.text
