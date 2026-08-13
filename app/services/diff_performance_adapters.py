"""Opt-in timing adapters for the unchanged version-comparison services."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.services.batch_diff_service import (
    BatchCandidateResolver,
    BatchWorkbookRunner,
)
from app.services.batch_store import BatchStore
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.svn_provider import SVNProvider


class TimedSVNProvider:
    """Record provider I/O without retaining endpoint or path identities."""

    def __init__(self, inner: SVNProvider, performance: DiffPerformanceRecorder):
        self.inner = inner
        self.performance = performance

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def list_tree(self, endpoint, prefix=""):
        self.performance.increment("svn.list_tree_calls")
        with self.performance.phase("svn.list_tree"):
            return self.inner.list_tree(endpoint, prefix)

    def list_children(self, endpoint, prefix=""):
        self.performance.increment("svn.list_children_calls")
        with self.performance.phase("svn.list_children"):
            return self.inner.list_children(endpoint, prefix)

    def read_bytes(self, endpoint, path):
        suffix = Path(str(path)).suffix.casefold()
        kind = "workbook" if suffix in {".xlsx", ".xlsm", ".xls"} else "csv"
        self.performance.increment(f"svn.{kind}_read_calls")
        with self.performance.phase(f"svn.{kind}_read"):
            raw = self.inner.read_bytes(endpoint, path)
        self.performance.increment(f"svn.{kind}_read_bytes", len(raw))
        return raw


class TimedSVNWorkbookDatasetResolver(SVNWorkbookDatasetResolver):
    def __init__(
        self,
        provider: SVNProvider,
        endpoint_registry: Callable[[], Sequence[Mapping[str, Any]]],
        dataset_layout: Mapping[str, Any],
        *,
        allowed_schemes: tuple[str, ...],
        performance: DiffPerformanceRecorder,
    ):
        self.performance = performance
        super().__init__(
            TimedSVNProvider(provider, performance),
            endpoint_registry,
            dataset_layout,
            allowed_schemes=allowed_schemes,
        )

    def _manifest(self, raw: bytes):
        with self.performance.phase("resolver.manifest_parse"):
            return super()._manifest(raw)

    def _read_csv_files(self, endpoint, csv_directory, manifest):
        with self.performance.phase("resolver.csv_fetch"):
            return super()._read_csv_files(endpoint, csv_directory, manifest)

    def _read_csv_side(self, *args, **kwargs):
        with self.performance.phase("resolver.csv_fetch"):
            return super()._read_csv_side(*args, **kwargs)

    def _write_side(
        self,
        directory: Path,
        workbook_name: str,
        workbook_raw: bytes,
        csv_files: Mapping[str, bytes],
    ) -> None:
        with self.performance.phase("resolver.materialize"):
            SVNWorkbookDatasetResolver._write_side(
                directory,
                workbook_name,
                workbook_raw,
                csv_files,
            )

    def resolve(self, payload):
        with self.performance.phase("resolver.total"):
            dataset = super().resolve(payload)
        try:
            materialized_bytes = sum(
                path.stat().st_size
                for directory in (dataset.source_directory, dataset.target_directory)
                for path in directory.iterdir()
                if path.is_file()
            )
        except OSError:
            materialized_bytes = 0
        self.performance.increment("resolver.materialize_bytes", materialized_bytes)
        return dataset


class TimedWorkbookDiffService(WorkbookDiffService):
    def __init__(self, layout: DatasetLayout, performance: DiffPerformanceRecorder):
        super().__init__(layout)
        self.performance = performance

    def _manifest(self, raw: bytes):
        with self.performance.phase("diff.manifest_parse"):
            return super()._manifest(raw)

    def _parse_csv(self, *args, **kwargs):
        with self.performance.phase("diff.csv_read_parse"):
            result = super()._parse_csv(*args, **kwargs)
        reference = result[1]
        if reference is not None:
            self.performance.increment("diff.csv_read_parse_calls")
        return result

    def _sheet_payload(self, *args, **kwargs):
        with self.performance.phase("diff.sheet_total"):
            return super()._sheet_payload(*args, **kwargs)

    def compare_local(
        self,
        source_directory: Path,
        target_directory: Path,
        workbook_name: str,
    ):
        with self.performance.phase("diff.total"):
            return super().compare_local(
                source_directory,
                target_directory,
                workbook_name,
            )


class TimedBatchCandidateResolver:
    def __init__(
        self,
        inner: BatchCandidateResolver,
        performance: DiffPerformanceRecorder,
    ):
        self.inner = inner
        self.performance = performance

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def prepare(self, source, target):
        self.performance.increment("batch.prepare_calls")
        with self.performance.phase("batch.prepare"):
            return self.inner.prepare(source, target)


class TimedBatchWorkbookRunner:
    def __init__(
        self,
        inner: BatchWorkbookRunner,
        performance: DiffPerformanceRecorder,
    ):
        self.inner = inner
        self.performance = performance

    def run(self, source, target, workbook_path):
        self.performance.increment("batch.workbook_calls")
        with self.performance.phase("batch.workbook"):
            return self.inner.run(source, target, workbook_path)


class TimedBatchStore(BatchStore):
    def __init__(
        self,
        state_directory: Path,
        *,
        performance: DiffPerformanceRecorder,
        event_retention_days: int = 90,
    ):
        super().__init__(
            state_directory,
            event_retention_days=event_retention_days,
        )
        self.performance = performance

    def create_task(self, *args, **kwargs):
        with self.performance.phase("store.sqlite.create_task"):
            return super().create_task(*args, **kwargs)

    def claim_preparation(self):
        with self.performance.phase("store.sqlite.claim_preparation"):
            return super().claim_preparation()

    def complete_preparation(self, task_id, prepared):
        with self.performance.phase("store.sqlite.complete_preparation"):
            return super().complete_preparation(task_id, prepared)

    def fail_preparation(self, *args, **kwargs):
        with self.performance.phase("store.sqlite.fail_preparation"):
            return super().fail_preparation(*args, **kwargs)

    def claim_next_item(self):
        with self.performance.phase("store.sqlite.claim_item"):
            return super().claim_next_item()

    def renew_lease(self, item_id, lease_token):
        with self.performance.phase("store.sqlite.renew_lease"):
            return super().renew_lease(item_id, lease_token)

    def write_result_blob(self, task_id, item_id, content):
        self.performance.increment("store.result_input_bytes", len(content))
        with self.performance.phase("store.result_gzip_fsync"):
            return super().write_result_blob(task_id, item_id, content)

    def complete_item_result(self, **kwargs):
        with self.performance.phase("store.sqlite.complete_item"):
            return super().complete_item_result(**kwargs)

    def fail_item(self, **kwargs):
        with self.performance.phase("store.sqlite.fail_item"):
            return super().fail_item(**kwargs)

    def cancel_task(self, *args, **kwargs):
        with self.performance.phase("store.sqlite.cancel_task"):
            return super().cancel_task(*args, **kwargs)

    def recover(self):
        with self.performance.phase("store.sqlite.recover"):
            return super().recover()

    def recover_expired_leases(self, *, force=False):
        with self.performance.phase("store.sqlite.recover_expired_leases"):
            return super().recover_expired_leases(force=force)

    def load_result(self, result_ref):
        with self.performance.phase("store.result_load"):
            return super().load_result(result_ref)
