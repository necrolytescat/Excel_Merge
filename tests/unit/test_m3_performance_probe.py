import json

from app.tools import m3_performance_probe


def test_probe_redacts_unexpected_diagnostic_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        m3_performance_probe,
        "diagnostic_main",
        lambda argv: (_ for _ in ()).throw(RuntimeError("private path and URL")),
    )

    assert m3_performance_probe.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "code": "diagnostic_internal_error",
    }
