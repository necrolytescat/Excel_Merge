"""Phase 2 report publication protocol and canonical JSON reference publisher."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Protocol
from uuid import UUID, uuid4

from app.schemas.monitor import (
    MonitorChangePayload,
    MonitorChangeType,
    MonitorChangeTypeCountsPayload,
    MonitorCoveragePayload,
    MonitorPublicErrorPayload,
    MonitorReportPayload,
    MonitorReportSummaryPayload,
    MonitorRevisionRangePayload,
    MonitorRunSummaryPayload,
    MonitorTaskPayload,
    MonitorTimeIntervalPayload,
    serialize_monitor_json,
)


@dataclass(frozen=True)
class ReportPublication:
    status: str
    start_revision: int
    end_revision: int
    run_summary: MonitorRunSummaryPayload
    report_ref: str
    report_sha256: str
    report_expires_at: datetime
    errors: tuple[MonitorPublicErrorPayload, ...]


class MonitorReportPublisher(Protocol):
    def publish(
        self,
        *,
        run_id: str,
        task: MonitorTaskPayload,
        interval: MonitorTimeIntervalPayload,
        start_revision: int,
        end_revision: int,
        workbook_count: int,
        changes: tuple[MonitorChangePayload, ...],
        errors: tuple[MonitorPublicErrorPayload, ...],
        generated_at: datetime,
    ) -> ReportPublication: ...


class CanonicalJsonReferencePublisher:
    """Validate canonical report JSON without creating Phase 3 filesystem artifacts."""

    def __init__(self):
        self.results: dict[str, bytes] = {}

    @staticmethod
    def _reference() -> str:
        token = base64.urlsafe_b64encode(uuid4().bytes).decode("ascii").rstrip("=")
        return "m3r_" + token

    def publish(
        self,
        *,
        run_id: str,
        task: MonitorTaskPayload,
        interval: MonitorTimeIntervalPayload,
        start_revision: int,
        end_revision: int,
        workbook_count: int,
        changes: tuple[MonitorChangePayload, ...],
        errors: tuple[MonitorPublicErrorPayload, ...],
        generated_at: datetime,
    ) -> ReportPublication:
        counts = {kind.value: 0 for kind in MonitorChangeType}
        for change in changes:
            counts[change.change_type.value] += 1
        changed_workbooks = {change.workbook for change in changes}
        changed_sheets = {(change.workbook, change.sheet_name) for change in changes}
        changed_rows = {
            (change.workbook, change.sheet_name, change.row_key)
            for change in changes
            if change.row_key is not None
        }
        changed_fields = {
            (change.workbook, change.sheet_name, change.row_key, change.field_name)
            for change in changes
            if change.field_name is not None
        }
        known_authors = {
            change.attribution.author
            for change in changes
            if change.attribution.status == "attributed"
        }
        unknown_authors = sum(
            change.attribution.status == "unknown_author" for change in changes
        )
        unresolved = sum(change.attribution.status == "unresolved" for change in changes)
        failed_workbooks = {error.workbook for error in errors if error.workbook is not None}
        status = "partial" if errors or unresolved else "succeeded"
        summary = MonitorReportSummaryPayload(
            workbook_count=workbook_count,
            changed_workbook_count=len(changed_workbooks),
            sheet_count=len(changed_sheets),
            changed_row_count=len(changed_rows),
            changed_field_count=len(changed_fields),
            author_count=len(known_authors),
            change_count=len(changes),
            error_count=len(errors),
            by_change_type=MonitorChangeTypeCountsPayload(**counts),
        )
        report = MonitorReportPayload(
            report_id=uuid4(),
            run_id=UUID(run_id),
            task_id=task.task_id,
            task_name=task.name,
            status=status,
            branch=task.branch,
            interval=interval,
            revisions=MonitorRevisionRangePayload(
                start_revision=start_revision,
                end_revision=end_revision,
            ),
            generated_at=max(generated_at.astimezone(timezone.utc), interval.end_at),
            summary=summary,
            coverage=MonitorCoveragePayload(
                excluded_content=[
                    "scope_none_fields", "unexported_fields", "excel_notes",
                    "formulas", "styles", "macros",
                ],
                unknown_author_count=unknown_authors,
                unattributed_change_count=unresolved,
                failed_workbook_count=len(failed_workbooks),
            ),
            changes=list(changes),
            errors=list(errors),
        )
        raw = serialize_monitor_json(report)
        reference = self._reference()
        self.results[reference] = raw
        run_summary = MonitorRunSummaryPayload(
            workbook_count=workbook_count,
            changed_workbook_count=len(changed_workbooks),
            change_count=len(changes),
            error_count=len(errors),
        )
        return ReportPublication(
            status=status,
            start_revision=start_revision,
            end_revision=end_revision,
            run_summary=run_summary,
            report_ref=reference,
            report_sha256=hashlib.sha256(raw).hexdigest(),
            report_expires_at=report.generated_at + timedelta(days=30),
            errors=errors,
        )
