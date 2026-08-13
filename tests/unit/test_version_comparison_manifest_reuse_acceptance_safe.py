from app.tools import version_comparison_manifest_reuse_acceptance_safe as safe


def test_safe_entrypoint_returns_nonzero_without_exception_details(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("secret workbook")

    monkeypatch.setattr(safe, "acceptance_main", fail)

    assert safe.main([]) == 2
    output = capsys.readouterr().out
    assert "acceptance_internal_error" in output
    assert "secret workbook" not in output
