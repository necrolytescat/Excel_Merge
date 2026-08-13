from __future__ import annotations

import json

from app.tools import version_comparison_batch_acceptance_safe as safe_acceptance


def test_safe_entrypoint_redacts_unexpected_failures(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("secret://repository/hidden/Table.xlsm")

    monkeypatch.setattr(safe_acceptance, "acceptance_main", fail)

    assert safe_acceptance.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "code": "acceptance_internal_error",
    }
