from __future__ import annotations

import json

from app.tools import version_comparison_performance_safe as safe_benchmark


def test_safe_entrypoint_redacts_unexpected_failures(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("secret://repository/hidden/Table.xlsm")

    monkeypatch.setattr(safe_benchmark, "benchmark_main", fail)

    assert safe_benchmark.main([]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "failed", "code": "benchmark_internal_error"}
