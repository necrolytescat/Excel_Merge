from __future__ import annotations

import pytest

from app.services import workbook_diff_service
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.diff_performance_probe import probe_diff_functions


def test_probe_restores_core_bindings_after_success_and_failure():
    original_csv = workbook_diff_service.parse_table_csv
    original_diff = workbook_diff_service.diff_table_csv
    recorder = DiffPerformanceRecorder(enabled=True)

    with probe_diff_functions(recorder):
        assert workbook_diff_service.parse_table_csv is not original_csv
        assert workbook_diff_service.diff_table_csv is not original_diff
    assert workbook_diff_service.parse_table_csv is original_csv
    assert workbook_diff_service.diff_table_csv is original_diff

    with pytest.raises(RuntimeError):
        with probe_diff_functions(recorder):
            raise RuntimeError("fixture failure")
    assert workbook_diff_service.parse_table_csv is original_csv
    assert workbook_diff_service.diff_table_csv is original_diff
