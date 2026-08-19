from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from core.svn_provider import MockSVNProvider


def test_results_page_exposes_export_controls_and_decision_contract() -> None:
    client = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )
    page = client.get("/compare/results")
    script = client.get("/static/compare_results.js")

    assert page.status_code == 200
    assert 'id="export-target-switch"' in page.text
    assert 'name="export-target-layout" value="source"' in page.text
    assert 'name="export-target-layout" value="target"' in page.text
    assert 'id="export-workbook"' in page.text
    assert 'id="export-select-source"' in page.text
    assert 'id="export-select-target"' in page.text
    assert 'id="export-clear-sheet"' in page.text
    assert 'id="diff-export-selection-summary"' in page.text
    assert 'schema_version: "m2.export.v1"' in script.text
    assert 'action: "write"' in script.text
    assert 'action === "delete"' in script.text
    assert '切换目标结构会清空当前工作簿所有 Sheet 的导出选择' in script.text
    assert '"/api/diff/batch-results/" + encodeURIComponent(result.resultRef) + "/export"' in script.text
