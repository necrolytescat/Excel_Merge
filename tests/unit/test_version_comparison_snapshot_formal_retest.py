from pathlib import Path

import pytest

from app.tools import version_comparison_snapshot_formal_retest as tool
from app.tools import version_comparison_snapshot_formal_retest_safe as safe


def test_retest_rejects_unregistered_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tool,
        "load_config",
        lambda path: {"svn": {"endpoint_registry": [{"id": "source"}]}},
    )

    with pytest.raises(ValueError, match="not registered"):
        tool.run_retest(
            config_path=tmp_path / "settings.json",
            source_id="source",
            source_revision=1,
            target_id="target",
            target_revision=2,
        )


def test_safe_entrypoint_hides_repository_details(monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("secret repository")

    monkeypatch.setattr(safe, "retest_main", fail)

    assert safe.main([]) == 2
    output = capsys.readouterr().out
    assert "retest_internal_error" in output
    assert "secret repository" not in output
