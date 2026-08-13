from app.tools import version_comparison_snapshot_parallel_acceptance_safe as safe


def test_safe_entrypoint_returns_nonzero_without_exception_details(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("secret repository")

    monkeypatch.setattr(safe, "acceptance_main", fail)

    assert safe.main([]) == 2
    output = capsys.readouterr().out
    assert "acceptance_internal_error" in output
    assert "secret repository" not in output
