"""Measure manifest reuse with the registered offline Replay fixture."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path, PurePosixPath
import statistics
import time
from typing import Any

from app.schemas.diff import serialize_diff_json
from app.services.offline_fixture import load_offline_fixture
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from app.tools.version_comparison_performance import (
    DEFAULT_FIXTURE,
    _peak_working_set_bytes,
)

EXPECTED_RESULT_SET_SHA256 = (
    "d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f"
)


class _CountingManifestService(WorkbookDiffService):
    def __init__(self, layout: DatasetLayout):
        super().__init__(layout)
        self.manifest_calls = 0

    def _manifest(self, raw):
        self.manifest_calls += 1
        return super()._manifest(raw)


def _result_set_sha256(result_hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(result_hashes).encode("ascii")).hexdigest()


def _run_round(fixture, layout: DatasetLayout) -> dict[str, Any]:
    resolver_parser = _CountingManifestService(layout)
    legacy_diff = _CountingManifestService(layout)
    reused_diff = _CountingManifestService(layout)
    resolver_manifest_seconds = 0.0
    legacy_diff_seconds = 0.0
    reused_diff_seconds = 0.0
    matched = 0
    mismatched = 0
    result_hashes: list[str] = []
    started = time.perf_counter()

    for result_manifest in fixture.manifest.results:
        item_id = str(result_manifest.item_id)
        workbook_name = PurePosixPath(result_manifest.workbook_path).name
        with fixture.materialize(result_manifest.workbook_path) as dataset:
            source_path = dataset.source_directory / workbook_name
            target_path = dataset.target_directory / workbook_name
            source_raw = source_path.read_bytes()
            target_raw = target_path.read_bytes()

            phase_started = time.perf_counter()
            source_manifest = resolver_parser._manifest(source_raw)
            target_manifest = resolver_parser._manifest(target_raw)
            resolver_manifest_seconds += time.perf_counter() - phase_started

            phase_started = time.perf_counter()
            legacy_content = serialize_diff_json(
                legacy_diff.compare_local(
                    dataset.source_directory,
                    dataset.target_directory,
                    workbook_name,
                )
            )
            legacy_diff_seconds += time.perf_counter() - phase_started

            phase_started = time.perf_counter()
            reused_content = serialize_diff_json(
                reused_diff.compare_local(
                    dataset.source_directory,
                    dataset.target_directory,
                    workbook_name,
                    source_manifest=source_manifest,
                    target_manifest=target_manifest,
                )
            )
            reused_diff_seconds += time.perf_counter() - phase_started

        golden = fixture.golden_results[item_id]
        if reused_content == legacy_content == golden:
            matched += 1
        else:
            mismatched += 1
        result_hashes.append(hashlib.sha256(reused_content).hexdigest())

    legacy_total = resolver_manifest_seconds + legacy_diff_seconds
    reused_total = resolver_manifest_seconds + reused_diff_seconds
    return {
        "workbook_count": len(fixture.manifest.results),
        "matched_count": matched,
        "mismatched_count": mismatched,
        "resolver_manifest_calls": resolver_parser.manifest_calls,
        "legacy_diff_manifest_calls": legacy_diff.manifest_calls,
        "reused_diff_manifest_calls": reused_diff.manifest_calls,
        "resolver_manifest_seconds": round(resolver_manifest_seconds, 6),
        "legacy_diff_seconds": round(legacy_diff_seconds, 6),
        "reused_diff_seconds": round(reused_diff_seconds, 6),
        "legacy_equivalent_seconds": round(legacy_total, 6),
        "reused_equivalent_seconds": round(reused_total, 6),
        "saved_seconds": round(legacy_total - reused_total, 6),
        "speedup": round(legacy_total / reused_total, 3),
        "result_set_sha256": _result_set_sha256(result_hashes),
        "round_wall_seconds": round(time.perf_counter() - started, 6),
    }


def run_acceptance(fixture_path: Path, *, rounds: int = 5) -> dict[str, Any]:
    raw = fixture_path.read_bytes()
    fixture = load_offline_fixture(raw)
    layout = DatasetLayout.from_config(fixture.manifest.dataset_layout)
    runs = []
    for _ in range(rounds):
        runs.append(_run_round(fixture, layout))
        # Materialization creates cyclic parser objects; release them before
        # starting the next round to keep the Windows working set bounded.
        gc.collect()
    all_rounds_passed = all(
        run["matched_count"] == len(fixture.golden_results)
        and run["mismatched_count"] == 0
        and run["result_set_sha256"] == EXPECTED_RESULT_SET_SHA256
        for run in runs
    )
    return {
        "schema_version": "m2.version-comparison-manifest-reuse-acceptance.v1",
        "fixture": {
            "archive_sha256": fixture.archive_sha256,
            "archive_size_bytes": len(raw),
            "golden_result_count": len(fixture.golden_results),
        },
        "rounds": runs,
        "summary": {
            "requested_rounds": rounds,
            "completed_rounds": len(runs),
            "expected_result_set_sha256": EXPECTED_RESULT_SET_SHA256,
            "all_rounds_passed": all_rounds_passed,
            "legacy_equivalent_p50_seconds": round(
                statistics.median(run["legacy_equivalent_seconds"] for run in runs),
                6,
            ),
            "reused_equivalent_p50_seconds": round(
                statistics.median(run["reused_equivalent_seconds"] for run in runs),
                6,
            ),
            "saved_p50_seconds": round(
                statistics.median(run["saved_seconds"] for run in runs),
                6,
            ),
            "speedup_p50": round(
                statistics.median(run["speedup"] for run in runs),
                3,
            ),
            "peak_working_set_bytes": _peak_working_set_bytes(),
            "unique_result_set_sha256": len(
                {run["result_set_sha256"] for run in runs}
            ),
        },
        "writes": {
            "svn": False,
            "batch_database": False,
            "golden_fixture": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    report = run_acceptance(args.fixture, rounds=args.rounds)
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    expected_calls = report["fixture"]["golden_result_count"] * 2
    return 0 if (
        report["summary"]["requested_rounds"] == args.rounds
        and report["summary"]["completed_rounds"] == args.rounds
        and report["summary"]["unique_result_set_sha256"] == 1
        and report["summary"]["all_rounds_passed"]
        and all(
            run["matched_count"] == report["fixture"]["golden_result_count"]
            and run["mismatched_count"] == 0
            and run["result_set_sha256"] == EXPECTED_RESULT_SET_SHA256
            and run["resolver_manifest_calls"] == expected_calls
            and run["legacy_diff_manifest_calls"] == expected_calls
            and run["reused_diff_manifest_calls"] == 0
            for run in report["rounds"]
        )
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
