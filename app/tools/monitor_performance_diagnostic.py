"""Read-only, non-publishing M3 legacy/incremental performance diagnostic."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import ctypes
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import time
from typing import Any

from app.services.branch_history_service import BranchHistoryService
from app.services.config_service import ConfigStore
from app.services.monitor_attribution_service import MonitorAttributionService
from app.services.monitor_diff_service import MonitorDiffService, SvnMonitorSnapshotReader
from app.services.monitor_incremental_service import (
    MonitorIncrementalReplayService,
    compare_legacy_and_incremental,
)
from app.services.monitor_performance import (
    MonitorPerformanceRecorder,
    monitor_semantic_fingerprint,
)
from app.services.workbook_diff_service import DatasetLayout
from core.models import EndpointSpec
from core.svn_history import BranchIdentity, parse_svn_datetime
from core.svn_provider import CLISVNProvider, SVNProviderError, provider_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"


class DiagnosticGateError(RuntimeError):
    pass


class DiagnosticHistoryService(BranchHistoryService):
    def __init__(self, provider, performance: MonitorPerformanceRecorder):
        super().__init__(provider)
        self.performance = performance

    def resolve_branch_identity(self, endpoint):
        self.performance.increment("svn.info_calls")
        with self.performance.phase("svn.info"):
            return super().resolve_branch_identity(endpoint)

    def verify_branch_identity(self, endpoint, expected):
        self.performance.increment("svn.info_calls")
        with self.performance.phase("svn.info"):
            return super().verify_branch_identity(endpoint, expected)

    def resolve_revision_at(self, identity, instant):
        self.performance.increment("svn.log_date_calls")
        with self.performance.phase("svn.log_date"):
            return super().resolve_revision_at(identity, instant)

    def list_branch_commits(self, identity, start, end):
        # The current CLI implementation resolves both date boundaries internally.
        self.performance.increment("svn.log_date_calls", 2)
        self.performance.increment("svn.log_range_calls")
        with self.performance.phase("svn.log_range"):
            return super().list_branch_commits(identity, start, end)

    def read_path_bytes_at_revision(self, identity, path, revision):
        self.performance.increment("svn.cat_requests")
        with self.performance.phase("svn.cat"):
            raw = super().read_path_bytes_at_revision(identity, path, revision)
        self.performance.increment("svn.cat_bytes", len(raw))
        return raw

    def list_paths_at_revision(self, identity, revision):
        self.performance.increment("svn.list_recursive_calls")
        with self.performance.phase("svn.list_recursive"):
            return super().list_paths_at_revision(identity, revision)

    def resolve_copy_boundary(self, identity):
        self.performance.increment("svn.log_copy_boundary_calls")
        with self.performance.phase("svn.log_copy_boundary"):
            return super().resolve_copy_boundary(identity)


def _peak_working_set_bytes() -> int | None:
    if not hasattr(ctypes, "windll"):
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    )
    return int(counters.PeakWorkingSetSize) if ok else None


def _record(config: dict[str, Any], endpoint_id: str) -> dict[str, Any]:
    records = list(dict(config.get("svn") or {}).get("endpoint_registry") or [])
    matches = [record for record in records if str(record.get("id")) == endpoint_id]
    if len(matches) != 1 or not bool(matches[0].get("enabled", True)):
        raise DiagnosticGateError("endpoint_not_available")
    record = dict(matches[0])
    if not str(record.get("url", "")).strip():
        raise DiagnosticGateError("endpoint_url_missing")
    return record


def _table_directory(
    history: DiagnosticHistoryService,
    identity: BranchIdentity,
    revision: int,
    directory_name: str,
) -> str:
    candidates = set()
    for entry in history.list_paths_at_revision(identity, revision):
        path = PurePosixPath(entry.path)
        for parent in path.parents:
            value = parent.as_posix()
            if value != "." and parent.name.casefold() == directory_name.casefold():
                candidates.add(value)
    if not candidates:
        raise DiagnosticGateError("table_directory_missing")
    return sorted(candidates, key=lambda value: (value.count("/"), value.casefold()))[0]


def _summary(result) -> dict[str, int]:
    return {
        "workbook_count": result.workbook_count,
        "reliable_workbook_count": result.reliable_workbook_count,
        "change_count": len(result.changes),
        "error_count": len(result.errors),
        "unknown_author_count": sum(
            change.attribution.status == "unknown_author"
            for change in result.changes
        ),
        "unresolved_count": sum(
            change.attribution.status == "unresolved" for change in result.changes
        ),
    }


def _gate(actual: dict[str, int], expected: dict[str, int]) -> None:
    mismatches = [
        name for name, value in expected.items() if actual.get(name) != value
    ]
    if mismatches:
        raise DiagnosticGateError("result_gate_mismatch:" + ",".join(mismatches))


def run_diagnostic(
    *,
    config: dict[str, Any],
    endpoint_id: str,
    start_at: datetime,
    end_at: datetime,
    mode: str,
    expected: dict[str, int],
    expected_copy_boundary: int,
    cache_dir: str | None = None,
) -> dict[str, Any]:
    diagnostic_config = deepcopy(config)
    if cache_dir is not None:
        diagnostic_config.setdefault("svn", {})["cache_dir"] = cache_dir
    provider = provider_from_config(diagnostic_config)
    if not isinstance(provider, CLISVNProvider):
        raise DiagnosticGateError("cli_provider_required")
    performance = MonitorPerformanceRecorder(enabled=True)
    cache_before = provider.client.cache_metrics()
    history = DiagnosticHistoryService(provider, performance)
    record = _record(diagnostic_config, endpoint_id)
    endpoint = EndpointSpec(
        url=str(record["url"]),
        revision="HEAD",
        label=str(record.get("label", endpoint_id)),
    )

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    with performance.phase("diagnostic.total"):
        identity = history.resolve_branch_identity(endpoint)
        boundary = history.resolve_copy_boundary(identity)
        if boundary.revision != expected_copy_boundary:
            raise DiagnosticGateError("copy_boundary_mismatch")
        history.verify_branch_identity(endpoint, identity)
        start_revision = history.resolve_revision_at(identity, start_at)
        end_revision = history.resolve_revision_at(identity, end_at)
        if start_revision != expected["start_revision"]:
            raise DiagnosticGateError("start_revision_mismatch")
        if end_revision != expected["end_revision"]:
            raise DiagnosticGateError("end_revision_mismatch")
        dataset = dict(diagnostic_config["dataset_layout"])
        workbook_source = dict(dataset["workbook_source"])
        csv_export = dict(dataset["csv_export"])
        table_directory = _table_directory(
            history,
            identity,
            end_revision,
            str(workbook_source["directory_name"]),
        )
        snapshot_reader = SvnMonitorSnapshotReader(
            history,
            identity,
            DatasetLayout.from_config(dataset),
            table_directory=table_directory,
            csv_directory_name=str(csv_export["directory_name"]),
        )
        diff_service = MonitorDiffService(snapshot_reader)
        commits = history.list_branch_commits(identity, start_at, end_at)
        if mode == "shadow":
            shadow = compare_legacy_and_incremental(
                diff_service,
                start_revision=start_revision,
                end_revision=end_revision,
                commits=commits,
                performance=performance,
            )
            if not shadow.matches:
                raise DiagnosticGateError("shadow_fingerprint_mismatch")
            result = shadow.incremental.result
            fingerprint = shadow.incremental.semantic_fingerprint
            legacy_fingerprint = shadow.legacy_fingerprint
            plans = shadow.incremental.plans
        elif mode == "incremental":
            incremental = MonitorIncrementalReplayService(
                diff_service, performance=performance
            ).replay(
                start_revision=start_revision,
                end_revision=end_revision,
                commits=commits,
            )
            result = incremental.result
            fingerprint = incremental.semantic_fingerprint
            legacy_fingerprint = None
            plans = incremental.plans
        else:
            net = diff_service.compare_revisions(start_revision, end_revision)
            result = MonitorAttributionService(diff_service).attribute(
                net,
                start_revision=start_revision,
                commits=commits,
            )
            fingerprint = monitor_semantic_fingerprint(
                start_revision=start_revision,
                end_revision=end_revision,
                workbook_count=result.workbook_count,
                reliable_workbook_count=result.reliable_workbook_count,
                changes=result.changes,
                errors=result.errors,
                field_catalog=result.field_catalog,
            )
            legacy_fingerprint = fingerprint
            plans = ()

        actual = {
            "start_revision": start_revision,
            "end_revision": end_revision,
            **_summary(result),
        }
        _gate(actual, expected)

    cache_after = provider.client.cache_metrics()
    cache_delta = {
        name: cache_after.get(name, 0) - cache_before.get(name, 0)
        for name in ("memory_hits", "disk_hits", "misses", "writes")
    }
    fallback_reasons = Counter(
        plan.fallback_reason for plan in plans if plan.fallback_reason is not None
    )
    return {
        "schema_version": "m3.monitor-performance-diagnostic.v1",
        "mode": mode,
        "endpoint_id": endpoint_id,
        "result": actual,
        "commit_count": len(commits),
        "changed_path_count": sum(len(commit.changed_paths) for commit in commits),
        "candidate_workbook_count": sum(len(plan.affected_workbooks) for plan in plans),
        "candidate_sheet_count": sum(len(plan.affected_sheets) for plan in plans),
        "fallback_count": sum(plan.requires_fallback for plan in plans),
        "fallback_reasons": dict(sorted(fallback_reasons.items())),
        "semantic_fingerprint": fingerprint,
        "legacy_fingerprint": legacy_fingerprint,
        "cache_delta": cache_delta,
        "wall_seconds": round(time.perf_counter() - wall_started, 6),
        "cpu_seconds": round(time.process_time() - cpu_started, 6),
        "peak_working_set_bytes": _peak_working_set_bytes(),
        "performance": performance.snapshot(),
        "writes": {
            "monitor_store": False,
            "reports": False,
            "latest": False,
            "windows_scheduler": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--start-at", required=True)
    parser.add_argument("--end-at", required=True)
    parser.add_argument("--mode", choices=("legacy", "shadow", "incremental"), default="shadow")
    parser.add_argument("--cache-dir")
    parser.add_argument("--expected-start-revision", type=int, required=True)
    parser.add_argument("--expected-end-revision", type=int, required=True)
    parser.add_argument("--expected-copy-boundary", type=int, required=True)
    parser.add_argument("--expected-workbooks", type=int, required=True)
    parser.add_argument("--expected-reliable-workbooks", type=int, required=True)
    parser.add_argument("--expected-changes", type=int, required=True)
    parser.add_argument("--expected-errors", type=int, required=True)
    parser.add_argument("--expected-unknown", type=int, required=True)
    parser.add_argument("--expected-unresolved", type=int, required=True)
    args = parser.parse_args(argv)
    expected = {
        "start_revision": args.expected_start_revision,
        "end_revision": args.expected_end_revision,
        "workbook_count": args.expected_workbooks,
        "reliable_workbook_count": args.expected_reliable_workbooks,
        "change_count": args.expected_changes,
        "error_count": args.expected_errors,
        "unknown_author_count": args.expected_unknown,
        "unresolved_count": args.expected_unresolved,
    }
    try:
        output = run_diagnostic(
            config=ConfigStore(Path(args.config)).read(),
            endpoint_id=args.endpoint_id,
            start_at=parse_svn_datetime(args.start_at),
            end_at=parse_svn_datetime(args.end_at),
            mode=args.mode,
            expected=expected,
            expected_copy_boundary=args.expected_copy_boundary,
            cache_dir=args.cache_dir,
        )
    except (DiagnosticGateError, KeyError, OSError, SVNProviderError, ValueError) as error:
        code = str(error) if isinstance(error, DiagnosticGateError) else "diagnostic_failed"
        print(json.dumps({"status": "failed", "code": code}, separators=(",", ":")))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
