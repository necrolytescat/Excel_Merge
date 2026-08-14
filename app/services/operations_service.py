from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from app.schemas.operations import (
    OperationsLogEntryPayload,
    OperationsLogListPayload,
    SVNCacheClearResultPayload,
    SVNCacheStatusPayload,
)


_LOG_NAME = re.compile(r"^excel-merge-(\d{8})-p(\d+)-(\d{3})\.jsonl$")
_CACHE_NAME = re.compile(r"^rev_.+__[0-9a-f]{32}\.bin$")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_TASK_ID = re.compile(rf"\btask_id=({_UUID})\b", re.IGNORECASE)
_REQUEST_ID = re.compile(rf"\brequest_id=({_UUID})\b", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:\\|\\\\)[^\s\"']+")
_FILE_URI = re.compile(r"\bfile://[^\s\"']+", re.IGNORECASE)
_SVN_URI = re.compile(r"\b(?:svn|svn\+ssh)://[^\s\"']+", re.IGNORECASE)
_URI_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|credential)\b\s*[:=]\s*([^\s,;]+)"
)
_INTERNAL_METRIC_KEY = re.compile(r"^[a-z0-9_.-]{1,64}$")
_SENSITIVE_INTERNAL_METRIC_KEYS = {
    "authorization",
    "canonical_url",
    "credential",
    "directory",
    "file_path",
    "password",
    "passwd",
    "path",
    "secret",
    "token",
    "url",
}
_SECRET_INTERNAL_METRIC_KEY_PARTS = {
    "authorization",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_log_text(value: Any, *, limit: int = 1024) -> str:
    text = str(value or "").replace("\x00", "")
    if "Traceback (most recent call last)" in text:
        text = text.split("Traceback (most recent call last)", 1)[0].rstrip()
        text = text or "内部异常详情已隐藏"
    lines = []
    for line in text.splitlines() or [text]:
        stripped = line.strip()
        if stripped.startswith("File \"") or stripped.startswith(("at ", "Caused by:")):
            continue
        lines.append(stripped)
    text = " ".join(part for part in lines if part)
    text = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _URI_CREDENTIALS.sub(lambda match: match.group("scheme") + "[redacted]@", text)
    text = _WINDOWS_PATH.sub("[internal-path]", text)
    text = _FILE_URI.sub("[internal-path]", text)
    text = _SVN_URI.sub("[svn-endpoint]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "日志内容已隐藏")[:limit]


def sanitize_internal_metrics(
    value: Any,
    *,
    depth: int = 0,
) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    """Bound and redact metrics stored only in the process JSONL file."""
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_log_text(value, limit=256)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:256]:
            key = str(raw_key).strip().lower()
            if not _INTERNAL_METRIC_KEY.fullmatch(key):
                continue
            key_parts = set(re.split(r"[._-]+", key))
            if (
                key in _SENSITIVE_INTERNAL_METRIC_KEYS
                or bool(key_parts & _SECRET_INTERNAL_METRIC_KEY_PARTS)
                or key.endswith(("_url", "_path", "_directory"))
            ):
                result[key] = "[redacted]"
                continue
            result[key] = sanitize_internal_metrics(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize_internal_metrics(item, depth=depth + 1)
            for item in list(value)[:256]
        ]
    return sanitize_log_text(value, limit=256)

class OperationsError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ProcessDailySizeJsonHandler(logging.Handler):
    def __init__(
        self,
        log_dir: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        retention_days: int = 14,
        max_files: int = 200,
    ) -> None:
        super().__init__(level=logging.DEBUG)
        self.log_dir = log_dir
        self.max_bytes = max(1024, int(max_bytes))
        self.retention_days = max(1, int(retention_days))
        self.max_files = max(2, int(max_files))
        self.process_id = os.getpid()
        self._last_cleanup_day = ""

    def _files(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        return [
            path
            for path in self.log_dir.iterdir()
            if path.is_file() and not path.is_symlink() and _LOG_NAME.fullmatch(path.name)
        ]

    def _target(self, day: str, line_size: int) -> Path:
        candidates: list[tuple[int, Path]] = []
        for path in self._files():
            match = _LOG_NAME.fullmatch(path.name)
            if match and match.group(1) == day and int(match.group(2)) == self.process_id:
                candidates.append((int(match.group(3)), path))
        if candidates:
            sequence, path = max(candidates, key=lambda item: item[0])
            if path.stat().st_size + line_size <= self.max_bytes:
                return path
            sequence += 1
        else:
            sequence = 0
        return self.log_dir / f"excel-merge-{day}-p{self.process_id}-{sequence:03d}.jsonl"

    def _cleanup(self, day: str) -> None:
        if self._last_cleanup_day == day:
            return
        self._last_cleanup_day = day
        cutoff = time.time() - self.retention_days * 86400
        files = sorted(self._files(), key=lambda path: path.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            if index >= self.max_files or path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                except OSError:
                    continue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            created = datetime.fromtimestamp(record.created, timezone.utc)
            day = created.strftime("%Y%m%d")
            message = sanitize_log_text(record.getMessage())
            task_id = getattr(record, "task_id", None)
            request_id = getattr(record, "request_id", None)
            if not task_id:
                match = _TASK_ID.search(record.getMessage())
                task_id = match.group(1) if match else None
            if not request_id:
                match = _REQUEST_ID.search(record.getMessage())
                request_id = match.group(1) if match else None
            if record.levelno >= logging.ERROR:
                level = "error"
            elif record.levelno >= logging.WARNING:
                level = "warning"
            elif record.levelno >= logging.INFO:
                level = "info"
            else:
                level = "debug"
            payload = {
                "event_id": str(uuid4()),
                "created_at": created.isoformat().replace("+00:00", "Z"),
                "level": level,
                "logger": sanitize_log_text(record.name, limit=128),
                "event": sanitize_log_text(getattr(record, "event", "application.log"), limit=64).lower(),
                "message": message,
                "request_id": str(request_id) if request_id else None,
                "task_id": str(task_id) if task_id else None,
                "process_id": self.process_id,
            }
            internal_metrics = getattr(record, "internal_metrics", None)
            if isinstance(internal_metrics, dict):
                payload["internal_metrics"] = sanitize_internal_metrics(
                    internal_metrics
                )

            if not re.fullmatch(r"[a-z0-9._-]+", payload["event"]):
                payload["event"] = "application.log"
            line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup(day)
            target = self._target(day, len(line))
            with target.open("ab") as handle:
                handle.write(line)
        except Exception:
            self.handleError(record)


class OperationalLogService:
    def __init__(
        self,
        log_dir: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        retention_days: int = 14,
        max_files: int = 200,
        max_scan_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.log_dir = log_dir
        self.max_scan_bytes = max(1024 * 1024, int(max_scan_bytes))
        self.handler = ProcessDailySizeJsonHandler(
            log_dir,
            max_bytes=max_bytes,
            retention_days=retention_days,
            max_files=max_files,
        )
        self._logger = logging.getLogger("app")
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._logger.addHandler(self.handler)
        if self._logger.level == logging.NOTSET or self._logger.level > logging.INFO:
            self._logger.setLevel(logging.INFO)
        self._started = True

    def close(self) -> None:
        if not self._started:
            return
        self._logger.removeHandler(self.handler)
        self.handler.close()
        self._started = False

    def record_request(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        duration_ms: int,
        request_id: UUID,
        task_id: UUID | None,
    ) -> None:
        logging.getLogger("app.http").info(
            "%s %s -> %s (%sms)",
            method,
            path,
            status_code,
            duration_ms,
            extra={
                "event": "http.request",
                "request_id": str(request_id),
                "task_id": str(task_id) if task_id else None,
            },
        )

    @staticmethod
    def _encode_cursor(entry: OperationsLogEntryPayload) -> str:
        raw = json.dumps(
            {"created_at": entry.created_at.isoformat(), "event_id": str(entry.event_id)},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
        if not cursor:
            return None
        try:
            padding = "=" * (-len(cursor) % 4)
            data = json.loads(base64.urlsafe_b64decode(cursor + padding))
            return datetime.fromisoformat(data["created_at"]), str(UUID(data["event_id"]))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise OperationsError("OPERATIONS_INVALID_CURSOR", "日志分页游标无效") from exc

    def _read_entries(self) -> list[OperationsLogEntryPayload]:
        if not self.log_dir.exists():
            return []
        files = [
            path
            for path in self.log_dir.iterdir()
            if path.is_file() and not path.is_symlink() and _LOG_NAME.fullmatch(path.name)
        ]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        entries: list[OperationsLogEntryPayload] = []
        scanned = 0
        for path in files:
            size = path.stat().st_size
            if scanned and scanned + size > self.max_scan_bytes:
                break
            scanned += size
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            raw = json.loads(line)
                            # Internal diagnostics never extend the public log contract.
                            raw.pop("internal_metrics", None)
                            raw["message"] = sanitize_log_text(raw.get("message"))
                            entries.append(OperationsLogEntryPayload.model_validate(raw))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
            except OSError:
                continue
        entries.sort(key=lambda item: (item.created_at, str(item.event_id)), reverse=True)
        return entries

    def list_logs(
        self,
        *,
        limit: int,
        cursor: str | None,
        level: str | None,
        query: str | None,
        task_id: UUID | None,
        request_id: UUID | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> OperationsLogListPayload:
        before = self._decode_cursor(cursor)
        if created_from is not None and created_from.tzinfo is None:
            created_from = created_from.replace(tzinfo=timezone.utc)
        if created_to is not None and created_to.tzinfo is None:
            created_to = created_to.replace(tzinfo=timezone.utc)
        needle = (query or "").strip().casefold()
        filtered = []
        for entry in self._read_entries():
            key = (entry.created_at, str(entry.event_id))
            if before and key >= before:
                continue
            if level and entry.level != level:
                continue
            if task_id and entry.task_id != task_id:
                continue
            if request_id and entry.request_id != request_id:
                continue
            if created_from and entry.created_at < created_from:
                continue
            if created_to and entry.created_at > created_to:
                continue
            if needle and needle not in " ".join((entry.logger, entry.event, entry.message)).casefold():
                continue
            filtered.append(entry)
            if len(filtered) > limit:
                break
        has_more = len(filtered) > limit
        items = filtered[:limit]
        return OperationsLogListPayload(
            items=items,
            has_more=has_more,
            next_cursor=self._encode_cursor(items[-1]) if has_more and items else None,
            as_of=utc_now(),
        )


class SVNCacheService:
    def __init__(
        self,
        cache_dir: Path | None,
        *,
        client: Any = None,
        enabled: bool = True,
        allow_clear: bool = True,
        excluded_roots: tuple[Path, ...] = (),
    ) -> None:
        self.cache_dir = cache_dir.resolve() if cache_dir is not None else None
        self.client = client
        self.enabled = bool(enabled and cache_dir is not None)
        self.allow_clear = bool(allow_clear)
        self.excluded_roots = tuple(path.resolve() for path in excluded_roots)
        self._commands: dict[UUID, SVNCacheClearResultPayload] = {}
        self._lock = threading.Lock()

    def _is_safe(self) -> bool:
        root = self.cache_dir
        if root is None or root.parent == root:
            return False
        if root.name.casefold() in {".git", "m2-fixtures", "m2-batch"}:
            return False
        for excluded in self.excluded_roots:
            if root == excluded or excluded in root.parents or root in excluded.parents:
                return False
        return True

    def _files(self) -> tuple[list[Path], int]:
        if not self.enabled or self.cache_dir is None or not self.cache_dir.exists():
            return [], 0
        managed: list[Path] = []
        ignored = 0
        for path in self.cache_dir.iterdir():
            if path.is_file() and not path.is_symlink() and _CACHE_NAME.fullmatch(path.name):
                managed.append(path)
            else:
                ignored += 1
        return managed, ignored

    def status(self) -> SVNCacheStatusPayload:
        files, ignored = self._files()
        size_bytes = 0
        last_modified: datetime | None = None
        for path in files:
            stat = path.stat()
            size_bytes += stat.st_size
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if last_modified is None or modified > last_modified:
                last_modified = modified
        metrics = self.client.cache_metrics() if self.client and hasattr(self.client, "cache_metrics") else {}
        memory_hits = int(metrics.get("memory_hits", 0))
        disk_hits = int(metrics.get("disk_hits", 0))
        misses = int(metrics.get("misses", 0))
        attempts = memory_hits + disk_hits + misses
        return SVNCacheStatusPayload(
            enabled=self.enabled,
            can_clear=self.enabled and self.allow_clear and self._is_safe(),
            file_count=len(files),
            size_bytes=size_bytes,
            ignored_file_count=ignored,
            memory_entry_count=int(metrics.get("memory_entries", 0)),
            session_memory_hits=memory_hits,
            session_disk_hits=disk_hits,
            session_misses=misses,
            session_writes=int(metrics.get("writes", 0)),
            session_hit_rate=((memory_hits + disk_hits) / attempts if attempts else None),
            last_modified_at=last_modified,
        )

    def clear(self, request_id: UUID) -> SVNCacheClearResultPayload:
        with self._lock:
            prior = self._commands.get(request_id)
            if prior is not None:
                return prior
            status = self.status()
            if not status.can_clear or self.cache_dir is None:
                raise OperationsError(
                    "SVN_CACHE_CLEAR_DISABLED",
                    "全局 SVN 缓存清理未启用或目录不安全",
                    status_code=409,
                )
            files, _ = self._files()
            removed_count = 0
            removed_size = 0
            for path in files:
                try:
                    size = path.stat().st_size
                    path.unlink()
                    removed_count += 1
                    removed_size += size
                except OSError as exc:
                    raise OperationsError(
                        "SVN_CACHE_CLEAR_FAILED",
                        "全局 SVN 缓存清理失败",
                        status_code=500,
                    ) from exc
            memory_count = 0
            if self.client and hasattr(self.client, "clear_memory_cache"):
                memory_count = int(self.client.clear_memory_cache())
            result = SVNCacheClearResultPayload(
                request_id=request_id,
                removed_file_count=removed_count,
                removed_size_bytes=removed_size,
                cleared_memory_entry_count=memory_count,
                completed_at=utc_now(),
            )
            self._commands[request_id] = result
            return result
