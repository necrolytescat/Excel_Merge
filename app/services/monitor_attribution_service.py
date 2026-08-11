"""Field-level event ledger and final-state attribution for M3."""
from __future__ import annotations

from dataclasses import dataclass

from app.schemas.monitor import (
    MonitorAttributionPayload,
    MonitorChangePayload,
    MonitorChangeType,
    MonitorErrorCode,
    MonitorErrorStage,
    MonitorPublicErrorPayload,
    MonitorReportSheetFieldsPayload,
)
from app.services.monitor_diff_service import MonitorDiffService, MonitorNetDiff
from core.svn_history import BranchCommit


@dataclass(frozen=True)
class MonitorAttributionResult:
    workbook_count: int
    reliable_workbook_count: int
    changes: tuple[MonitorChangePayload, ...]
    errors: tuple[MonitorPublicErrorPayload, ...]
    field_catalog: tuple[MonitorReportSheetFieldsPayload, ...] = ()


@dataclass(frozen=True)
class _LedgerEvent:
    change: MonitorChangePayload
    commit: BranchCommit


class MonitorAttributionService:
    def __init__(self, diff_service: MonitorDiffService):
        self.diff_service = diff_service

    @staticmethod
    def _same_identity(left: MonitorChangePayload, right: MonitorChangePayload) -> bool:
        return (
            left.workbook == right.workbook
            and left.sheet_name == right.sheet_name
            and left.row_key == right.row_key
            and left.field_name == right.field_name
        )

    @staticmethod
    def _definition_state(
        change: MonitorChangePayload,
        side: str,
    ) -> tuple[str, str] | None:
        payload = getattr(change, side)
        if payload is None or payload.field_definition is None:
            return None
        definition = payload.field_definition
        return (definition.declared_type, definition.scope)

    @classmethod
    def _forms_final_state(
        cls,
        event: MonitorChangePayload,
        final: MonitorChangePayload,
    ) -> bool:
        if not cls._same_identity(event, final):
            return False
        if final.change_type == MonitorChangeType.FIELD_MODIFIED:
            return (
                event.change_type == MonitorChangeType.FIELD_MODIFIED
                and event.target is not None
                and final.target is not None
                and event.target.normalized_value == final.target.normalized_value
            )
        if final.change_type == MonitorChangeType.ROW_ADDED:
            return event.change_type == MonitorChangeType.ROW_ADDED
        if final.change_type == MonitorChangeType.ROW_DELETED:
            return event.change_type == MonitorChangeType.ROW_DELETED
        if final.change_type == MonitorChangeType.FIELD_ADDED:
            return (
                event.change_type == MonitorChangeType.FIELD_ADDED
                and cls._definition_state(event, "target")
                == cls._definition_state(final, "target")
            )
        if final.change_type == MonitorChangeType.FIELD_REMOVED:
            return event.change_type == MonitorChangeType.FIELD_REMOVED
        return (
            event.change_type
            in {
                MonitorChangeType.FIELD_DEFINITION_MODIFIED,
                MonitorChangeType.FIELD_ADDED,
            }
            and cls._definition_state(event, "target")
            == cls._definition_state(final, "target")
        )

    @staticmethod
    def _attribution(commit: BranchCommit) -> MonitorAttributionPayload:
        message = commit.message[:512] or None
        if commit.author:
            return MonitorAttributionPayload(
                status="attributed",
                author=commit.author[:256],
                revision=commit.revision,
                changed_at=commit.changed_at,
                commit_message=message,
            )
        return MonitorAttributionPayload(
            status="unknown_author",
            author="未知",
            revision=commit.revision,
            changed_at=commit.changed_at,
            commit_message=message,
        )

    @staticmethod
    def _incomplete_error(workbook: str | None) -> MonitorPublicErrorPayload:
        return MonitorPublicErrorPayload(
            code=MonitorErrorCode.ATTRIBUTION_INCOMPLETE,
            stage=MonitorErrorStage.ATTRIBUTION,
            message="区间事件无法与最终净变化可靠连接",
            retryable=False,
            workbook=workbook,
        )

    def attribute(
        self,
        net: MonitorNetDiff,
        *,
        start_revision: int,
        commits: list[BranchCommit],
    ) -> MonitorAttributionResult:
        previous = self.diff_service.snapshot_reader.load_snapshot(start_revision)
        ledger: list[_LedgerEvent] = []
        blocked_workbooks: set[str] = set()
        all_workbooks_blocked = any(error.workbook is None for error in previous.errors)
        blocked_workbooks.update(
            error.workbook for error in previous.errors if error.workbook is not None
        )

        for commit in sorted(commits, key=lambda item: item.revision):
            current = self.diff_service.snapshot_reader.load_snapshot(commit.revision)
            event_diff = self.diff_service.compare_snapshots(previous, current)
            ledger.extend(_LedgerEvent(change=change, commit=commit) for change in event_diff.changes)
            if any(error.workbook is None for error in event_diff.errors):
                all_workbooks_blocked = True
            blocked_workbooks.update(
                error.workbook for error in event_diff.errors if error.workbook is not None
            )
            previous = current

        attributed: list[MonitorChangePayload] = []
        unresolved_workbooks: set[str] = set()
        for final in net.changes:
            match = None
            if not all_workbooks_blocked and final.workbook not in blocked_workbooks:
                for event in ledger:
                    if self._forms_final_state(event.change, final):
                        match = event
            if match is None:
                unresolved_workbooks.add(final.workbook)
                attributed.append(final)
            else:
                attributed.append(
                    final.model_copy(
                        update={"attribution": self._attribution(match.commit)}
                    )
                )

        net_error_workbooks = {
            error.workbook for error in net.errors if error.workbook is not None
        }
        has_global_net_error = any(error.workbook is None for error in net.errors)
        if all_workbooks_blocked and not has_global_net_error:
            attribution_errors = [self._incomplete_error(None)]
        else:
            attribution_errors = [
                self._incomplete_error(workbook)
                for workbook in sorted(
                    (blocked_workbooks | unresolved_workbooks) - net_error_workbooks,
                    key=str.casefold,
                )
            ]
        known_error_keys = {
            (error.code, error.stage, error.workbook, error.sheet_name)
            for error in net.errors
        }
        errors = list(net.errors)
        errors.extend(
            error
            for error in attribution_errors
            if (error.code, error.stage, error.workbook, error.sheet_name)
            not in known_error_keys
        )
        return MonitorAttributionResult(
            workbook_count=net.workbook_count,
            reliable_workbook_count=net.reliable_workbook_count,
            changes=tuple(attributed),
            errors=tuple(errors),
            field_catalog=net.field_catalog,
        )
