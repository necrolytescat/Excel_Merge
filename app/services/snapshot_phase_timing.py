"""Request-scoped, redacted timing for frozen snapshot construction."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Any, Iterator
from uuid import uuid4


_SIDES = ("source", "target")


@dataclass
class _PhaseAggregate:
    calls: int = 0
    failures: int = 0
    wall_ns: int = 0
    cpu_ns: int = 0
    bytes: int = 0
    items: int = 0
    samples_ns: list[int] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        *,
        wall_ns: int,
        cpu_ns: int,
        bytes_count: int,
        items: int,
        failed: bool,
        source: str | None,
    ) -> None:
        self.calls += 1
        self.failures += int(failed)
        self.wall_ns += wall_ns
        self.cpu_ns += cpu_ns
        self.bytes += max(0, int(bytes_count))
        self.items += max(0, int(items))
        self.samples_ns.append(wall_ns)
        if source:
            self.sources[source] = self.sources.get(source, 0) + 1


class _PhaseObservation:
    def __init__(self) -> None:
        self.bytes = 0
        self.items = 0
        self.source: str | None = None
        self.failed = False

    def result(
        self,
        *,
        bytes_count: int = 0,
        items: int = 0,
        source: str | None = None,
    ) -> None:
        self.bytes = max(0, int(bytes_count))
        self.items = max(0, int(items))
        self.source = source


def _seconds(value: int) -> float:
    return round(value / 1_000_000_000, 6)


def _percentile(samples: list[int], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return _seconds(ordered[index])


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[list[int]] = []
    for started, finished in sorted(intervals):
        if not merged or started > merged[-1][1]:
            merged.append([started, finished])
        else:
            merged[-1][1] = max(merged[-1][1], finished)
    return [(started, finished) for started, finished in merged]


def _interval_total(intervals: list[tuple[int, int]]) -> int:
    return sum(finished - started for started, finished in _merge_intervals(intervals))


def _interval_overlap(
    left: list[tuple[int, int]],
    right: list[tuple[int, int]],
) -> int:
    left_merged = _merge_intervals(left)
    right_merged = _merge_intervals(right)
    left_index = 0
    right_index = 0
    overlap = 0
    while left_index < len(left_merged) and right_index < len(right_merged):
        left_started, left_finished = left_merged[left_index]
        right_started, right_finished = right_merged[right_index]
        overlap += max(
            0,
            min(left_finished, right_finished) - max(left_started, right_started),
        )
        if left_finished <= right_finished:
            left_index += 1
        else:
            right_index += 1
    return overlap


class SnapshotPhaseTiming:
    """Collect one snapshot request without relying on process-global deltas."""

    schema_version = "m2.snapshot-phase-timing.v1"

    def __init__(
        self,
        *,
        request_context_id: str | None,
        source_endpoint_id: str,
        source_revision: int | str,
        target_endpoint_id: str,
        target_revision: int | str,
    ) -> None:
        self.request_context_id = request_context_id or str(uuid4())
        self.build_context_id: str | None = None
        self.reuse_mode = "unresolved"
        self._lock = threading.Lock()
        self._started_ns = time.perf_counter_ns()
        self._request_thread_id = threading.get_ident()
        self._request_cpu_started_ns = time.thread_time_ns()
        self._worker_cpu_ns = 0
        self._cpu_scope_depth: dict[int, int] = {}
        self._side_cpu_ns = {side: 0 for side in _SIDES}
        self._phases: dict[tuple[str, str | None], _PhaseAggregate] = {}
        self._events: list[tuple[int, int]] = []
        self._side_intervals: dict[str, list[tuple[int, int]]] = {
            side: [] for side in _SIDES
        }
        self._counters: dict[tuple[str, str | None], int] = {}
        self._provider_active = 0
        self._provider_peak = 0
        self._provider_active_by_side = {side: 0 for side in _SIDES}
        self._provider_peak_by_side = {side: 0 for side in _SIDES}
        self._endpoints: dict[str, dict[str, Any]] = {
            "source": {
                "endpoint_id": source_endpoint_id,
                "requested_revision": source_revision,
                "resolved_revision": None,
                "repository_uuid": None,
            },
            "target": {
                "endpoint_id": target_endpoint_id,
                "requested_revision": target_revision,
                "resolved_revision": None,
                "repository_uuid": None,
            },
        }

    @contextmanager
    def phase(
        self,
        name: str,
        *,
        side: str | None = None,
    ) -> Iterator[_PhaseObservation]:
        started_ns = time.perf_counter_ns()
        cpu_started_ns = time.thread_time_ns()
        observation = _PhaseObservation()
        try:
            yield observation
        except BaseException:
            observation.failed = True
            raise
        finally:
            finished_ns = time.perf_counter_ns()
            cpu_finished_ns = time.thread_time_ns()
            self._record_phase(
                name,
                side=side,
                started_ns=started_ns,
                finished_ns=finished_ns,
                cpu_ns=cpu_finished_ns - cpu_started_ns,
                observation=observation,
            )

    def _record_phase(
        self,
        name: str,
        *,
        side: str | None,
        started_ns: int,
        finished_ns: int,
        cpu_ns: int,
        observation: _PhaseObservation,
    ) -> None:
        with self._lock:
            aggregate = self._phases.setdefault(
                (name, side),
                _PhaseAggregate(),
            )
            aggregate.add(
                wall_ns=finished_ns - started_ns,
                cpu_ns=cpu_ns,
                bytes_count=observation.bytes,
                items=observation.items,
                failed=observation.failed,
                source=observation.source,
            )
            self._events.append((started_ns, finished_ns))

    @contextmanager
    def side_scope(self, side: str) -> Iterator[None]:
        started_ns = time.perf_counter_ns()
        cpu_started_ns = time.thread_time_ns()
        thread_id = threading.get_ident()
        with self._lock:
            depth = self._cpu_scope_depth.get(thread_id, 0)
            self._cpu_scope_depth[thread_id] = depth + 1
        outermost = depth == 0
        try:
            yield
        finally:
            finished_ns = time.perf_counter_ns()
            cpu_ns = time.thread_time_ns() - cpu_started_ns
            with self._lock:
                remaining = self._cpu_scope_depth[thread_id] - 1
                if remaining:
                    self._cpu_scope_depth[thread_id] = remaining
                else:
                    self._cpu_scope_depth.pop(thread_id, None)
                if outermost:
                    self._side_intervals[side].append((started_ns, finished_ns))
                    self._side_cpu_ns[side] += cpu_ns
                    self._events.append((started_ns, finished_ns))
                    if thread_id != self._request_thread_id:
                        self._worker_cpu_ns += cpu_ns

    @contextmanager
    def file_worker_scope(self, side: str) -> Iterator[None]:
        started_ns = time.perf_counter_ns()
        cpu_started_ns = time.thread_time_ns()
        thread_id = threading.get_ident()
        with self._lock:
            depth = self._cpu_scope_depth.get(thread_id, 0)
            self._cpu_scope_depth[thread_id] = depth + 1
        outermost = depth == 0
        try:
            yield
        finally:
            finished_ns = time.perf_counter_ns()
            cpu_ns = time.thread_time_ns() - cpu_started_ns
            with self._lock:
                remaining = self._cpu_scope_depth[thread_id] - 1
                if remaining:
                    self._cpu_scope_depth[thread_id] = remaining
                else:
                    self._cpu_scope_depth.pop(thread_id, None)
                if outermost:
                    self._events.append((started_ns, finished_ns))
                    self._side_cpu_ns[side] += cpu_ns
                    if thread_id != self._request_thread_id:
                        self._worker_cpu_ns += cpu_ns

    def provider_enter(self, side: str) -> None:
        with self._lock:
            self._provider_active += 1
            self._provider_peak = max(self._provider_peak, self._provider_active)
            self._provider_active_by_side[side] += 1
            self._provider_peak_by_side[side] = max(
                self._provider_peak_by_side[side],
                self._provider_active_by_side[side],
            )

    def provider_exit(self, side: str) -> None:
        with self._lock:
            self._provider_active -= 1
            self._provider_active_by_side[side] -= 1

    def increment(self, name: str, *, side: str | None = None, amount: int = 1) -> None:
        with self._lock:
            key = (name, side)
            self._counters[key] = self._counters.get(key, 0) + int(amount)

    def set_build_context(
        self,
        build_context_id: str | None,
        reuse_mode: str,
    ) -> None:
        with self._lock:
            self.build_context_id = build_context_id
            self.reuse_mode = reuse_mode

    def set_endpoint(
        self,
        side: str,
        *,
        resolved_revision: int | None = None,
        repository_uuid: str | None = None,
    ) -> None:
        with self._lock:
            if resolved_revision is not None:
                self._endpoints[side]["resolved_revision"] = resolved_revision
            if repository_uuid:
                self._endpoints[side]["repository_uuid"] = repository_uuid

    @staticmethod
    def _aggregate_payload(aggregate: _PhaseAggregate) -> dict[str, Any]:
        return {
            "calls": aggregate.calls,
            "failures": aggregate.failures,
            "wall_seconds": _seconds(aggregate.wall_ns),
            "cpu_seconds": _seconds(aggregate.cpu_ns),
            "p50_seconds": _percentile(aggregate.samples_ns, 0.50),
            "p95_seconds": _percentile(aggregate.samples_ns, 0.95),
            "max_seconds": _percentile(aggregate.samples_ns, 1.0),
            "bytes": aggregate.bytes,
            "items": aggregate.items,
            "sources": dict(sorted(aggregate.sources.items())),
        }

    @staticmethod
    def _combine(aggregates: list[_PhaseAggregate]) -> _PhaseAggregate:
        combined = _PhaseAggregate()
        for aggregate in aggregates:
            combined.calls += aggregate.calls
            combined.failures += aggregate.failures
            combined.wall_ns += aggregate.wall_ns
            combined.cpu_ns += aggregate.cpu_ns
            combined.bytes += aggregate.bytes
            combined.items += aggregate.items
            combined.samples_ns.extend(aggregate.samples_ns)
            for source, count in aggregate.sources.items():
                combined.sources[source] = combined.sources.get(source, 0) + count
        return combined

    def finish(self, *, outcome: str) -> dict[str, Any]:
        finished_ns = time.perf_counter_ns()
        request_thread_cpu_ns = time.thread_time_ns() - self._request_cpu_started_ns
        with self._lock:
            total_wall_ns = finished_ns - self._started_ns
            total_cpu_ns = request_thread_cpu_ns + self._worker_cpu_ns
            phase_names = sorted({name for name, _ in self._phases})
            stages: dict[str, Any] = {}
            for name in phase_names:
                overall = self._combine(
                    [
                        aggregate
                        for (phase_name, _), aggregate in self._phases.items()
                        if phase_name == name
                    ]
                )
                by_side = {
                    side: self._aggregate_payload(self._phases[(name, side)])
                    for side in _SIDES
                    if (name, side) in self._phases
                }
                stages[name] = {
                    **self._aggregate_payload(overall),
                    "by_side": by_side,
                }

            source_intervals = self._side_intervals["source"]
            target_intervals = self._side_intervals["target"]
            source_wall_ns = _interval_total(source_intervals)
            target_wall_ns = _interval_total(target_intervals)
            overlap_ns = _interval_overlap(source_intervals, target_intervals)
            active_union_ns = _interval_total(source_intervals + target_intervals)
            accounted_ns = _interval_total(self._events)
            stage_wall_sum_ns = sum(
                aggregate.wall_ns for aggregate in self._phases.values()
            )

            def prefixed(prefix: str) -> dict[str, Any]:
                return {
                    name: stages[name]
                    for name in stages
                    if name.startswith(prefix)
                }

            def named(name: str) -> dict[str, Any]:
                return stages.get(
                    name,
                    self._aggregate_payload(_PhaseAggregate()) | {"by_side": {}},
                )

            counters = {
                name: sum(
                    value
                    for (counter_name, _), value in self._counters.items()
                    if counter_name == name
                )
                for name in sorted({name for name, _ in self._counters})
            }
            counters_by_side = {
                side: {
                    name: value
                    for (name, counter_side), value in sorted(self._counters.items())
                    if counter_side == side
                }
                for side in _SIDES
            }
            sha_aggregate = self._combine(
                [
                    aggregate
                    for (name, _), aggregate in self._phases.items()
                    if name.startswith("sha256.")
                ]
            )
            result = {
                "schema_version": self.schema_version,
                "request": {
                    "request_context_id": self.request_context_id,
                    "build_context_id": self.build_context_id,
                    "reuse_mode": self.reuse_mode,
                    "outcome": outcome,
                    "wall_seconds": _seconds(total_wall_ns),
                    "cpu_seconds": _seconds(total_cpu_ns),
                    "request_thread_cpu_seconds": _seconds(request_thread_cpu_ns),
                    "worker_cpu_seconds": _seconds(self._worker_cpu_ns),
                },
                "endpoints": {side: dict(values) for side, values in self._endpoints.items()},
                "parallel": {
                    "source_wall_seconds": _seconds(source_wall_ns),
                    "target_wall_seconds": _seconds(target_wall_ns),
                    "source_cpu_seconds": _seconds(self._side_cpu_ns["source"]),
                    "target_cpu_seconds": _seconds(self._side_cpu_ns["target"]),
                    "overlap_seconds": _seconds(overlap_ns),
                    "side_critical_path_seconds": _seconds(active_union_ns),
                    "side_aggregate_seconds": _seconds(source_wall_ns + target_wall_ns),
                },
                "summary": {
                    "endpoint_info": named("endpoint.info"),
                    "list_tree": named("svn.list_tree"),
                    "persistent_lookup": named("persistent.lookup"),
                    "provider_read": {
                        **named("provider.read"),
                        "peak_concurrency": self._provider_peak,
                        "peak_concurrency_by_side": dict(self._provider_peak_by_side),
                    },
                    "sha256": self._aggregate_payload(sha_aggregate),
                    "blob_io": prefixed("blob."),
                    "tree_index_io": prefixed("index."),
                    "pairing": prefixed("pairing."),
                    "sorting": prefixed("sort."),
                    "response_build": prefixed("response."),
                    "critical_path_accounted_seconds": _seconds(accounted_ns),
                    "unattributed_wall_seconds": _seconds(
                        max(0, total_wall_ns - accounted_ns)
                    ),
                    "stage_aggregate_seconds": _seconds(stage_wall_sum_ns),
                    "aggregation_note": (
                        "阶段墙钟包含嵌套和并行工作，不能直接相加；"
                        "关键路径使用时间区间并集，两侧重叠单独列示。"
                    ),
                },
                "stages": stages,
                "counters": counters,
                "counters_by_side": counters_by_side,
            }
        return result
