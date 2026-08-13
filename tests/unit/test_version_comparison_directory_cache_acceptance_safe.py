from __future__ import annotations

import json

from app.tools import version_comparison_directory_cache_acceptance_safe as safe


def test_safe_entrypoint_redacts_unexpected_failures(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("secret://repository/hidden")

    monkeypatch.setattr(safe, "acceptance_main", fail)

    assert safe.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "code": "acceptance_internal_error",
    }
