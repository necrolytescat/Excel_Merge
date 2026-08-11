"""Changed-path planning and incremental M3 event replay.

The legacy full-snapshot engine remains the formal path. This module is an
opt-in shadow implementation until fixed-revision equivalence gates pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import posixpath
from typing import Iterable, Literal, Mapping

from app.schemas.monitor import MonitorErrorStage, MonitorPublicErrorPayload
from app.services.monitor_attribution_service import (
    MonitorAttributionResult,
    MonitorAttributionService,
)
from app.services.monitor_diff_service import (
    EXCEL_EXTENSIONS,
    MonitorDiffService,
    MonitorNetDiff,
    MonitorSnapshot,
    MonitorWorkbookSnapshot,
    SvnMonitorSnapshotReader,
    _public_parse_error,
    _public_svn_error,
)
from app.services.monitor_performance import (
    MonitorPerformanceRecorder,
    monitor_semantic_fingerprint,
)
from core.m2_errors import M2ProcessingError
from core.svn_history import BranchCommit
from core.svn_provider import SVNProviderError, normalize_relative_path
from core.table_csv_parser import ParsedTableCsv, parse_table_csv
from core.workbook_manifest_parser import WorkbookManifest, parse_workbook_manifest


_PathKind = Literal["workbook", "csv", "irrelevant", "fallback"]
_SUPPORTED_ACTIONS = {"A", "D", "M", "R"}


@dataclass(frozen=True)
class MonitorManifestIndex:
    csv_owners: Mapping[str, frozenset[tuple[str, str]]]
    sheets_by_workbook: Mapping[str, frozenset[str]]
    unreliable_manifest: bool

    @classmethod
    def from_snapshot(
        cls,
        snapshot: MonitorSnapshot,
        *,
        csv_directory: str,
    ) -> "MonitorManifestIndex":
        expected = {
            (workbook, sheet_name): normalize_relative_path(
                posixpath.join(csv_directory, table.name)
            )
            for workbook, book in snapshot.workbooks.items()
            for sheet_name, table in book.sheets.items()
        }
        sheets = {
            workbook: frozenset(book.sheets)
            for workbook, book in snapshot.workbooks.items()
        }
        return cls._from_relationships(
            expected,
            sheets,
            unreliable_manifest=any(
                error.workbook is None
                or error.stage == MonitorErrorStage.MANIFEST_PARSE
                for error in snapshot.errors
            ),
        )

    @classmethod
    def _from_relationships(
        cls,
        expected_csv_paths: Mapping[tuple[str, str], str],
        sheets_by_workbook: Mapping[str, frozenset[str]],
        *,
        unreliable_manifest: bool,
    ) -> "MonitorManifestIndex":
        owners: dict[str, set[tuple[str, str]]] = {}
        for owner, path in expected_csv_paths.items():
            owners.setdefault(path.casefold(), set()).add(owner)
        return cls(
            csv_owners={key: frozenset(value) for key, value in owners.items()},
            sheets_by_workbook=dict(sheets_by_workbook),
            unreliable_manifest=unreliable_manifest,
        )


@dataclass(frozen=True)
class MonitorCandidatePlan:
    revision: int
    workbook_actions: tuple[tuple[str, str], ...]
    changed_csv_paths: tuple[str, ...]
    affected_sheets: tuple[tuple[str, str], ...]
    irrelevant_path_count: int
    fallback_reason: str | None = None

    @property
    def requires_fallback(self) -> bool:
        return self.fallback_reason is not None

    @property
    def affected_workbooks(self) -> tuple[str, ...]:
        values = {workbook for workbook, _ in self.workbook_actions}
        values.update(workbook for workbook, _ in self.affected_sheets)
        return tuple(sorted(values, key=str.casefold))


class MonitorChangedPathPlanner:
    def __init__(self, reader: SvnMonitorSnapshotReader):
        self.table_directory = reader.table_directory
        self.csv_directory = reader.csv_directory
        self.csv_extension = reader.layout.csv_extension.casefold()

    @staticmethod
    def _is_below(path: str, parent: str) -> bool:
        return path == parent or path.startswith(parent + "/")

    def _classify(self, raw_path: str) -> tuple[_PathKind, str]:
        try:
            path = normalize_relative_path(raw_path)
        except ValueError:
            return "fallback", "invalid_path"
        if self._is_below(path, self.table_directory):
            suffix = PurePosixPath(path).suffix.casefold()
            if path != self.table_directory and suffix in EXCEL_EXTENSIONS:
                return "workbook", path[len(self.table_directory) :].lstrip("/")
            return "fallback", "table_scope_change"
        if self._is_below(path, self.csv_directory):
            if (
                posixpath.dirname(path) == self.csv_directory
                and PurePosixPath(path).suffix.casefold() == self.csv_extension
            ):
                return "csv", path
            return "fallback", "csv_scope_change"
        return "irrelevant", path

    def plan(
        self,
        commit: BranchCommit,
        index: MonitorManifestIndex,
    ) -> MonitorCandidatePlan:
        if not commit.changed_paths:
            return MonitorCandidatePlan(
                commit.revision, (), (), (), 0, "missing_changed_paths"
            )
        workbook_actions: dict[str, list[str]] = {}
        csv_paths: set[str] = set()
        irrelevant = 0
        fallback_reason = None
        for changed in commit.changed_paths:
            action = changed.action.upper()
            if action not in _SUPPORTED_ACTIONS:
                fallback_reason = "unsupported_action"
                break
            kind, value = self._classify(changed.branch_relative_path)
            if kind == "fallback":
                fallback_reason = value
                break
            if kind == "irrelevant":
                irrelevant += 1
            elif kind == "workbook":
                workbook_actions.setdefault(value, []).append(action)
            else:
                csv_paths.add(value)

        affected_sheets: set[tuple[str, str]] = set()
        if fallback_reason is None:
            for path in csv_paths:
                owners = index.csv_owners.get(path.casefold(), frozenset())
                if not owners and index.unreliable_manifest:
                    fallback_reason = "csv_owner_unknown"
                    break
                affected_sheets.update(owners)

        stable_actions = []
        for workbook, actions in workbook_actions.items():
            final_action = "D" if all(action == "D" for action in actions) else actions[-1]
            stable_actions.append((workbook, final_action))
        return MonitorCandidatePlan(
            revision=commit.revision,
            workbook_actions=tuple(
                sorted(stable_actions, key=lambda item: item[0].casefold())
            ),
            changed_csv_paths=tuple(sorted(csv_paths, key=str.casefold)),
            affected_sheets=tuple(
                sorted(
                    affected_sheets,
                    key=lambda item: (item[0].casefold(), item[1].casefold()),
                )
            ),
            irrelevant_path_count=irrelevant,
            fallback_reason=fallback_reason,
        )


@dataclass(frozen=True)
class _IncrementalState:
    snapshot: MonitorSnapshot
    file_paths: frozenset[str]
    workbook_paths: Mapping[str, str]
    manifests: Mapping[str, WorkbookManifest]
    expected_csv_paths: Mapping[tuple[str, str], str]
    actual_csv_paths: Mapping[tuple[str, str], str]

    def index(self) -> MonitorManifestIndex:
        sheets = {
            workbook: frozenset(entry.sheet_name for entry in manifest.entries)
            for workbook, manifest in self.manifests.items()
        }
        unreliable = any(
            error.workbook is None or error.stage == MonitorErrorStage.MANIFEST_PARSE
            for error in self.snapshot.errors
        )
        return MonitorManifestIndex._from_relationships(
            self.expected_csv_paths,
            sheets,
            unreliable_manifest=unreliable,
        )


@dataclass(frozen=True)
class _LedgerEvent:
    change: object
    commit: BranchCommit


@dataclass(frozen=True)
class MonitorIncrementalReplayResult:
    result: MonitorAttributionResult
    plans: tuple[MonitorCandidatePlan, ...]
    semantic_fingerprint: str


@dataclass(frozen=True)
class MonitorShadowComparison:
    legacy: MonitorAttributionResult
    incremental: MonitorIncrementalReplayResult
    legacy_fingerprint: str
    matches: bool


class MonitorIncrementalReplayService:
    """Build one full start state, then replay changed workbook/Sheet state."""

    def __init__(
        self,
        diff_service: MonitorDiffService,
        *,
        performance: MonitorPerformanceRecorder | None = None,
    ):
        if not isinstance(diff_service.snapshot_reader, SvnMonitorSnapshotReader):
            raise TypeError("incremental replay requires SvnMonitorSnapshotReader")
        self.diff_service = diff_service
        self.reader = diff_service.snapshot_reader
        self.performance = performance or MonitorPerformanceRecorder()
        self.planner = MonitorChangedPathPlanner(self.reader)

    @staticmethod
    def _is_below(path: str, parent: str) -> bool:
        return path == parent or path.startswith(parent + "/")

    @staticmethod
    def _error_key(error: MonitorPublicErrorPayload) -> tuple[object, ...]:
        return (
            error.code,
            error.stage,
            error.message,
            error.retryable,
            error.workbook,
            error.sheet_name,
        )

    @classmethod
    def _stable_errors(
        cls, errors: Iterable[MonitorPublicErrorPayload]
    ) -> tuple[MonitorPublicErrorPayload, ...]:
        unique = {cls._error_key(error): error for error in errors}
        return tuple(
            sorted(
                unique.values(),
                key=lambda error: (
                    error.workbook or "",
                    error.sheet_name or "",
                    error.stage.value,
                    error.message,
                ),
            )
        )

    def _read(self, path: str, revision: int, kind: str) -> bytes:
        with self.performance.phase(f"incremental.read_{kind}"):
            raw = self.reader.history.read_path_bytes_at_revision(
                self.reader.identity, path, revision
            )
        self.performance.increment(f"incremental.{kind}_reads")
        self.performance.increment(f"incremental.{kind}_bytes", len(raw))
        return raw

    def _parse_manifest(self, raw: bytes) -> WorkbookManifest:
        with self.performance.phase("incremental.parse_manifest"):
            manifest = parse_workbook_manifest(
                raw,
                sheet_name=self.reader.layout.manifest_sheet_name,
                sheet_field=self.reader.layout.manifest_sheet_field,
                csv_name_field=self.reader.layout.manifest_csv_name_field,
                export_flag_field=self.reader.layout.manifest_export_flag_field,
            )
        self.performance.increment("incremental.manifest_parses")
        self.performance.increment(f"incremental.manifest_parser.{manifest.parser}")
        return manifest

    def _parse_csv(self, raw: bytes, csv_name: str) -> ParsedTableCsv:
        with self.performance.phase("incremental.parse_csv"):
            table = parse_table_csv(
                raw,
                csv_name,
                field_name_row=self.reader.layout.field_name_row,
                field_type_row=self.reader.layout.field_type_row,
                field_scope_row=self.reader.layout.field_scope_row,
                data_start_row=self.reader.layout.data_start_row,
                primary_key_fields=self.reader.layout.primary_key_fields,
            )
        self.performance.increment("incremental.csv_parses")
        return table

    def _csv_match(
        self, file_paths: Iterable[str], csv_name: str
    ) -> tuple[str | None, str]:
        expected = normalize_relative_path(
            posixpath.join(self.reader.csv_directory, csv_name)
        )
        matches = [
            path
            for path in file_paths
            if posixpath.dirname(path) == self.reader.csv_directory
            and PurePosixPath(path).name.casefold() == csv_name.casefold()
        ]
        exact = [path for path in matches if PurePosixPath(path).name == csv_name]
        selected = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
        return selected, expected

    def _load_sheet(
        self,
        *,
        revision: int,
        file_paths: Iterable[str],
        workbook: str,
        sheet_name: str,
        csv_name: str,
    ) -> tuple[ParsedTableCsv | None, str, str | None, MonitorPublicErrorPayload | None]:
        selected, expected = self._csv_match(file_paths, csv_name)
        if selected is None:
            return (
                None,
                expected,
                None,
                _public_parse_error(
                    stage=MonitorErrorStage.CSV_PARSE,
                    message="main 清单对应的 TableCsv 不存在或匹配不唯一",
                    workbook=workbook,
                    sheet_name=sheet_name,
                ),
            )
        try:
            raw = self._read(selected, revision, "csv")
        except SVNProviderError as error:
            return None, expected, selected, _public_svn_error(
                error, workbook=workbook, sheet_name=sheet_name
            )
        try:
            return self._parse_csv(raw, csv_name), expected, selected, None
        except M2ProcessingError:
            return (
                None,
                expected,
                selected,
                _public_parse_error(
                    stage=MonitorErrorStage.CSV_PARSE,
                    message="TableCsv 无法按冻结规则解析",
                    workbook=workbook,
                    sheet_name=sheet_name,
                ),
            )

    def _load_full_state(self, revision: int) -> _IncrementalState:
        try:
            with self.performance.phase("incremental.list_tree"):
                entries = self.reader.history.list_paths_at_revision(
                    self.reader.identity, revision
                )
        except SVNProviderError as error:
            snapshot = MonitorSnapshot(
                revision=revision, errors=(_public_svn_error(error),)
            )
            return _IncrementalState(snapshot, frozenset(), {}, {}, {}, {})
        file_paths = frozenset(
            sorted(
                {
                    normalize_relative_path(entry.path)
                    for entry in entries
                    if entry.kind == "file"
                },
                key=str.casefold,
            )
        )
        self.performance.increment("incremental.listed_paths", len(file_paths))
        workbook_paths = {
            path[len(self.reader.table_directory) :].lstrip("/"): path
            for path in file_paths
            if self._is_below(path, self.reader.table_directory)
            and PurePosixPath(path).suffix.casefold() in EXCEL_EXTENSIONS
        }
        workbooks: dict[str, MonitorWorkbookSnapshot] = {}
        manifests: dict[str, WorkbookManifest] = {}
        expected_paths: dict[tuple[str, str], str] = {}
        actual_paths: dict[tuple[str, str], str] = {}
        errors = []
        for workbook in sorted(workbook_paths, key=str.casefold):
            path = workbook_paths[workbook]
            try:
                raw = self._read(path, revision, "workbook")
            except SVNProviderError as error:
                errors.append(_public_svn_error(error, workbook=workbook))
                workbooks[workbook] = MonitorWorkbookSnapshot()
                continue
            try:
                manifest = self._parse_manifest(raw)
            except M2ProcessingError:
                errors.append(
                    _public_parse_error(
                        stage=MonitorErrorStage.MANIFEST_PARSE,
                        message="工作簿导出清单无法按冻结规则解析",
                        workbook=workbook,
                    )
                )
                workbooks[workbook] = MonitorWorkbookSnapshot()
                continue
            manifests[workbook] = manifest
            sheets = {}
            for entry in manifest.entries:
                csv_name = self.reader.layout.filename_template.format(
                    tbxName=entry.tbx_name
                )
                table, expected, actual, error = self._load_sheet(
                    revision=revision,
                    file_paths=file_paths,
                    workbook=workbook,
                    sheet_name=entry.sheet_name,
                    csv_name=csv_name,
                )
                owner = (workbook, entry.sheet_name)
                expected_paths[owner] = expected
                if actual is not None:
                    actual_paths[owner] = actual
                if error is not None:
                    errors.append(error)
                elif table is not None:
                    sheets[entry.sheet_name] = table
            workbooks[workbook] = MonitorWorkbookSnapshot(sheets=sheets)
        snapshot = MonitorSnapshot(
            revision=revision,
            workbooks=workbooks,
            errors=self._stable_errors(errors),
        )
        return _IncrementalState(
            snapshot=snapshot,
            file_paths=file_paths,
            workbook_paths=workbook_paths,
            manifests=manifests,
            expected_csv_paths=expected_paths,
            actual_csv_paths=actual_paths,
        )

    @staticmethod
    def _manifest_map(manifest: WorkbookManifest | None) -> dict[str, str]:
        if manifest is None:
            return {}
        return {entry.sheet_name: entry.tbx_name for entry in manifest.entries}

    def _apply_local(
        self,
        previous: _IncrementalState,
        commit: BranchCommit,
        plan: MonitorCandidatePlan,
    ) -> _IncrementalState:
        file_paths = set(previous.file_paths)
        for changed in commit.changed_paths:
            kind, value = self.planner._classify(changed.branch_relative_path)
            if kind not in {"workbook", "csv"}:
                continue
            path = normalize_relative_path(changed.branch_relative_path)
            if changed.action.upper() == "D":
                file_paths.discard(path)
            else:
                file_paths.add(path)

        workbooks = dict(previous.snapshot.workbooks)
        workbook_paths = dict(previous.workbook_paths)
        manifests = dict(previous.manifests)
        expected_paths = dict(previous.expected_csv_paths)
        actual_paths = dict(previous.actual_csv_paths)
        errors = list(previous.snapshot.errors)
        old_index = previous.index()
        candidate_sheets: set[tuple[str, str]] = set(plan.affected_sheets)

        for workbook, action in plan.workbook_actions:
            old_manifest = manifests.pop(workbook, None)
            old_map = self._manifest_map(old_manifest)
            errors = [error for error in errors if error.workbook != workbook]
            for owner in [key for key in expected_paths if key[0] == workbook]:
                expected_paths.pop(owner, None)
                actual_paths.pop(owner, None)
            path = normalize_relative_path(
                posixpath.join(self.reader.table_directory, workbook)
            )
            if action == "D":
                workbook_paths.pop(workbook, None)
                workbooks.pop(workbook, None)
                candidate_sheets.update((workbook, sheet) for sheet in old_map)
                continue
            workbook_paths[workbook] = path
            try:
                raw = self._read(path, commit.revision, "workbook")
            except SVNProviderError as error:
                errors.append(_public_svn_error(error, workbook=workbook))
                workbooks[workbook] = MonitorWorkbookSnapshot()
                candidate_sheets.update((workbook, sheet) for sheet in old_map)
                continue
            try:
                manifest = self._parse_manifest(raw)
            except M2ProcessingError:
                errors.append(
                    _public_parse_error(
                        stage=MonitorErrorStage.MANIFEST_PARSE,
                        message="工作簿导出清单无法按冻结规则解析",
                        workbook=workbook,
                    )
                )
                workbooks[workbook] = MonitorWorkbookSnapshot()
                candidate_sheets.update((workbook, sheet) for sheet in old_map)
                continue
            manifests[workbook] = manifest
            new_map = self._manifest_map(manifest)
            old_book = previous.snapshot.workbooks.get(
                workbook, MonitorWorkbookSnapshot()
            )
            reusable = {
                sheet: table
                for sheet, table in old_book.sheets.items()
                if old_map.get(sheet) == new_map.get(sheet)
            }
            workbooks[workbook] = MonitorWorkbookSnapshot(sheets=reusable)
            for sheet, tbx_name in new_map.items():
                owner = (workbook, sheet)
                expected_paths[owner] = normalize_relative_path(
                    posixpath.join(
                        self.reader.csv_directory,
                        self.reader.layout.filename_template.format(tbxName=tbx_name),
                    )
                )
                if old_map.get(sheet) == tbx_name:
                    actual = previous.actual_csv_paths.get(owner)
                    if actual is not None:
                        actual_paths[owner] = actual
            candidate_sheets.update(
                (workbook, sheet)
                for sheet in set(old_map) | set(new_map)
                if old_map.get(sheet) != new_map.get(sheet)
            )

        new_index = MonitorManifestIndex._from_relationships(
            expected_paths,
            {
                workbook: frozenset(self._manifest_map(manifest))
                for workbook, manifest in manifests.items()
            },
            unreliable_manifest=any(
                error.workbook is None
                or error.stage == MonitorErrorStage.MANIFEST_PARSE
                for error in errors
            ),
        )
        for csv_path in plan.changed_csv_paths:
            candidate_sheets.update(
                old_index.csv_owners.get(csv_path.casefold(), frozenset())
            )
            candidate_sheets.update(
                new_index.csv_owners.get(csv_path.casefold(), frozenset())
            )

        for workbook, sheet_name in sorted(
            candidate_sheets,
            key=lambda item: (item[0].casefold(), item[1].casefold()),
        ):
            errors = [
                error
                for error in errors
                if not (
                    error.workbook == workbook and error.sheet_name == sheet_name
                )
            ]
            manifest = manifests.get(workbook)
            manifest_map = self._manifest_map(manifest)
            book = workbooks.get(workbook)
            if book is None or sheet_name not in manifest_map:
                expected_paths.pop((workbook, sheet_name), None)
                actual_paths.pop((workbook, sheet_name), None)
                if book is not None:
                    sheets = dict(book.sheets)
                    sheets.pop(sheet_name, None)
                    workbooks[workbook] = MonitorWorkbookSnapshot(sheets=sheets)
                continue
            csv_name = self.reader.layout.filename_template.format(
                tbxName=manifest_map[sheet_name]
            )
            table, expected, actual, error = self._load_sheet(
                revision=commit.revision,
                file_paths=file_paths,
                workbook=workbook,
                sheet_name=sheet_name,
                csv_name=csv_name,
            )
            owner = (workbook, sheet_name)
            expected_paths[owner] = expected
            if actual is None:
                actual_paths.pop(owner, None)
            else:
                actual_paths[owner] = actual
            sheets = dict(book.sheets)
            sheets.pop(sheet_name, None)
            if table is not None:
                sheets[sheet_name] = table
            workbooks[workbook] = MonitorWorkbookSnapshot(sheets=sheets)
            if error is not None:
                errors.append(error)

        snapshot = MonitorSnapshot(
            revision=commit.revision,
            workbooks=workbooks,
            errors=self._stable_errors(errors),
        )
        return _IncrementalState(
            snapshot=snapshot,
            file_paths=frozenset(file_paths),
            workbook_paths=workbook_paths,
            manifests=manifests,
            expected_csv_paths=expected_paths,
            actual_csv_paths=actual_paths,
        )

    @staticmethod
    def _attribute_from_ledger(
        net: MonitorNetDiff,
        *,
        ledger: list[_LedgerEvent],
        blocked_workbooks: set[str],
        all_workbooks_blocked: bool,
    ) -> MonitorAttributionResult:
        attributed = []
        unresolved_workbooks: set[str] = set()
        for final in net.changes:
            match = None
            if not all_workbooks_blocked and final.workbook not in blocked_workbooks:
                for event in ledger:
                    if MonitorAttributionService._forms_final_state(event.change, final):
                        match = event
            if match is None:
                unresolved_workbooks.add(final.workbook)
                attributed.append(final)
            else:
                attributed.append(
                    final.model_copy(
                        update={
                            "attribution": MonitorAttributionService._attribution(
                                match.commit
                            )
                        }
                    )
                )

        net_error_workbooks = {
            error.workbook for error in net.errors if error.workbook is not None
        }
        has_global_net_error = any(error.workbook is None for error in net.errors)
        if all_workbooks_blocked and not has_global_net_error:
            attribution_errors = [MonitorAttributionService._incomplete_error(None)]
        else:
            attribution_errors = [
                MonitorAttributionService._incomplete_error(workbook)
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

    def replay(
        self,
        *,
        start_revision: int,
        end_revision: int,
        commits: list[BranchCommit],
    ) -> MonitorIncrementalReplayResult:
        with self.performance.phase("incremental.start_snapshot"):
            start = self._load_full_state(start_revision)
        previous = start
        plans = []
        ledger: list[_LedgerEvent] = []
        blocked_workbooks = {
            error.workbook
            for error in previous.snapshot.errors
            if error.workbook is not None
        }
        all_workbooks_blocked = any(
            error.workbook is None for error in previous.snapshot.errors
        )

        for commit in sorted(commits, key=lambda item: item.revision):
            with self.performance.phase("incremental.plan"):
                plan = self.planner.plan(commit, previous.index())
            plans.append(plan)
            self.performance.increment(
                "incremental.changed_path_count", len(commit.changed_paths)
            )
            self.performance.increment(
                "incremental.candidate_workbook_count", len(plan.affected_workbooks)
            )
            self.performance.increment(
                "incremental.candidate_sheet_count", len(plan.affected_sheets)
            )
            if plan.requires_fallback:
                self.performance.increment("incremental.fallback_count")
                with self.performance.phase("incremental.fallback_snapshot"):
                    current = self._load_full_state(commit.revision)
            else:
                with self.performance.phase("incremental.apply_local"):
                    current = self._apply_local(previous, commit, plan)
            with self.performance.phase("incremental.event_diff"):
                event_diff = self.diff_service.compare_snapshots(
                    previous.snapshot, current.snapshot
                )
            ledger.extend(
                _LedgerEvent(change=change, commit=commit)
                for change in event_diff.changes
            )
            if any(error.workbook is None for error in event_diff.errors):
                all_workbooks_blocked = True
            blocked_workbooks.update(
                error.workbook
                for error in event_diff.errors
                if error.workbook is not None
            )
            previous = current

        if previous.snapshot.revision != end_revision:
            self.performance.increment("incremental.terminal_fallback_count")
            with self.performance.phase("incremental.terminal_snapshot"):
                previous = self._load_full_state(end_revision)
        with self.performance.phase("incremental.final_diff"):
            net = self.diff_service.compare_snapshots(
                start.snapshot, previous.snapshot
            )
        with self.performance.phase("incremental.attribution_match"):
            result = self._attribute_from_ledger(
                net,
                ledger=ledger,
                blocked_workbooks=blocked_workbooks,
                all_workbooks_blocked=all_workbooks_blocked,
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
        self.performance.set_value("result.semantic_fingerprint", fingerprint)
        return MonitorIncrementalReplayResult(
            result=result,
            plans=tuple(plans),
            semantic_fingerprint=fingerprint,
        )


def compare_legacy_and_incremental(
    diff_service: MonitorDiffService,
    *,
    start_revision: int,
    end_revision: int,
    commits: list[BranchCommit],
    performance: MonitorPerformanceRecorder | None = None,
) -> MonitorShadowComparison:
    recorder = performance or MonitorPerformanceRecorder()
    with recorder.phase("shadow.legacy_total"):
        net = diff_service.compare_revisions(start_revision, end_revision)
        legacy = MonitorAttributionService(diff_service).attribute(
            net,
            start_revision=start_revision,
            commits=commits,
        )
    legacy_fingerprint = monitor_semantic_fingerprint(
        start_revision=start_revision,
        end_revision=end_revision,
        workbook_count=legacy.workbook_count,
        reliable_workbook_count=legacy.reliable_workbook_count,
        changes=legacy.changes,
        errors=legacy.errors,
        field_catalog=legacy.field_catalog,
    )
    with recorder.phase("shadow.incremental_total"):
        incremental = MonitorIncrementalReplayService(
            diff_service, performance=recorder
        ).replay(
            start_revision=start_revision,
            end_revision=end_revision,
            commits=commits,
        )
    matches = legacy_fingerprint == incremental.semantic_fingerprint
    recorder.set_value("shadow.matches", matches)
    return MonitorShadowComparison(
        legacy=legacy,
        incremental=incremental,
        legacy_fingerprint=legacy_fingerprint,
        matches=matches,
    )
