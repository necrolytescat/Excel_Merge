from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.diff_plan_service import DiffPlanService, DiffPlanWorkbookCatalogService
from app.services.diff_plan_store import DiffPlanStore
from app.services.diff_plan_store import DiffPlanError
from app.schemas.diff_plan import DiffPlanRunListPayload, DiffPlanRunPayload
from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry
from core.svn_provider import MockSVNProvider


class CatalogProvider(MockSVNProvider):
    def info(self, endpoint):
        return SvnInfo(
            url=endpoint.url,
            repository_root="mock://repo",
            repository_uuid="m4-test",
            revision="120",
            last_changed_revision="120",
            last_changed_author="tester",
            last_changed_date="2026-08-12T00:00:00Z",
        )

    def list_tree(self, endpoint):
        return [
            TreeEntry(path="Game/Table", kind="dir"),
            TreeEntry(path="Game/Table/Battle", kind="dir"),
            TreeEntry(path="Game/Table/Battle/Hero.xlsx", kind="file", size=1024, revision="119"),
            TreeEntry(path="Game/Table/Skill.xlsm", kind="file", size=2048, revision="120"),
            TreeEntry(path="Game/TableCsv/Hero.csv", kind="file", size=100, revision="120"),
        ]


def build_client(tmp_path: Path):
    provider = CatalogProvider()
    config = {
        "svn": {
            "provider": "mock",
            "endpoint_registry": [
                {"id": "source", "region": "TC", "track": "DEV", "label": "开发主干", "url": "mock://source", "logical_scopes": ["TABLE"], "physical_path_filters": {"TABLE": "Game/Table"}, "enabled": True},
                {"id": "target", "region": "TC", "track": "FIX", "label": "发布分支", "url": "mock://target", "logical_scopes": ["TABLE"], "physical_path_filters": {"TABLE": "Game/Table"}, "enabled": True},
            ],
        }
    }
    snapshot = SnapshotService(provider, allowed_schemes=("mock",))
    service = DiffPlanService(
        DiffPlanStore(tmp_path / "m4.sqlite3"),
        DiffPlanWorkbookCatalogService(provider, snapshot, lambda: config["svn"]["endpoint_registry"]),
        lambda: config["svn"]["endpoint_registry"],
    )
    return TestClient(create_app(config=config, provider=provider, diff_plan_service=service))


def test_workbook_catalog_and_plan_crud_contract(tmp_path):
    api = build_client(tmp_path)
    catalog = api.post("/api/diff-plans/workbook-catalog", json={
        "schema_version": "m4.workbook-catalog.request.v1",
        "endpoint_id": "source",
        "revision": "HEAD",
    })
    assert catalog.status_code == 200
    assert catalog.json()["resolved_revision"] == 120
    assert [item["path"] for item in catalog.json()["workbooks"]] == ["Battle/Hero.xlsx", "Skill.xlsm"]

    request_id = str(uuid4())
    body = {
        "schema_version": "m4.diff-plan-create.request.v1",
        "request_id": request_id,
        "name": "战斗核心表",
        "source_endpoint_id": "source",
        "target_endpoint_ids": ["target"],
        "workbook_paths": ["Battle/Hero.xlsx"],
    }
    created = api.post("/api/diff-plans", json=body)
    replay = api.post("/api/diff-plans", json=body)
    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.json()["plan_id"] == replay.json()["plan_id"]

    plan_id = created.json()["plan_id"]
    assert api.get("/api/diff-plans").json()["total"] == 1
    assert api.get(f"/api/diff-plans/{plan_id}").headers["etag"]

    archived = api.post(f"/api/diff-plans/{plan_id}/archive", json={
        "schema_version": "m4.diff-plan-command.request.v1",
        "request_id": str(uuid4()),
        "expected_version": 1,
    })
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert api.get("/api/diff-plans?archived=true").json()["total"] == 1


def test_plan_api_rejects_non_table_workbook_and_unknown_fields(tmp_path):
    api = build_client(tmp_path)
    invalid = api.post("/api/diff-plans", json={
        "schema_version": "m4.diff-plan-create.request.v1",
        "request_id": str(uuid4()),
        "name": "非法计划",
        "source_endpoint_id": "source",
        "target_endpoint_ids": ["target"],
        "workbook_paths": ["Other/Hidden.xlsx"],
    })
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "DIFF_PLAN_WORKBOOK_NOT_IN_SOURCE"

    unknown = api.post("/api/diff-plans/workbook-catalog", json={
        "schema_version": "m4.workbook-catalog.request.v1",
        "endpoint_id": "source",
        "revision": "HEAD",
        "unexpected": True,
    })
    assert unknown.status_code == 422 or unknown.status_code == 400


def test_diff_plan_pages_and_navigation_contract(tmp_path):
    api = build_client(tmp_path)
    for path in ("/diff-plans", "/diff-plans/new"):
        response = api.get(path)
        assert response.status_code == 200
        assert '<span>表格计划对比</span><span class="nav-current">M4</span>' in response.text
        assert 'href="/diff-plans" aria-current="page"' in response.text

    index = api.get("/diff-plans")
    form = api.get("/diff-plans/new")
    assert "计划列表" in index.text
    assert "有效计划" in index.text
    assert "已归档" in index.text
    assert "选择 TABLE 表格" in form.text
    assert "0 / 10" in form.text
    assert "0 / 4" in form.text
    assert "仅保存" in form.text
    assert "保存并开始" in form.text
    assert "本次运行 Revision 设置" in form.text
    assert 'id="source-endpoint-query"' in form.text
    assert 'id="source-endpoint-options" role="listbox"' in form.text
    assert 'id="target-endpoint-query"' in form.text
    assert 'id="target-list" role="listbox" aria-multiselectable="true"' in form.text
    assert form.text.count('role="combobox"') == 2

    script = (Path(__file__).parents[2] / "app" / "static" / "diff_plan_form.js").read_text(encoding="utf-8")
    assert "/api/svn/config" in script
    assert "/api/svn/endpoints" in script
    assert "/api/svn/branch-candidates" in script
    assert "endpointIdForMatch" in script
    assert "ensureEndpointsRegistered" in script


class StubRunService:
    def __init__(self, plan_id, *, expired=False):
        self.plan_id = plan_id
        self.expired = expired

    def payload(self):
        return DiffPlanRunPayload.model_validate({
            "run_id": "00000000-0000-4000-8000-000000000010",
            "plan_id": str(self.plan_id),
            "retry_of_run_id": None,
            "status": "completed",
            "plan_version": 1,
            "plan_name": "战斗核心表",
            "source_endpoint_id": "source",
            "target_endpoint_ids": ["target"],
            "workbook_paths": ["Battle/Hero.xlsx"],
            "source_revision": 120,
            "target_revisions": {"target": 121},
            "progress": {"total_items": 1, "processed_items": 1, "identical_items": 0, "semantic_equal_items": 0, "changed_items": 1, "missing_items": 0, "failed_items": 0, "cancelled_items": 0, "ratio": 1},
            "items": [{
                "item_id": "00000000-0000-4000-8000-000000000011",
                "ordinal": 0, "workbook_path": "Battle/Hero.xlsx", "target_endpoint_id": "target",
                "status": "changed", "candidate_status": "modified", "source_exists": True,
                "target_exists": True, "source_sha256": "a" * 64, "target_sha256": "b" * 64,
                "diff_status": "modified", "diff_error_count": 0, "result_ref": "m4r_1234567890123456789012",
            }],
            "created_at": "2026-08-12T00:00:00Z", "started_at": "2026-08-12T00:00:01Z",
            "finished_at": "2026-08-12T00:00:02Z",
            "details_expires_at": "2000-01-01T00:00:02Z" if self.expired else "2099-09-11T00:00:02Z",
            "details_expired": self.expired,
        })

    def start_run(self, plan_id, payload):
        assert str(plan_id) == str(self.plan_id)
        return self.payload(), True

    def get_run(self, run_id):
        return self.payload()

    def list_runs(self, plan_id):
        payload = self.payload()
        from app.schemas.diff_plan import DiffPlanRunSummaryPayload
        summary = {
            key: value for key, value in payload.model_dump(mode="json").items()
            if key in DiffPlanRunSummaryPayload.model_fields
        }
        summary["schema_version"] = "m4.diff-plan-run-summary.v1"
        return DiffPlanRunListPayload(runs=[DiffPlanRunSummaryPayload.model_validate(summary)], total=1)

    def cancel(self, run_id, payload):
        return self.payload()

    def retry(self, run_id, payload):
        return self.payload(), True

    def load_result(self, result_ref):
        if self.expired:
            raise DiffPlanError("DIFF_PLAN_RESULT_EXPIRED", "运行明细已过期，矩阵摘要仍可查看", status_code=410)
        content = b'{"schema_version":"m2.diff.v1"}'
        return content, "a" * 64


def test_diff_plan_run_api_and_reused_result_page_contract(tmp_path):
    api = build_client(tmp_path)
    created = api.post("/api/diff-plans", json={
        "schema_version": "m4.diff-plan-create.request.v1", "request_id": str(uuid4()),
        "name": "战斗核心表", "source_endpoint_id": "source",
        "target_endpoint_ids": ["target"], "workbook_paths": ["Battle/Hero.xlsx"],
    }).json()
    api.app.state.diff_plan_run_service = StubRunService(created["plan_id"])

    started = api.post(f"/api/diff-plans/{created['plan_id']}/runs", json={
        "schema_version": "m4.diff-plan-run-start.request.v1", "request_id": str(uuid4()), "revisions": {},
    })
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert api.get(f"/api/diff-plans/runs/{run_id}").json()["schema_version"] == "m4.diff-plan-run.v1"
    assert api.get(f"/api/diff-plans/{created['plan_id']}/runs").json()["total"] == 1
    result = api.get("/api/diff-plans/run-results/m4r_1234567890123456789012")
    assert result.status_code == 200
    assert result.json()["schema_version"] == "m2.diff.v1"

    page = api.get(f"/diff-plan-runs/{run_id}")
    assert page.status_code == 200
    assert 'data-m4-run-id="' + run_id + '"' in page.text
    assert "m2_diff_mapper.js" in page.text
    assert "compare_results.js" in page.text
    assert "diff_plan_run.js" in page.text
    assert "compare_results_batch.js" not in page.text
    assert 'id="m4-matrix-panel"' in page.text
    assert 'id="m4-run-tabs"' in page.text

    m2_page = api.get("/compare/results")
    assert "compare_results_batch.js" in m2_page.text
    assert "diff_plan_run.js" not in m2_page.text


def test_expired_run_keeps_matrix_and_result_endpoint_returns_410(tmp_path):
    api = build_client(tmp_path)
    created = api.post("/api/diff-plans", json={
        "schema_version": "m4.diff-plan-create.request.v1", "request_id": str(uuid4()),
        "name": "过期明细计划", "source_endpoint_id": "source",
        "target_endpoint_ids": ["target"], "workbook_paths": ["Battle/Hero.xlsx"],
    }).json()
    service = StubRunService(created["plan_id"], expired=True)
    api.app.state.diff_plan_run_service = service
    run = service.payload()

    response = api.get(f"/api/diff-plans/runs/{run.run_id}")
    assert response.status_code == 200
    assert response.json()["details_expired"] is True
    assert response.json()["progress"]["changed_items"] == 1
    assert response.json()["items"][0]["result_ref"]

    detail = api.get("/api/diff-plans/run-results/m4r_1234567890123456789012")
    assert detail.status_code == 410
    assert detail.json() == {
        "error": {"code": "DIFF_PLAN_RESULT_EXPIRED", "message": "运行明细已过期，矩阵摘要仍可查看"}
    }
    script = (Path(__file__).parents[2] / "app" / "static" / "diff_plan_run.js").read_text(encoding="utf-8")
    assert "明细已过期，矩阵摘要与冻结 Revision 仍长期保留" in script
