from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.monitor_runner import P1MonitorRunEngine
from app.services.monitor_attribution_service import MonitorAttributionResult


UTC = timezone.utc


class _History:
    def __init__(self):
        self.commits = [object()]

    def verify_branch_identity(self, endpoint, identity):
        return identity

    def resolve_revision_at(self, identity, instant):
        return 100 if instant.hour == 9 else 101

    def list_branch_commits(self, identity, start, end):
        return self.commits


class _Publisher:
    def __init__(self):
        self.arguments = None

    def render(self, **kwargs):
        self.arguments = kwargs
        return object()


class _TaskService:
    def to_public_task(self, task):
        return "public-task"


class _LegacyAttribution:
    def attribute(self, *args, **kwargs):
        raise AssertionError("formal incremental mode must not call legacy attribution")


def test_formal_runner_mode_uses_incremental_result_for_existing_report_pipeline(
    monkeypatch,
):
    history = _History()
    diff_service = object()
    publisher = _Publisher()
    attributed = MonitorAttributionResult(
        workbook_count=197,
        reliable_workbook_count=197,
        changes=(),
        errors=(),
        field_catalog=(),
    )
    replay_calls = []

    class _Replay:
        def __init__(self, actual_diff_service):
            assert actual_diff_service is diff_service

        def replay(self, **kwargs):
            replay_calls.append(kwargs)
            return SimpleNamespace(result=attributed)

    monkeypatch.setattr(
        "app.monitor_runner.MonitorIncrementalReplayService", _Replay
    )
    engine = P1MonitorRunEngine(
        history=history,
        endpoint=object(),
        identity=object(),
        diff_service=diff_service,
        attribution_service=_LegacyAttribution(),
        publisher=publisher,
        task_service=_TaskService(),
        engine_mode="incremental",
    )
    run = SimpleNamespace(
        run_id="run-id",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        boundary_type=SimpleNamespace(value="scheduled"),
    )

    result = engine.execute(run, object(), datetime(2026, 8, 10, 11, tzinfo=UTC))

    assert replay_calls == [
        {
            "start_revision": 100,
            "end_revision": 101,
            "commits": history.commits,
        }
    ]
    assert publisher.arguments["start_revision"] == 100
    assert publisher.arguments["end_revision"] == 101
    assert publisher.arguments["workbook_count"] == 197
    assert publisher.arguments["changes"] == ()
    assert publisher.arguments["errors"] == ()
    assert publisher.arguments["field_catalog"] == ()
    assert result.draft is not None
    assert result.publisher is publisher
