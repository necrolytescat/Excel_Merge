"""Filesystem ownership, atomic publication, references, and retention for M3 reports."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
from typing import Callable, Iterator
from uuid import UUID, uuid4

from app.schemas.monitor import MonitorReportPayload
from app.services.monitor_report_service import (
    _MANAGED_HISTORY,
    REPORT_RETENTION,
    REPORT_TIMEZONE,
    MonitorReportPublishError,
    MonitorReportReferenceError,
    ReportDraft,
    ReportPublication,
    ResolvedMonitorReport,
    _decode_embedded_report,
    create_report_draft,
    parse_report_reference,
    publication_from_draft,
)


class FileSystemMonitorReportPublisher:
    """Publish immutable history and maintain latest through a task-local lock."""

    def __init__(self, reports_root: str | Path):
        self.reports_root = Path(reports_root)

    @staticmethod
    def _task_segment(task_id: str | UUID) -> str:
        return str(UUID(str(task_id)))

    @staticmethod
    def _history_stem(cutoff: datetime) -> str:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("logical cutoff must include timezone")
        local = cutoff.astimezone(REPORT_TIMEZONE)
        base = local.strftime("%Y%m%d-%H%M%S")
        return f"{base}-{local.microsecond:06d}" if local.microsecond else base

    @staticmethod
    def _compat_history_stem(cutoff: datetime) -> str | None:
        local = cutoff.astimezone(REPORT_TIMEZONE)
        if local.microsecond != 0:
            return None
        return local.strftime("%Y%m%d-%H%M%S-000000")

    @classmethod
    def _stem_matches_cutoff(cls, stem: str, cutoff: datetime) -> bool:
        return stem == cls._history_stem(cutoff) or stem == cls._compat_history_stem(
            cutoff
        )

    def _paths(
        self, task_id: str | UUID, cutoff: datetime
    ) -> tuple[Path, Path, Path]:
        task_dir = self.reports_root / self._task_segment(task_id)
        history = task_dir / "history"
        stem = self._history_stem(cutoff)
        return (
            history / f"{stem}.json",
            history / f"{stem}.html",
            task_dir / "latest.html",
        )

    def _history_paths(
        self, task_id: str | UUID, cutoff: datetime
    ) -> tuple[Path, Path]:
        json_path, html_path, _ = self._paths(task_id, cutoff)
        if (
            json_path.exists()
            or json_path.is_symlink()
            or html_path.exists()
            or html_path.is_symlink()
        ):
            return json_path, html_path
        compat_stem = self._compat_history_stem(cutoff)
        if compat_stem is None:
            return json_path, html_path
        history = json_path.parent
        legacy_json = history / f"{compat_stem}.json"
        legacy_html = history / f"{compat_stem}.html"
        if (
            legacy_json.exists()
            or legacy_json.is_symlink()
            or legacy_html.exists()
            or legacy_html.is_symlink()
        ):
            return legacy_json, legacy_html
        return json_path, html_path

    def _validate_directories(
        self, task_id: str | UUID, *, create: bool
    ) -> tuple[Path, Path]:
        task_dir = self.reports_root / self._task_segment(task_id)
        history = task_dir / "history"
        for directory in (self.reports_root, task_dir, history):
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or not directory.is_dir():
                    raise MonitorReportPublishError(
                        "managed report directory ownership is invalid",
                        retryable=False,
                    )
            elif create:
                directory.mkdir()
        return task_dir, history

    @staticmethod
    def _regular_file(path: Path) -> bool:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        return stat.S_ISREG(mode) and not path.is_symlink()

    @staticmethod
    def _atomic_write(
        path: Path,
        content: bytes,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".m3tmp-{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if ensure_owned is not None:
                ensure_owned()
            os.replace(temporary, path)
        finally:
            try:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass

    def _write_immutable(
        self,
        path: Path,
        content: bytes,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> None:
        if path.exists() or path.is_symlink():
            if not self._regular_file(path) or path.read_bytes() != content:
                raise MonitorReportPublishError(
                    "managed report history conflicts", retryable=False
                )
            return
        self._atomic_write(path, content, ensure_owned=ensure_owned)

    @contextmanager
    def _latest_lock(self, latest_path: Path) -> Iterator[None]:
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = latest_path.parent / ".latest.lock"
        if lock_path.is_symlink() or (
            lock_path.exists() and not self._regular_file(lock_path)
        ):
            raise MonitorReportPublishError(
                "latest lock ownership is invalid", retryable=False
            )
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            try:
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def render(self, **kwargs) -> ReportDraft:
        return create_report_draft(**kwargs)

    def publish_history(
        self,
        draft: ReportDraft,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> ReportPublication:
        report = draft.payload
        try:
            self._validate_directories(report.task_id, create=True)
            json_path, html_path = self._history_paths(
                report.task_id, report.interval.logical_cutoff_at
            )
            self._write_immutable(
                json_path, draft.canonical_json, ensure_owned=ensure_owned
            )
            self._write_immutable(
                html_path, draft.offline_html, ensure_owned=ensure_owned
            )
        except MonitorReportPublishError:
            raise
        except OSError as error:
            raise MonitorReportPublishError("report history publication failed") from error
        return publication_from_draft(draft)

    def activate_latest(
        self,
        draft: ReportDraft,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> None:
        report = draft.payload
        try:
            self._validate_directories(report.task_id, create=True)
            _, _, latest_path = self._paths(
                report.task_id, report.interval.logical_cutoff_at
            )
            with self._latest_lock(latest_path):
                if latest_path.exists() or latest_path.is_symlink():
                    if not self._regular_file(latest_path):
                        raise MonitorReportPublishError(
                            "latest report is not a regular file", retryable=False
                        )
                    current = _decode_embedded_report(latest_path.read_bytes())
                    if current.task_id != report.task_id:
                        raise MonitorReportPublishError(
                            "latest report ownership mismatch", retryable=False
                        )
                    if (
                        current.interval.logical_cutoff_at
                        > report.interval.logical_cutoff_at
                    ):
                        return
                    if (
                        current.interval.logical_cutoff_at
                        == report.interval.logical_cutoff_at
                        and current != report
                    ):
                        raise MonitorReportPublishError(
                            "latest report conflicts at the same cutoff",
                            retryable=False,
                        )
                self._atomic_write(
                    latest_path,
                    draft.offline_html,
                    ensure_owned=ensure_owned,
                )
        except MonitorReportPublishError:
            raise
        except MonitorReportReferenceError as error:
            raise MonitorReportPublishError(
                "latest report content is invalid", retryable=False
            ) from error
        except OSError as error:
            raise MonitorReportPublishError("latest report publication failed") from error

    def resolve(
        self,
        *,
        task_id: str,
        run_id: str,
        logical_cutoff_at: datetime,
        reference: str,
        expected_json_sha256: str,
        expected_html_sha256: str | None = None,
    ) -> ResolvedMonitorReport:
        report_id = parse_report_reference(reference)
        try:
            self._validate_directories(task_id, create=False)
        except (MonitorReportPublishError, OSError) as error:
            raise MonitorReportReferenceError(
                "report directory ownership is invalid"
            ) from error
        json_path, html_path = self._history_paths(
            task_id, logical_cutoff_at
        )
        if not self._regular_file(json_path) or not self._regular_file(html_path):
            raise MonitorReportReferenceError("report artifact is unavailable")
        canonical = json_path.read_bytes()
        offline_html = html_path.read_bytes()
        json_sha = hashlib.sha256(canonical).hexdigest()
        html_sha = hashlib.sha256(offline_html).hexdigest()
        if json_sha != expected_json_sha256:
            raise MonitorReportReferenceError("report JSON checksum mismatch")
        if expected_html_sha256 is not None and html_sha != expected_html_sha256:
            raise MonitorReportReferenceError("report HTML checksum mismatch")
        try:
            payload = MonitorReportPayload.model_validate_json(canonical)
        except Exception as error:
            raise MonitorReportReferenceError("report JSON is invalid") from error
        cutoff = logical_cutoff_at.astimezone(timezone.utc)
        if (
            payload.report_id != report_id
            or str(payload.task_id) != self._task_segment(task_id)
            or str(payload.run_id) != str(UUID(run_id))
            or payload.interval.logical_cutoff_at != cutoff
        ):
            raise MonitorReportReferenceError("report ownership mismatch")
        if _decode_embedded_report(offline_html) != payload:
            raise MonitorReportReferenceError(
                "offline report does not match canonical JSON"
            )
        return ResolvedMonitorReport(
            payload=payload,
            canonical_json=canonical,
            offline_html=offline_html,
            json_sha256=json_sha,
            html_sha256=html_sha,
        )

    def load_registered(
        self,
        *,
        task_id: str,
        run_id: str,
        logical_cutoff_at: datetime,
        reference: str,
        json_sha256: str,
        html_sha256: str,
        report_expires_at: datetime,
    ) -> ReportDraft:
        resolved = self.resolve(
            task_id=task_id,
            run_id=run_id,
            logical_cutoff_at=logical_cutoff_at,
            reference=reference,
            expected_json_sha256=json_sha256,
            expected_html_sha256=html_sha256,
        )
        return ReportDraft(
            payload=resolved.payload,
            canonical_json=resolved.canonical_json,
            offline_html=resolved.offline_html,
            report_ref=reference,
            json_sha256=json_sha256,
            html_sha256=html_sha256,
            report_expires_at=report_expires_at,
        )

    @staticmethod
    def is_expired(expires_at: datetime, *, now: datetime) -> bool:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (expires_at, now)
        ):
            raise ValueError("expiry comparison requires timezone-aware values")
        return now.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc)

    def cleanup_expired(self, task_id: str, *, now: datetime) -> tuple[str, ...]:
        """Delete only validated paired history files for one task; never latest."""
        task_segment = self._task_segment(task_id)
        try:
            _, history = self._validate_directories(task_segment, create=False)
        except (MonitorReportPublishError, OSError):
            return ()
        if not history.exists():
            return ()
        removed: list[str] = []
        for json_path in sorted(history.iterdir(), key=lambda path: path.name):
            match = _MANAGED_HISTORY.fullmatch(json_path.name)
            if (
                match is None
                or match.group("kind") != "json"
                or not self._regular_file(json_path)
            ):
                continue
            html_path = json_path.with_suffix(".html")
            if (
                html_path.is_symlink()
                or (html_path.exists() and not self._regular_file(html_path))
            ):
                continue
            try:
                payload = MonitorReportPayload.model_validate_json(
                    json_path.read_bytes()
                )
            except Exception:
                continue
            if (
                str(payload.task_id) != task_segment
                or not self._stem_matches_cutoff(
                    match.group("stem"),
                    payload.interval.logical_cutoff_at,
                )
                or not self.is_expired(
                    payload.generated_at + REPORT_RETENTION, now=now
                )
            ):
                continue
            if self._regular_file(html_path):
                html_path.unlink()
            json_path.unlink()
            removed.append(match.group("stem"))
        return tuple(removed)
