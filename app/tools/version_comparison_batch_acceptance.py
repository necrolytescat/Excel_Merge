"""Run the Replay fixture through a temporary local BatchDiffService."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import time
from typing import Any
from uuid import uuid4

from app.schemas.batch import BatchCreateRequestPayload
from app.schemas.diff import serialize_diff_json
from app.services.batch_diff_service import BatchDiffService
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.diff_performance_adapters import (
    TimedBatchCandidateResolver,
    TimedBatchStore,
    TimedBatchWorkbookRunner,
    TimedWorkbookDiffService,
)
from app.services.diff_performance_probe import probe_diff_functions
from app.services.offline_fixture import LoadedOfflineFixture, load_offline_fixture
from app.services.workbook_diff_service import DatasetLayout
from app.tools.version_comparison_performance import DEFAULT_FIXTURE


TERMINAL_STATUSES = {
    "completed",
    "completed_with_failures",
    "cancelled",
    "failed",
}


class _FixtureCandidateResolver:
    def __init__(self, fixture: LoadedOfflineFixture):
        self.fixture = fixture

    def validate_endpoints(self, source, target) -> None:
        if source != self.fixture.task.source or target != self.fixture.task.target:
            raise ValueError("fixture endpoint identity mismatch")

    def prepare(self, source, target):
        self.validate_endpoints(source, target)
        return [item.candidate for item in self.fixture.task.items]


class _FixtureWorkbookRunner:
    def __init__(
        self,
        fixture: LoadedOfflineFixture,
        performance: DiffPerformanceRecorder,
    ):
        self.fixture = fixture
        self.performance = performance
        self.service = TimedWorkbookDiffService(
            DatasetLayout.from_config(fixture.manifest.dataset_layout),
            performance,
        )

    def run(self, source, target, workbook_path: str) -> bytes:
        if source != self.fixture.task.source or target != self.fixture.task.target:
            raise ValueError("fixture endpoint identity mismatch")
        workbook_name = PurePosixPath(workbook_path).name
        with self.performance.phase("batch.fixture_materialize"):
            dataset = self.fixture.materialize(workbook_path)
            dataset.__enter__()
        try:
            result = self.service.compare_local(
                dataset.source_directory,
                dataset.target_directory,
                workbook_name,
            )
            with self.performance.phase("diff.serialize"):
                return serialize_diff_json(result)
        finally:
            with self.performance.phase("batch.fixture_cleanup"):
                dataset.__exit__(None, None, None)


def _golden_by_path(fixture: LoadedOfflineFixture) -> dict[str, bytes]:
    return {
        item.candidate.path: fixture.golden_results[str(item.item_id)]
        for item in fixture.task.items
        if str(item.item_id) in fixture.golden_results
    }


def run_batch_acceptance(
    fixture_path: Path,
    *,
    poll_interval_seconds: float = 0.02,
) -> dict[str, Any]:
    fixture = load_offline_fixture(fixture_path.read_bytes())
    performance = DiffPerformanceRecorder(enabled=True)
    candidate_resolver = TimedBatchCandidateResolver(
        _FixtureCandidateResolver(fixture),
        performance,
    )
    workbook_runner = TimedBatchWorkbookRunner(
        _FixtureWorkbookRunner(fixture, performance),
        performance,
    )
    golden = _golden_by_path(fixture)
    wall_started = time.perf_counter()
    first_result_seconds: float | None = None

    with TemporaryDirectory(prefix="excel-merge-batch-acceptance-") as temporary:
        state_directory = Path(temporary) / "state"
        store = TimedBatchStore(state_directory, performance=performance)
        service = BatchDiffService(
            store,
            candidate_resolver,
            workbook_runner,
            poll_interval_seconds=poll_interval_seconds,
        )
        try:
            payload = BatchCreateRequestPayload(
                schema_version="m2.batch-create.request.v1",
                request_id=uuid4(),
                source=fixture.task.source,
                target=fixture.task.target,
            )
            with probe_diff_functions(performance):
                task, created = service.create_task(payload)
                if not created:
                    raise RuntimeError("temporary batch request was not created")
                while task.status not in TERMINAL_STATUSES:
                    time.sleep(poll_interval_seconds)
                    task = service.get_task(task.task_id)
                    if first_result_seconds is None and any(
                        item.result_ref is not None for item in task.items
                    ):
                        first_result_seconds = time.perf_counter() - wall_started

            matched = 0
            mismatched = 0
            result_hashes: list[str] = []
            for item in task.items:
                expected = golden.get(item.candidate.path)
                if item.result_ref is None or expected is None:
                    mismatched += 1
                    continue
                content, digest = service.load_result(item.result_ref)
                result_hashes.append(digest)
                if content == expected:
                    matched += 1
                else:
                    mismatched += 1
            result = {
                "schema_version": "m2.version-comparison-batch-acceptance.v1",
                "task_status": task.status,
                "workbook_count": len(task.items),
                "matched_count": matched,
                "mismatched_count": mismatched,
                "first_result_seconds": round(first_result_seconds or 0.0, 6),
                "all_results_seconds": round(time.perf_counter() - wall_started, 6),
                "result_set_sha256": hashlib.sha256(
                    "\n".join(result_hashes).encode("ascii")
                ).hexdigest(),
                "performance": performance.snapshot(),
                "temporary_state_removed": False,
                "writes": {
                    "svn": False,
                    "formal_batch_database": False,
                    "golden_fixture": False,
                    "temporary_batch_database": True,
                },
            }
        finally:
            service.close()
        temporary_root = Path(temporary)
    result["temporary_state_removed"] = not temporary_root.exists()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_batch_acceptance(args.fixture)
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if (
        report["mismatched_count"] == 0
        and report["temporary_state_removed"]
        and report["task_status"] == "completed"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
