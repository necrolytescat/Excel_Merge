"""Temporary function-level probes for the isolated Replay benchmark."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.services.diff_performance import DiffPerformanceRecorder
from app.services import workbook_diff_service


@contextmanager
def probe_diff_functions(
    performance: DiffPerformanceRecorder,
) -> Iterator[None]:
    """Time core calls and restore module bindings even when Replay fails."""
    original_csv_parser = workbook_diff_service.parse_table_csv
    original_semantic_diff = workbook_diff_service.diff_table_csv

    def timed_csv_parser(*args, **kwargs):
        with performance.phase("diff.csv_parse"):
            return original_csv_parser(*args, **kwargs)

    def timed_semantic_diff(*args, **kwargs):
        with performance.phase("diff.semantic"):
            return original_semantic_diff(*args, **kwargs)

    workbook_diff_service.parse_table_csv = timed_csv_parser
    workbook_diff_service.diff_table_csv = timed_semantic_diff
    try:
        yield
    finally:
        workbook_diff_service.parse_table_csv = original_csv_parser
        workbook_diff_service.diff_table_csv = original_semantic_diff
