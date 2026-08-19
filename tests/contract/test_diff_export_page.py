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
    results_script = client.get("/static/compare_results.js")
    export_script = client.get("/static/compare_results_export.js")

    assert page.status_code == 200
    assert results_script.status_code == 200
    assert export_script.status_code == 200
    assert 'id="enter-diff-export"' in page.text
    assert 'id="diff-export-panel"' in page.text
    assert 'id="export-target-switch"' not in page.text
    assert 'name="export-target-layout"' not in page.text
    assert "右侧 TARGET 作为导出目标" in page.text
    assert 'id="export-row-filter"' not in page.text
    assert 'id="export-select-target"' in page.text
    assert 'id="export-select-source"' in page.text
    assert 'id="export-clear-visible"' in page.text
    assert '>全部右侧</button>' in page.text
    assert '>全部左侧</button>' in page.text
    assert '>恢复默认</button>' in page.text
    assert page.text.index('>全部左侧</button>') < page.text.index('>全部右侧</button>')
    assert 'id="diff-export-submit-bar"' in page.text
    assert 'id="diff-export-total-summary"' in page.text
    assert 'id="cancel-diff-export"' in page.text
    assert 'id="submit-diff-export"' in page.text
    assert "ExcelDiffExportRuntime?.createDecisionRow" in results_script.text
    assert "ExcelDiffExportRuntime?.filterRows" not in results_script.text
    assert "export-row-choice" not in results_script.text
    assert "function defaultDecisionForRow(row)" in export_script.text
    assert 'targetLayout: "target"' in export_script.text
    assert 'button.setAttribute("aria-pressed", selected ? "true" : "false")' in export_script.text
    assert '["source", "使用左侧"' in export_script.text
    assert '["target", "使用右侧"' in export_script.text
    assert export_script.text.index('["source", "使用左侧"') < export_script.text.index('["target", "使用右侧"')
    assert '["delete", "删除"' in export_script.text
    assert 'currentSheetRows().forEach' in export_script.text
    assert 'value_side: side' in export_script.text
    assert 'schema_version: "m2.export.v1"' in export_script.text
    assert 'target_layout: "target"' in export_script.text
    assert '["formal", "m4"].includes(mode)' in export_script.text
    assert 'host.state.context?.mode === "m4"' in export_script.text
    assert '"/api/diff-plans/run-results/"' in export_script.text
    assert '当前 Sheet 已恢复默认决策' in export_script.text
    assert '已跳过 ' in export_script.text
    assert 'return prefix + encodeURIComponent(result.resultRef) + "/export"' in export_script.text
