"""Measure frozen directory fact caching with a deterministic local provider."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time
from typing import Any

from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from core.models import EndpointSpec, TreeEntry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.json"


class _DelayedDirectoryProvider:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self.list_tree_calls = 0
        self.list_children_calls = 0

    def list_tree(self, endpoint, prefix=""):
        self.list_tree_calls += 1
        time.sleep(self.delay_seconds)
        side = endpoint.url.rsplit("/", 1)[-1]
        return [TreeEntry(path=f"{side}/Table", kind="dir")]

    def list_children(self, endpoint, prefix=""):
        self.list_children_calls += 1
        time.sleep(self.delay_seconds)
        side = endpoint.url.rsplit("/", 1)[-1]
        return [TreeEntry(path=f"{side}/TableCsv", kind="dir")]


def _resolver(provider, layout, records):
    return SVNWorkbookDatasetResolver(
        provider,
        lambda: records,
        layout,
        allowed_schemes=("mock",),
    )


def _run(
    *,
    workbook_count: int,
    delay_seconds: float,
    shared_resolver: bool,
    layout: dict[str, Any],
) -> dict[str, Any]:
    records = [
        {
            "id": "LEFT",
            "url": "mock://repository/left",
            "physical_path_filters": {"TABLE": "left/Table"},
        },
        {
            "id": "RIGHT",
            "url": "mock://repository/right",
            "physical_path_filters": {"TABLE": "right/Table"},
        },
    ]
    endpoints = [
        EndpointSpec(url=records[0]["url"], revision=101),
        EndpointSpec(url=records[1]["url"], revision=202),
    ]
    provider = _DelayedDirectoryProvider(delay_seconds)
    resolver = _resolver(provider, layout, records)
    item_seconds: list[float] = []
    started = time.perf_counter()
    for _ in range(workbook_count):
        item_started = time.perf_counter()
        if not shared_resolver:
            resolver = _resolver(provider, layout, records)
        for record, endpoint in zip(records, endpoints, strict=True):
            table = resolver._cached_table_directory(record, endpoint)
            resolver._cached_csv_directory(record, endpoint, table)
        item_seconds.append(time.perf_counter() - item_started)
    return {
        "wall_seconds": round(time.perf_counter() - started, 6),
        "item_p50_seconds": round(statistics.median(item_seconds), 6),
        "list_tree_calls": provider.list_tree_calls,
        "list_children_calls": provider.list_children_calls,
    }


def run_acceptance(
    *,
    workbook_count: int = 55,
    delay_seconds: float = 0.005,
) -> dict[str, Any]:
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    layout = config["dataset_layout"]
    uncached = _run(
        workbook_count=workbook_count,
        delay_seconds=delay_seconds,
        shared_resolver=False,
        layout=layout,
    )
    cached = _run(
        workbook_count=workbook_count,
        delay_seconds=delay_seconds,
        shared_resolver=True,
        layout=layout,
    )
    speedup = uncached["wall_seconds"] / cached["wall_seconds"]
    return {
        "schema_version": "m2.version-comparison-directory-cache-acceptance.v1",
        "workbook_count": workbook_count,
        "provider_delay_seconds": delay_seconds,
        "uncached": uncached,
        "cached": cached,
        "wall_speedup": round(speedup, 3),
        "expected_cached_calls": {
            "list_tree": 2,
            "list_children": 2,
        },
        "writes": {
            "svn": False,
            "batch_database": False,
            "golden_fixture": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbooks", type=int, default=55)
    parser.add_argument("--delay-seconds", type=float, default=0.005)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.workbooks < 1 or args.delay_seconds < 0:
        parser.error("workbooks must be positive and delay must be non-negative")
    report = run_acceptance(
        workbook_count=args.workbooks,
        delay_seconds=args.delay_seconds,
    )
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    cached = report["cached"]
    expected = report["expected_cached_calls"]
    return 0 if (
        report["wall_speedup"] >= 2
        and cached["list_tree_calls"] == expected["list_tree"]
        and cached["list_children_calls"] == expected["list_children"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
