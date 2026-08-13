"""Opt-in, redacted performance metrics for version comparison."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
import threading
import time
from typing import Any, Iterator


_METRIC_NAME = re.compile(r"^[a-z][a-z0-9_.]*$")


@dataclass
class _PhaseMetric:
    calls: int = 0
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    max_wall_seconds: float = 0.0


class DiffPerformanceRecorder:
    """Collect aggregate timings without retaining paths or payload data."""

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._phases: dict[str, _PhaseMetric] = {}
        self._counters: dict[str, int | float] = {}
        self._values: dict[str, int | float | str | bool] = {}

    @staticmethod
    def _name(value: str) -> str:
        if not _METRIC_NAME.fullmatch(value):
            raise ValueError("performance metric name is invalid")
        return value

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        key = self._name(name)
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        try:
            yield
        finally:
            wall = time.perf_counter() - wall_started
            cpu = time.process_time() - cpu_started
            with self._lock:
                metric = self._phases.setdefault(key, _PhaseMetric())
                metric.calls += 1
                metric.wall_seconds += wall
                metric.cpu_seconds += cpu
                metric.max_wall_seconds = max(metric.max_wall_seconds, wall)

    def increment(self, name: str, value: int | float = 1) -> None:
        if not self.enabled:
            return
        key = self._name(name)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def set_value(self, name: str, value: int | float | str | bool) -> None:
        if not self.enabled:
            return
        key = self._name(name)
        with self._lock:
            self._values[key] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            phases = {
                name: {
                    "calls": metric.calls,
                    "wall_seconds": round(metric.wall_seconds, 6),
                    "cpu_seconds": round(metric.cpu_seconds, 6),
                    "max_wall_seconds": round(metric.max_wall_seconds, 6),
                }
                for name, metric in sorted(self._phases.items())
            }
            counters = dict(sorted(self._counters.items()))
            values = dict(sorted(self._values.items()))
        return {
            "schema_version": "m2.diff-performance.v1",
            "enabled": self.enabled,
            "phases": phases,
            "counters": counters,
            "values": values,
        }
