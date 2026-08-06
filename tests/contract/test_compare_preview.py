from fastapi.testclient import TestClient

from app.main import create_app
from core.svn_provider import MockSVNProvider


def test_compare_input_is_formalized_and_table_excel_only():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    response = api.get("/compare")

    assert response.status_code == 200
    assert "Config Atlas · 版本对比" in response.text
    assert "确认两个端点" in response.text
    assert "端点注册表" in response.text
    assert 'id="source-endpoint"' in response.text
    assert 'id="target-endpoint"' in response.text
    assert 'id="swap-endpoints"' in response.text
    assert 'id="create-snapshot"' in response.text
    assert 'src="http://testserver/static/compare.js?v=1.1.2"' in response.text
    assert "锁定并读取快照" in response.text
    assert "Table" in response.text
    assert "全量 Excel" in response.text
    assert "CONFIG" not in response.text
    assert "TABLECSV" not in response.text
    assert ".csv" not in response.text.lower()
    assert "Revision 下拉" not in response.text
    assert "日期" not in response.text
    assert "左侧快照" not in response.text
    assert "右侧快照" not in response.text
    assert "DIFF CANDIDATES" in response.text
    assert "snapshot-progress" in response.text


def test_settings_navigation_renames_compare_entry():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    response = api.get("/")

    assert response.status_code == 200
    assert "版本对比" in response.text
    assert "双端点对比" not in response.text

def test_compare_script_uses_registered_and_branch_candidates():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )
    response = api.get("/static/compare.js")
    assert response.status_code == 200
    assert "/api/svn/endpoints" in response.text
    assert "/api/svn/branch-candidates" in response.text
    assert "/api/svn/snapshots" in response.text
    assert "pendingRegistration" in response.text
    assert "buildDifferenceFiles" in response.text
    assert "content_hash" in response.text
    assert 'mode: state.mockMode ? "demo" : "formal"' in response.text
    assert "endpointId: state.mockMode" in response.text
    assert "/api/diff/batches" in response.text
    assert "m2.batch-create.request.v1" in response.text


def test_formal_results_page_calls_m2_diff_api_without_demo_fixture_dependency():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    page = api.get("/compare/results")
    script = api.get("/static/compare_results.js")
    mapper = api.get("/static/m2_diff_mapper.js")
    batch_script = api.get("/static/compare_results_batch.js")

    assert page.status_code == 200
    assert "m2_diff_mapper.js?v=1.0.0" in page.text
    assert "compare_results.js?v=1.2.0" in page.text
    assert "compare_results_batch.js?v=1.1.0" in page.text
    assert 'id="batch-task-panel"' in page.text
    assert "语义 Diff 服务尚未接入" not in page.text
    assert "左侧 · SOURCE" in page.text
    assert "右侧 · TARGET" in page.text
    assert "/api/diff/workbooks/compare" in script.text
    assert "m2.workbook-compare.request.v1" in script.text
    assert "M2DiffMapper.mapDiffPayload" in script.text
    assert 'const demo = context.mode === "demo"' in script.text
    assert "m2.diff.v1" in mapper.text
    assert "/api/diff/batches/" in batch_script.text
    assert "/api/diff/batch-results/" in batch_script.text
    assert "m2.batch-cancel.request.v1" in batch_script.text
    assert "m2.batch-retry.request.v1" in batch_script.text
    assert "batchTaskId" in batch_script.text
    assert "result_ref" in batch_script.text
    assert 'state: "diff_pending"' in batch_script.text
    assert "点击左侧失败工作簿查看原因" in batch_script.text
    assert 'result.itemStatus === "business_failed"' in script.text
    assert "source_only" in mapper.text
    assert '"整行"' not in mapper.text


def test_compare_demo_is_separate_and_development_only():
    dev_api = TestClient(
        create_app(
            config={"web": {"dev_mode": True}, "svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    formal_response = dev_api.get("/compare?demo=1")
    demo_response = dev_api.get("/compare/demo")
    demo_results_response = dev_api.get("/compare/demo/results")
    results_response = dev_api.get("/compare/results")
    replay_response = dev_api.get("/compare/replay")

    assert formal_response.status_code == 200
    assert 'data-demo-mode="false"' in formal_response.text
    assert 'class="compare-readable-page"' in formal_response.text
    assert "compare_readability.css?v=1.0.0" in formal_response.text
    assert "本地样本入口" not in formal_response.text
    assert demo_response.status_code == 200
    assert 'data-demo-mode="true"' in demo_response.text
    assert 'class="compare-readable-page"' in demo_response.text
    assert "compare_readability.css?v=1.0.0" in demo_response.text
    assert "Excel Diff 流程示例" in demo_response.text
    assert results_response.status_code == 200
    assert 'class="results-readable-page"' in results_response.text
    assert "compare_results_readability.css?v=1.0.0" in results_response.text
    assert "差异结果" in results_response.text
    assert demo_results_response.status_code == 200
    assert 'data-demo-mode="true"' in demo_results_response.text
    assert 'class="results-readable-page"' in demo_results_response.text
    assert "compare_results_readability.css?v=1.0.0" in demo_results_response.text
    assert "示例差异结果" in demo_results_response.text

    assert replay_response.status_code == 200
    assert 'data-replay-mode="true"' in replay_response.text
    assert 'id="offline-fixture-file"' in replay_response.text
    assert "offline_replay.js?v=1.0.0" in replay_response.text
    assert "offline_replay.css?v=1.0.0" in replay_response.text

    production_api = TestClient(
        create_app(
            config={"web": {"dev_mode": False}, "svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )
    assert production_api.get("/compare/demo").status_code == 404
    assert production_api.get("/compare/demo/results").status_code == 404
    assert production_api.get("/compare/replay").status_code == 404
    assert production_api.get("/__local_verify/atlas").status_code == 404
