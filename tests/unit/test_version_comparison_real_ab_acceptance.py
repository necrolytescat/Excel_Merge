from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.tools import version_comparison_real_ab_acceptance as acceptance


class _InMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def info(self, endpoint):
        self.calls.append("info")
        return {"revision": endpoint.revision}

    def read_bytes(self, endpoint, path: str) -> bytes:
        self.calls.append(f"read_bytes:{path}")
        return b"csv"

    def read_bytes_with_source(self, endpoint, path: str) -> tuple[bytes, str]:
        self.calls.append(f"read_bytes_with_source:{path}")
        return b"xlsx", "memory"


@pytest.mark.parametrize("revision", ["HEAD", None, 0, -1, True])
def test_counting_provider_rejects_head_and_non_fixed_revisions(revision):
    inner = _InMemoryProvider()
    provider = acceptance._CountingProvider(inner)

    with pytest.raises(RuntimeError, match="forbids HEAD or non-fixed Revision"):
        provider.info(SimpleNamespace(revision=revision))

    assert inner.calls == []
    assert provider.snapshot()["calls"] == {}


def test_counting_provider_counts_only_item_phase_content_calls():
    endpoint = SimpleNamespace(revision=123)
    provider = acceptance._CountingProvider(_InMemoryProvider())

    provider.set_phase("prepare")
    assert provider.read_bytes(endpoint, "TableCsv/prepare.csv") == b"csv"

    provider.set_phase("item")
    assert provider.info(endpoint) == {"revision": 123}
    assert provider.read_bytes(endpoint, "TableCsv/item.csv") == b"csv"
    assert provider.read_bytes_with_source(endpoint, "Table/item.xlsx") == (
        b"xlsx",
        "memory",
    )

    provider.set_phase("between")
    assert provider.read_bytes(endpoint, "TableCsv/between.csv") == b"csv"

    snapshot = provider.snapshot()
    assert snapshot["phase_calls"] == {
        "between.read_bytes": 1,
        "item.info": 1,
        "item.read_bytes": 1,
        "item.read_bytes_with_source": 1,
        "prepare.read_bytes": 1,
    }
    assert snapshot["item_content_calls"] == 2


def test_main_redacts_unexpected_failure(monkeypatch, capsys, tmp_path):
    secret = "svn+ssh://user:password@example.invalid/private/Table.xlsx"

    def fail(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(acceptance, "run_acceptance", fail)

    exit_code = acceptance.main(
        [
            "--config",
            str(tmp_path / "settings.json"),
            "--fixture",
            str(tmp_path / "fixture.json"),
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(output.out) == {
        "status": "failed",
        "code": "real_ab_internal_error",
    }
    assert output.err == ""
    assert secret not in output.out
