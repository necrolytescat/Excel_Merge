from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from app.schemas.monitor import (
    MonitorReportPayload,
    MonitorTaskPayload,
    serialize_monitor_json,
)
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
from app.services.monitor_report_service import (
    _MANAGED_HISTORY,
    REPORT_RETENTION,
    MonitorReportPublishError,
    MonitorReportReferenceError,
    ReportDraft,
    build_monitor_report,
    parse_report_reference,
    render_legacy_compatible_report_html,
    render_monitor_report_html,
    report_reference,
)


UTC = timezone.utc
EXAMPLE = (
    Path(__file__).parents[2]
    / "docs"
    / "contracts"
    / "m3.monitor-report.v1.example.json"
)
TASK_EXAMPLE = EXAMPLE.with_name("m3.monitor-task.v1.example.json")


def load_data() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def report_from(data: dict | None = None) -> MonitorReportPayload:
    return MonitorReportPayload.model_validate(data or load_data())


def draft_from(report: MonitorReportPayload) -> ReportDraft:
    canonical = serialize_monitor_json(report)
    offline_html = render_monitor_report_html(report)
    return ReportDraft(
        payload=report,
        canonical_json=canonical,
        offline_html=offline_html,
        report_ref=report_reference(report.report_id),
        json_sha256=hashlib.sha256(canonical).hexdigest(),
        html_sha256=hashlib.sha256(offline_html).hexdigest(),
        report_expires_at=report.generated_at + REPORT_RETENTION,
    )


def empty_report() -> MonitorReportPayload:
    data = load_data()
    data["status"] = "succeeded"
    data["changes"] = []
    data["errors"] = []
    data["summary"].update(
        {
            "changed_workbook_count": 0,
            "sheet_count": 0,
            "changed_row_count": 0,
            "changed_field_count": 0,
            "author_count": 0,
            "change_count": 0,
            "error_count": 0,
        }
    )
    data["summary"]["by_change_type"] = {
        key: 0 for key in data["summary"]["by_change_type"]
    }
    data["coverage"].update(
        {
            "unknown_author_count": 0,
            "unattributed_change_count": 0,
            "failed_workbook_count": 0,
        }
    )
    return report_from(data)


def later_report(base: MonitorReportPayload) -> MonitorReportPayload:
    data = base.model_dump(mode="json")
    run_id = uuid4()
    data["report_id"] = str(uuid5(run_id, "m3.monitor-report.v1"))
    data["run_id"] = str(run_id)
    data["interval"]["start_at"] = "2026-08-10T10:00:00Z"
    data["interval"]["end_at"] = "2026-08-11T10:00:00Z"
    data["interval"]["logical_cutoff_at"] = "2026-08-11T10:00:00Z"
    data["generated_at"] = "2026-08-11T10:02:00Z"
    return report_from(data)


def report_at(base: MonitorReportPayload, cutoff: datetime) -> MonitorReportPayload:
    data = base.model_dump(mode="json")
    run_id = uuid4()
    data["report_id"] = str(uuid5(run_id, "m3.monitor-report.v1"))
    data["run_id"] = str(run_id)
    data["interval"]["start_at"] = (cutoff - timedelta(hours=1)).isoformat()
    data["interval"]["end_at"] = cutoff.isoformat()
    data["interval"]["logical_cutoff_at"] = cutoff.isoformat()
    data["generated_at"] = (cutoff + timedelta(minutes=1)).isoformat()
    return report_from(data)


def test_canonical_json_is_utf8_unicode_two_space_and_newline():
    raw = serialize_monitor_json(report_from())
    assert raw.endswith(b"\n")
    assert b"\n  \"schema_version\"" in raw
    assert "每日战斗配置回归".encode() in raw
    assert b"\\u6bcf" not in raw
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(raw).hexdigest()


def test_report_builder_is_byte_stable_for_input_and_row_map_order():
    source = report_from()
    task = MonitorTaskPayload.model_validate_json(
        TASK_EXAMPLE.read_bytes()
    )
    reversed_changes = list(reversed(source.changes))
    row_change = next(
        change for change in reversed_changes if change.target and change.target.row_values
    )
    reversed_row = dict(reversed(list(row_change.target.row_values.items())))
    reversed_changes[reversed_changes.index(row_change)] = row_change.model_copy(
        update={
            "target": row_change.target.model_copy(
                update={"row_values": reversed_row}
            )
        }
    )
    common = {
        "run_id": str(source.run_id),
        "task": task,
        "interval": source.interval,
        "start_revision": source.revisions.start_revision,
        "end_revision": source.revisions.end_revision,
        "workbook_count": source.summary.workbook_count,
        "generated_at": source.generated_at,
    }
    first = build_monitor_report(
        **common,
        changes=tuple(source.changes),
        errors=tuple(source.errors),
    )
    second = build_monitor_report(
        **common,
        changes=tuple(reversed_changes),
        errors=tuple(reversed(source.errors)),
    )

    assert serialize_monitor_json(first) == serialize_monitor_json(second)


def test_html_blocks_script_and_markup_injection_and_handles_large_values():
    data = load_data()
    attack = "</script><img src=x onerror=alert(1)>"
    data["task_name"] = attack
    for change in data["changes"]:
        change["workbook"] = "<b>Combat</b>.xlsm"
    data["changes"][0]["target"]["display_value"] = attack + ("值" * 4000)
    data["changes"][0]["target"]["normalized_value"] = attack + ("值" * 4000)
    data["changes"][0]["attribution"]["commit_message"] = attack
    report = report_from(data)
    raw = render_monitor_report_html(report)
    text = raw.decode("utf-8")

    assert attack not in text
    assert "\\u003c/script\\u003e" in text
    assert "innerHTML" not in text
    assert "textContent" in text
    assert "Content-Security-Policy" in text
    assert "aria-live=\"polite\"" in text
    assert ":focus-visible" in text
    assert "prefers-reduced-motion" in text
    assert "@media(max-width:560px)" in text
    embedded = raw.split(
        b'<script type="application/json" id="report-data">', 1
    )[1].split(b"</script>", 1)[0]
    assert MonitorReportPayload.model_validate_json(embedded) == report


def test_html_has_workbook_sheet_grid_filters_and_attribution_drawer():
    html = render_monitor_report_html(report_from()).decode("utf-8")
    for control in ("query", "author"):
        assert f'id="{control}"' in html
    for region in (
        "workbook-list",
        "sheet-tabs",
        "table-wrap",
        "attribution-drawer",
        "attr-author",
        "attr-revision",
        "attr-time",
    ):
        assert f'id="{region}"' in html
    assert "本报告为部分成功" in html
    assert 'row.state==="row-added"?"新增行":"删除行"' in html
    assert 'type==="field_removed"?"column-removed"' in html
    assert "row-deleted td,td.column-removed" in html
    assert 'change.field_name&&change.change_type!=="field_added"' in html
    assert 'field_added:"新增字段"' not in html
    assert 'change.row_key==null' in html
    assert 'report.summary.workbook_count-bookList.length' in html
    assert "没有符合当前筛选条件的变化" in html
    assert 'id="table-wrap" class="table-wrap" tabindex="0"' in html
    assert "height:clamp(15rem,calc(100vh - 18rem),31rem)" in html
    assert "th{position:sticky;top:0;z-index:3" in html
    assert 'data-report-template="m3-workbench-v2.1"' in html
    assert '.join("\n")' not in html

    empty = render_monitor_report_html(empty_report()).decode("utf-8")
    assert '"changes": []' in empty
    assert '"status": "succeeded"' in empty


def test_legacy_blank_report_is_repaired_without_changing_valid_report():
    current = render_monitor_report_html(report_from())
    legacy = current.replace(
        b'data-report-template="m3-workbench-v2.1"',
        b'data-report-template="legacy"',
    ).replace(
        b"</body>",
        b'<script>["legacy"].join("\n")</script></body>',
    )

    assert legacy != current
    assert render_legacy_compatible_report_html(legacy) == current
    assert render_legacy_compatible_report_html(current) is current

    old_valid = current.replace(
        b'data-report-template="m3-workbench-v2.1"',
        b'data-report-template="m3-workbench-v2"',
    )
    assert render_legacy_compatible_report_html(old_valid) == current


def test_html_placeholder_text_in_task_name_does_not_replace_the_title():
    data = load_data()
    data["task_name"] = "QA __REPORT_DATA__ 日报"
    html = render_monitor_report_html(report_from(data)).decode("utf-8")
    title = html.split("<title>", 1)[1].split("</title>", 1)[0]

    assert title == "QA __REPORT_DATA__ 日报 - 版本监控报告"
    assert '"schema_version"' not in title


def test_reference_is_opaque_canonical_and_rejects_tampering():
    report_id = uuid4()
    reference = report_reference(report_id)
    assert parse_report_reference(reference) == report_id
    assert str(report_id) not in reference
    with pytest.raises(MonitorReportReferenceError):
        parse_report_reference(reference[:-1] + "!")


def test_history_is_immutable_and_latest_is_activated_last(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    draft = draft_from(report_from())
    publication = publisher.publish_history(draft)
    task_dir = tmp_path / "reports" / str(draft.payload.task_id)
    history = task_dir / "history"
    json_path = history / "20260810-180000.json"
    html_path = history / "20260810-180000.html"
    assert json_path.read_bytes() == draft.canonical_json
    assert html_path.read_bytes() == draft.offline_html
    assert not (task_dir / "latest.html").exists()
    initial_mtime = json_path.stat().st_mtime_ns

    publisher.publish_history(draft)
    assert json_path.stat().st_mtime_ns == initial_mtime
    publisher.activate_latest(draft)
    assert (task_dir / "latest.html").read_bytes() == draft.offline_html
    assert publication.report_sha256 == draft.json_sha256
    assert publication.html_sha256 == draft.html_sha256


def test_history_names_keep_seconds_and_distinguish_microsecond_cutoffs(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    base = empty_report()
    first = draft_from(
        report_at(base, datetime(2026, 8, 10, 10, 0, 0, 100000, tzinfo=UTC))
    )
    second = draft_from(
        report_at(base, datetime(2026, 8, 10, 10, 0, 0, 500000, tzinfo=UTC))
    )

    publisher.publish_history(first)
    publisher.publish_history(second)

    history = tmp_path / "reports" / str(base.task_id) / "history"
    assert (history / "20260810-180000-100000.json").exists()
    assert (history / "20260810-180000-500000.json").exists()
    assert len(list(history.glob("*.json"))) == 2


@pytest.mark.parametrize(
    "filename",
    (
        "20260810-180000.json",
        "20260810-180000-000001.html",
        "20260810-180000-999999.json",
    ),
)
def test_managed_history_name_accepts_only_second_or_six_digit_microsecond(filename):
    assert _MANAGED_HISTORY.fullmatch(filename)


@pytest.mark.parametrize(
    "filename",
    (
        "20260810-180000-1.json",
        "20260810-180000-0000000.html",
        "20260810-180000-.json",
        "20260810-180000.tmp",
    ),
)
def test_managed_history_name_rejects_malformed_precision(filename):
    assert _MANAGED_HISTORY.fullmatch(filename) is None


def test_whole_second_compat_artifacts_with_zero_microseconds_can_be_resolved(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    draft = draft_from(empty_report())
    publisher.publish_history(draft)
    history = tmp_path / "reports" / str(draft.payload.task_id) / "history"
    (history / "20260810-180000.json").rename(
        history / "20260810-180000-000000.json"
    )
    (history / "20260810-180000.html").rename(
        history / "20260810-180000-000000.html"
    )

    resolved = publisher.resolve(
        task_id=str(draft.payload.task_id),
        run_id=str(draft.payload.run_id),
        logical_cutoff_at=draft.payload.interval.logical_cutoff_at,
        reference=draft.report_ref,
        expected_json_sha256=draft.json_sha256,
        expected_html_sha256=draft.html_sha256,
    )

    assert resolved.payload == draft.payload


def test_lease_loss_between_fsync_and_replace_publishes_nothing(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    draft = draft_from(report_from())

    def lost():
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        publisher.publish_history(draft, ensure_owned=lost)
    task_dir = tmp_path / "reports" / str(draft.payload.task_id)
    assert not list(task_dir.rglob("*.json"))
    assert not list(task_dir.rglob("*.html"))
    assert not list(task_dir.rglob(".m3tmp-*.tmp"))


def test_same_cutoff_history_conflict_is_deterministic(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    first = draft_from(empty_report())
    second = draft_from(report_at(first.payload, first.payload.interval.end_at))
    publisher.publish_history(first)

    with pytest.raises(MonitorReportPublishError, match="conflicts") as captured:
        publisher.publish_history(second)

    assert captured.value.retryable is False


def test_latest_failure_keeps_old_latest_and_history_is_recoverable(
    tmp_path, monkeypatch
):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    old = draft_from(report_from())
    publisher.publish_history(old)
    publisher.activate_latest(old)
    latest = (
        tmp_path / "reports" / str(old.payload.task_id) / "latest.html"
    )
    previous = latest.read_bytes()
    new = draft_from(later_report(old.payload))
    publisher.publish_history(new)
    real_write = publisher._atomic_write

    def fail_latest(path, content, **kwargs):
        if path.name == "latest.html":
            raise OSError("locked")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(publisher, "_atomic_write", fail_latest)
    with pytest.raises(MonitorReportPublishError):
        publisher.activate_latest(new)
    assert latest.read_bytes() == previous
    assert not list(latest.parent.rglob(".m3tmp-*.tmp"))


def test_directory_access_error_is_a_retryable_publish_error(
    tmp_path, monkeypatch
):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    draft = draft_from(report_from())
    monkeypatch.setattr(
        publisher,
        "_validate_directories",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("temporarily locked")
        ),
    )

    with pytest.raises(MonitorReportPublishError) as captured:
        publisher.publish_history(draft)
    assert captured.value.retryable is True


def test_older_retry_never_rolls_latest_back(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    old = draft_from(report_from())
    new = draft_from(later_report(old.payload))
    publisher.publish_history(old)
    publisher.publish_history(new)
    publisher.activate_latest(new)
    latest = (
        tmp_path / "reports" / str(old.payload.task_id) / "latest.html"
    )
    expected = latest.read_bytes()

    publisher.activate_latest(old)

    assert latest.read_bytes() == expected


def test_resolve_checks_json_html_sha_and_task_run_ownership(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    draft = draft_from(report_from())
    publisher.publish_history(draft)
    report = draft.payload
    resolved = publisher.resolve(
        task_id=str(report.task_id),
        run_id=str(report.run_id),
        logical_cutoff_at=report.interval.logical_cutoff_at,
        reference=draft.report_ref,
        expected_json_sha256=draft.json_sha256,
        expected_html_sha256=draft.html_sha256,
    )
    assert resolved.payload == report
    assert resolved.html_sha256 == draft.html_sha256

    with pytest.raises(MonitorReportReferenceError, match="checksum"):
        publisher.resolve(
            task_id=str(report.task_id),
            run_id=str(report.run_id),
            logical_cutoff_at=report.interval.logical_cutoff_at,
            reference=draft.report_ref,
            expected_json_sha256="0" * 64,
        )
    with pytest.raises(MonitorReportReferenceError):
        publisher.resolve(
            task_id=str(report.task_id),
            run_id=str(uuid4()),
            logical_cutoff_at=report.interval.logical_cutoff_at,
            reference=draft.report_ref,
            expected_json_sha256=draft.json_sha256,
        )


def test_cleanup_is_30_day_exact_non_recursive_and_keeps_latest(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    old = draft_from(report_from())
    publisher.publish_history(old)
    publisher.activate_latest(old)
    task_dir = tmp_path / "reports" / str(old.payload.task_id)
    history = task_dir / "history"
    unknown = history / "notes.txt"
    unknown.write_text("keep", encoding="utf-8")
    nested = history / "nested"
    nested.mkdir()
    (nested / "20200101-000000.html").write_text("keep", encoding="utf-8")

    fresh = draft_from(later_report(old.payload))
    publisher.publish_history(fresh)
    now = old.payload.generated_at + timedelta(days=30)
    removed = publisher.cleanup_expired(str(old.payload.task_id), now=now)

    assert removed == ("20260810-180000",)
    assert not (history / "20260810-180000.json").exists()
    assert not (history / "20260810-180000.html").exists()
    assert (history / "20260811-180000.json").exists()
    assert unknown.exists()
    assert (nested / "20200101-000000.html").exists()
    assert (task_dir / "latest.html").exists()


def test_cleanup_recovers_after_one_file_was_deleted_before_access_error(
    tmp_path, monkeypatch
):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    draft = draft_from(report_from())
    publisher.publish_history(draft)
    history = (
        tmp_path
        / "reports"
        / str(draft.payload.task_id)
        / "history"
    )
    json_path = history / "20260810-180000.json"
    html_path = history / "20260810-180000.html"
    real_unlink = Path.unlink
    failed = False

    def fail_json_once(path, *args, **kwargs):
        nonlocal failed
        if path == json_path and not failed:
            failed = True
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_json_once)
    with pytest.raises(PermissionError):
        publisher.cleanup_expired(
            str(draft.payload.task_id),
            now=draft.report_expires_at,
        )
    assert not html_path.exists()
    assert json_path.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert publisher.cleanup_expired(
        str(draft.payload.task_id),
        now=draft.report_expires_at,
    ) == ("20260810-180000",)
    assert not json_path.exists()


def test_cleanup_skips_symlink_when_supported(tmp_path):
    publisher = FileSystemMonitorReportPublisher(tmp_path / "reports")
    report = report_from()
    task_dir = tmp_path / "reports" / str(report.task_id)
    history = task_dir / "history"
    history.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_bytes(serialize_monitor_json(report))
    link = history / "20260810-180000.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows account")
    (history / "20260810-180000.html").write_bytes(
        render_monitor_report_html(report)
    )
    assert publisher.cleanup_expired(
        str(report.task_id), now=report.generated_at + timedelta(days=31)
    ) == ()
    assert target.exists()
    assert link.is_symlink()


def test_publication_rejects_symlinked_task_directory_when_supported(tmp_path):
    report = report_from()
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    task_link = reports / str(report.task_id)
    try:
        task_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this Windows account")
    publisher = FileSystemMonitorReportPublisher(reports)

    with pytest.raises(MonitorReportPublishError, match="ownership") as captured:
        publisher.publish_history(draft_from(report))
    assert captured.value.retryable is False
    assert not list(outside.rglob("*"))
