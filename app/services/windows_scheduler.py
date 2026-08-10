"""Windows Task Scheduler boundary for M3 monitor triggers.

The gateway accepts only structured, validated task definitions.  User supplied
monitor names, SVN locations and credentials never enter a system task action.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Protocol, Sequence
from uuid import UUID
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from app.schemas.monitor import MonitorPublicErrorPayload
from app.services.monitor_store import MonitorStateConflict, MonitorStore, TaskRecord
from app.services.monitor_task_service import CreateMonitorTask, MonitorTaskService


TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
MONITOR_TASK_PREFIX = "ExcelMerge-M3-Monitor-"
MAINTENANCE_TASK_NAME = "ExcelMerge-M3-Maintenance"
TEST_TASK_PREFIX = "ExcelMerge-M3-Test-"
TASK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")
RESTART_INTERVAL = "PT10M"
RESTART_COUNT = 3
EXECUTION_TIME_LIMIT = "PT6H"
MULTIPLE_INSTANCES_POLICY = "IgnoreNew"
DEFAULT_MAINTENANCE_TIME = time(3, 15)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class SchedulerGatewayError(RuntimeError):
    """Internal scheduler error.  Its details must not cross the API boundary."""


@dataclass(frozen=True)
class SchedulerAction:
    executable: str
    arguments: str
    working_directory: str


@dataclass(frozen=True)
class ExpectedSchedulerTask:
    name: str
    enabled: bool
    run_as: str
    action: SchedulerAction
    daily_trigger_time: time
    login_trigger: bool
    end_trigger_at: datetime | None = None
    start_when_available: bool = True
    restart_interval: str = RESTART_INTERVAL
    restart_count: int = RESTART_COUNT
    execution_time_limit: str = EXECUTION_TIME_LIMIT
    multiple_instances_policy: str = MULTIPLE_INSTANCES_POLICY

    def __post_init__(self) -> None:
        if not TASK_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("unsafe Windows task name")
        if not self.run_as or any(ord(char) < 32 for char in self.run_as):
            raise ValueError("invalid Windows run identity")
        trigger = self.daily_trigger_time
        if trigger.tzinfo is not None or trigger.microsecond:
            raise ValueError("scheduler trigger must be a whole-second wall time")
        if self.end_trigger_at is not None:
            if self.end_trigger_at.tzinfo is None or self.end_trigger_at.utcoffset() is None:
                raise ValueError("scheduler end trigger must include a timezone")
        if self.restart_count != RESTART_COUNT:
            raise ValueError("scheduler restart count is fixed")
        for value in (self.action.executable, self.action.working_directory):
            if not Path(value).is_absolute():
                raise ValueError("scheduler paths must be absolute")


@dataclass(frozen=True)
class SchedulerInspection:
    name: str
    exists: bool
    enabled: bool | None = None
    run_as: str | None = None
    executable: str | None = None
    arguments: str | None = None
    working_directory: str | None = None
    daily_trigger_time: time | None = None
    daily_trigger_enabled: bool | None = None
    login_trigger: bool | None = None
    login_trigger_enabled: bool | None = None
    login_trigger_user_id: str | None = None
    end_trigger_at: datetime | None = None
    end_trigger_enabled: bool | None = None
    principal_id: str | None = None
    logon_type: str | None = None
    run_level: str | None = None
    actions_context: str | None = None
    start_when_available: bool | None = None
    restart_interval: str | None = None
    restart_count: int | None = None
    execution_time_limit: str | None = None
    multiple_instances_policy: str | None = None


@dataclass(frozen=True)
class SchedulerValidation:
    valid: bool
    drift_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchedulerSyncResult:
    task_id: str
    generation: int
    status: str
    stale: bool = False


class SchedulerGateway(Protocol):
    def create_or_update(self, expected: ExpectedSchedulerTask) -> SchedulerInspection: ...
    def enable(self, name: str) -> SchedulerInspection: ...
    def disable(self, name: str) -> SchedulerInspection: ...
    def delete(self, name: str) -> SchedulerInspection: ...
    def run_now(self, name: str) -> SchedulerInspection: ...
    def inspect(self, name: str) -> SchedulerInspection: ...
    def validate(
        self,
        expected: ExpectedSchedulerTask,
        actual: SchedulerInspection | None = None,
    ) -> SchedulerValidation: ...


def _safe_task_name(name: str) -> str:
    if not TASK_NAME_PATTERN.fullmatch(name):
        raise ValueError("unsafe Windows task name")
    return name


def monitor_task_name(task_id: str | UUID) -> str:
    return f"{MONITOR_TASK_PREFIX}{str(UUID(str(task_id))).lower()}"


def current_windows_user() -> str:
    if os.name == "nt":
        token_query = 0x0008
        token_user = 1

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("sid", ctypes.c_void_p),
                ("attributes", wintypes.DWORD),
            ]

        class TokenUser(ctypes.Structure):
            _fields_ = [("user", SidAndAttributes)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            raise SchedulerGatewayError("current Windows identity is unavailable")
        try:
            required = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token, token_user, None, 0, ctypes.byref(required)
            )
            if required.value <= 0:
                raise SchedulerGatewayError("current Windows identity is unavailable")
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token,
                token_user,
                buffer,
                required,
                ctypes.byref(required),
            ):
                raise SchedulerGatewayError("current Windows identity is unavailable")
            user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                user.user.sid, ctypes.byref(sid_text)
            ):
                raise SchedulerGatewayError("current Windows identity is unavailable")
            try:
                sid = sid_text.value
            finally:
                kernel32.LocalFree(sid_text)
            if not sid or not sid.startswith("S-1-"):
                raise SchedulerGatewayError("current Windows identity is unavailable")
            return sid
        finally:
            kernel32.CloseHandle(token)
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{username}" if domain else username


def _system_executable(name: str) -> str:
    if os.name != "nt":
        raise SchedulerGatewayError("Windows system directory is unavailable")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise SchedulerGatewayError("Windows system directory is unavailable")
    executable = Path(buffer.value) / name
    if not executable.is_file():
        raise SchedulerGatewayError("Windows system executable is unavailable")
    return str(executable)


def _absolute(path: str | Path) -> str:
    return str(Path(path).resolve())


def _arguments(parts: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(parts))


def monitor_expected_task(
    task: TaskRecord,
    *,
    python_executable: str | Path,
    database_path: str | Path,
    working_directory: str | Path,
    run_as: str,
    action_generation: int | None = None,
) -> ExpectedSchedulerTask:
    task_id = str(UUID(task.task_id))
    generation = action_generation or task.generation
    if generation <= 0:
        raise ValueError("monitor generation must be positive")
    executable = _absolute(python_executable)
    database = _absolute(database_path)
    workdir = _absolute(working_directory)
    parts = (
        "-m",
        "app.monitor_runner",
        "--task-id",
        task_id,
        "--generation",
        str(generation),
        "--database",
        database,
        "--scheduler-managed",
    )
    return ExpectedSchedulerTask(
        name=monitor_task_name(task_id),
        enabled=task.lifecycle == "active" and task.scheduler_desired_state == "enabled",
        run_as=run_as,
        action=SchedulerAction(executable, _arguments(parts), workdir),
        daily_trigger_time=time.fromisoformat(task.daily_trigger_time),
        login_trigger=True,
        end_trigger_at=task.end_at if task.lifecycle == "active" else None,
    )


def maintenance_expected_task(
    *,
    python_executable: str | Path,
    database_path: str | Path,
    working_directory: str | Path,
    run_as: str,
    daily_trigger_time: time = DEFAULT_MAINTENANCE_TIME,
    name: str = MAINTENANCE_TASK_NAME,
) -> ExpectedSchedulerTask:
    executable = _absolute(python_executable)
    database = _absolute(database_path)
    workdir = _absolute(working_directory)
    parts = (
        "-m",
        "app.monitor_runner",
        "--maintenance",
        "--database",
        database,
    )
    return ExpectedSchedulerTask(
        name=_safe_task_name(name),
        enabled=True,
        run_as=run_as,
        action=SchedulerAction(executable, _arguments(parts), workdir),
        daily_trigger_time=daily_trigger_time,
        login_trigger=False,
    )


def scheduler_public_error(*, drift: bool = False) -> MonitorPublicErrorPayload:
    return MonitorPublicErrorPayload(
        code="MONITOR_SCHEDULER_SYNC_FAILED",
        stage="scheduler",
        message=(
            "Windows 计划任务与当前监控配置不一致"
            if drift
            else "Windows 计划任务同步失败"
        ),
        retryable=False,
    )


def validate_scheduler_task(
    expected: ExpectedSchedulerTask,
    actual: SchedulerInspection,
) -> SchedulerValidation:
    if not actual.exists:
        return SchedulerValidation(False, ("missing",))
    fields: list[str] = []
    comparisons = {
        "enabled": (expected.enabled, actual.enabled),
        "run_as": (expected.run_as.casefold(), (actual.run_as or "").casefold()),
        "executable": (
            os.path.normcase(expected.action.executable),
            os.path.normcase(actual.executable or ""),
        ),
        "arguments": (expected.action.arguments, actual.arguments),
        "working_directory": (
            os.path.normcase(expected.action.working_directory),
            os.path.normcase(actual.working_directory or ""),
        ),
        "daily_trigger_time": (expected.daily_trigger_time, actual.daily_trigger_time),
        "daily_trigger_enabled": (True, actual.daily_trigger_enabled),
        "login_trigger": (expected.login_trigger, actual.login_trigger),
        "login_trigger_enabled": (
            True if expected.login_trigger else None,
            actual.login_trigger_enabled,
        ),
        "login_trigger_user_id": (
            expected.run_as.casefold() if expected.login_trigger else None,
            (
                actual.login_trigger_user_id.casefold()
                if actual.login_trigger_user_id is not None
                else None
            ),
        ),
        "end_trigger_at": (
            (
                expected.end_trigger_at.astimezone(timezone.utc)
                if expected.end_trigger_at is not None
                else None
            ),
            actual.end_trigger_at,
        ),
        "end_trigger_enabled": (
            True if expected.end_trigger_at is not None else None,
            actual.end_trigger_enabled,
        ),
        "logon_type": ("InteractiveToken", actual.logon_type),
        "run_level": ("LeastPrivilege", actual.run_level),
        "actions_context": ("CurrentUser", actual.actions_context),
        "principal_binding": (
            True,
            actual.principal_id == "CurrentUser"
            and actual.actions_context == actual.principal_id,
        ),
        "start_when_available": (
            expected.start_when_available,
            actual.start_when_available,
        ),
        "restart_interval": (expected.restart_interval, actual.restart_interval),
        "restart_count": (expected.restart_count, actual.restart_count),
        "execution_time_limit": (
            expected.execution_time_limit,
            actual.execution_time_limit,
        ),
        "multiple_instances_policy": (
            expected.multiple_instances_policy,
            actual.multiple_instances_policy,
        ),
    }
    for name, (wanted, observed) in comparisons.items():
        if wanted != observed:
            fields.append(name)
    return SchedulerValidation(not fields, tuple(fields))


def _inspection_from_expected(expected: ExpectedSchedulerTask) -> SchedulerInspection:
    return SchedulerInspection(
        name=expected.name,
        exists=True,
        enabled=expected.enabled,
        run_as=expected.run_as,
        executable=expected.action.executable,
        arguments=expected.action.arguments,
        working_directory=expected.action.working_directory,
        daily_trigger_time=expected.daily_trigger_time,
        daily_trigger_enabled=True,
        login_trigger=expected.login_trigger,
        login_trigger_enabled=True if expected.login_trigger else None,
        login_trigger_user_id=expected.run_as if expected.login_trigger else None,
        end_trigger_at=(
            expected.end_trigger_at.astimezone(timezone.utc)
            if expected.end_trigger_at is not None
            else None
        ),
        end_trigger_enabled=True if expected.end_trigger_at is not None else None,
        principal_id="CurrentUser",
        logon_type="InteractiveToken",
        run_level="LeastPrivilege",
        actions_context="CurrentUser",
        start_when_available=expected.start_when_available,
        restart_interval=expected.restart_interval,
        restart_count=expected.restart_count,
        execution_time_limit=expected.execution_time_limit,
        multiple_instances_policy=expected.multiple_instances_policy,
    )


class FakeSchedulerGateway:
    def __init__(self) -> None:
        self.tasks: dict[str, SchedulerInspection] = {}
        self.operations: list[tuple[str, str]] = []
        self.fail_next: str | None = None
        self.on_create_or_update: Callable[[ExpectedSchedulerTask], None] | None = None
        self.on_run_now: Callable[[str], None] | None = None

    def _fail(self, operation: str) -> None:
        if self.fail_next == operation:
            self.fail_next = None
            raise SchedulerGatewayError("fake scheduler operation failed")

    def create_or_update(self, expected: ExpectedSchedulerTask) -> SchedulerInspection:
        self._fail("create_or_update")
        self.operations.append(("create_or_update", expected.name))
        if self.on_create_or_update is not None:
            self.on_create_or_update(expected)
        self.tasks[expected.name] = _inspection_from_expected(expected)
        validation = self.validate(expected, self.tasks[expected.name])
        if not validation.valid:
            raise SchedulerGatewayError("scheduler validation failed")
        return self.tasks[expected.name]

    def enable(self, name: str) -> SchedulerInspection:
        self._fail("enable")
        name = _safe_task_name(name)
        self.operations.append(("enable", name))
        actual = self.inspect(name)
        if not actual.exists:
            raise SchedulerGatewayError("scheduler task is missing")
        self.tasks[name] = replace(actual, enabled=True)
        return self.tasks[name]

    def disable(self, name: str) -> SchedulerInspection:
        self._fail("disable")
        name = _safe_task_name(name)
        self.operations.append(("disable", name))
        actual = self.inspect(name)
        if not actual.exists:
            raise SchedulerGatewayError("scheduler task is missing")
        self.tasks[name] = replace(actual, enabled=False)
        return self.tasks[name]

    def delete(self, name: str) -> SchedulerInspection:
        self._fail("delete")
        name = _safe_task_name(name)
        self.operations.append(("delete", name))
        self.tasks.pop(name, None)
        return self.inspect(name)

    def run_now(self, name: str) -> SchedulerInspection:
        self._fail("run_now")
        name = _safe_task_name(name)
        self.operations.append(("run_now", name))
        actual = self.inspect(name)
        if not actual.exists or not actual.enabled:
            raise SchedulerGatewayError("scheduler task cannot run")
        if self.on_run_now is not None:
            self.on_run_now(name)
        return actual

    def inspect(self, name: str) -> SchedulerInspection:
        self._fail("inspect")
        name = _safe_task_name(name)
        return self.tasks.get(name, SchedulerInspection(name=name, exists=False))

    def validate(
        self,
        expected: ExpectedSchedulerTask,
        actual: SchedulerInspection | None = None,
    ) -> SchedulerValidation:
        return validate_scheduler_task(expected, actual or self.inspect(expected.name))

    def drift(self, name: str, **changes: object) -> SchedulerInspection:
        current = self.inspect(name)
        if not current.exists:
            raise KeyError(name)
        changed = replace(current, **changes)
        self.tasks[name] = changed
        return changed


def scheduler_task_xml(expected: ExpectedSchedulerTask) -> bytes:
    ET.register_namespace("", TASK_NAMESPACE)
    task = ET.Element(f"{{{TASK_NAMESPACE}}}Task", {"version": "1.4"})
    registration = ET.SubElement(task, f"{{{TASK_NAMESPACE}}}RegistrationInfo")
    ET.SubElement(registration, f"{{{TASK_NAMESPACE}}}Author").text = expected.run_as
    triggers = ET.SubElement(task, f"{{{TASK_NAMESPACE}}}Triggers")
    calendar = ET.SubElement(triggers, f"{{{TASK_NAMESPACE}}}CalendarTrigger")
    local_start = datetime.combine(
        date.today(), expected.daily_trigger_time, tzinfo=SHANGHAI
    )
    ET.SubElement(calendar, f"{{{TASK_NAMESPACE}}}StartBoundary").text = (
        local_start.isoformat(timespec="seconds")
    )
    ET.SubElement(calendar, f"{{{TASK_NAMESPACE}}}Enabled").text = "true"
    schedule = ET.SubElement(calendar, f"{{{TASK_NAMESPACE}}}ScheduleByDay")
    ET.SubElement(schedule, f"{{{TASK_NAMESPACE}}}DaysInterval").text = "1"
    if expected.login_trigger:
        login = ET.SubElement(triggers, f"{{{TASK_NAMESPACE}}}LogonTrigger")
        ET.SubElement(login, f"{{{TASK_NAMESPACE}}}Enabled").text = "true"
        ET.SubElement(login, f"{{{TASK_NAMESPACE}}}UserId").text = expected.run_as
    if expected.end_trigger_at is not None:
        ending = ET.SubElement(triggers, f"{{{TASK_NAMESPACE}}}TimeTrigger")
        ET.SubElement(ending, f"{{{TASK_NAMESPACE}}}StartBoundary").text = (
            expected.end_trigger_at.astimezone(SHANGHAI).isoformat(timespec="seconds")
        )
        ET.SubElement(ending, f"{{{TASK_NAMESPACE}}}Enabled").text = "true"
    principals = ET.SubElement(task, f"{{{TASK_NAMESPACE}}}Principals")
    principal = ET.SubElement(
        principals, f"{{{TASK_NAMESPACE}}}Principal", {"id": "CurrentUser"}
    )
    ET.SubElement(principal, f"{{{TASK_NAMESPACE}}}UserId").text = expected.run_as
    ET.SubElement(principal, f"{{{TASK_NAMESPACE}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{TASK_NAMESPACE}}}RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, f"{{{TASK_NAMESPACE}}}Settings")
    ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}MultipleInstancesPolicy").text = (
        expected.multiple_instances_policy
    )
    ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}StartWhenAvailable").text = (
        str(expected.start_when_available).lower()
    )
    ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}Enabled").text = (
        str(expected.enabled).lower()
    )
    ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}ExecutionTimeLimit").text = (
        expected.execution_time_limit
    )
    restart = ET.SubElement(settings, f"{{{TASK_NAMESPACE}}}RestartOnFailure")
    ET.SubElement(restart, f"{{{TASK_NAMESPACE}}}Interval").text = expected.restart_interval
    ET.SubElement(restart, f"{{{TASK_NAMESPACE}}}Count").text = str(expected.restart_count)
    actions = ET.SubElement(
        task, f"{{{TASK_NAMESPACE}}}Actions", {"Context": "CurrentUser"}
    )
    execute = ET.SubElement(actions, f"{{{TASK_NAMESPACE}}}Exec")
    ET.SubElement(execute, f"{{{TASK_NAMESPACE}}}Command").text = (
        expected.action.executable
    )
    ET.SubElement(execute, f"{{{TASK_NAMESPACE}}}Arguments").text = (
        expected.action.arguments
    )
    ET.SubElement(execute, f"{{{TASK_NAMESPACE}}}WorkingDirectory").text = (
        expected.action.working_directory
    )
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def _text(root: ET.Element, path: str) -> str | None:
    element = root.find(path, {"t": TASK_NAMESPACE})
    return element.text if element is not None else None


def _xml_bool(root: ET.Element, path: str) -> bool | None:
    value = _text(root, path)
    return value.casefold() == "true" if value is not None else None


def parse_scheduler_task_xml(name: str, raw: bytes | str) -> SchedulerInspection:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as original:
        if not isinstance(raw, bytes):
            raise
        root = None
        for encoding in ("utf-8", "mbcs", "utf-16", "cp936"):
            try:
                text = raw.decode(encoding)
            except (LookupError, UnicodeError):
                continue
            text = re.sub(r"^\s*<\?xml[^?]*\?>", "", text, count=1)
            try:
                root = ET.fromstring(text)
                break
            except ET.ParseError:
                continue
        if root is None:
            raise original
    boundary = _text(root, ".//t:CalendarTrigger/t:StartBoundary")
    trigger_time = None
    if boundary:
        trigger_time = datetime.fromisoformat(boundary).time().replace(tzinfo=None)
    enabled_text = _text(root, "./t:Settings/t:Enabled")
    restart_count = _text(root, "./t:Settings/t:RestartOnFailure/t:Count")
    calendar = root.find(".//t:CalendarTrigger", {"t": TASK_NAMESPACE})
    login = root.find(".//t:LogonTrigger", {"t": TASK_NAMESPACE})
    ending = root.find(".//t:TimeTrigger", {"t": TASK_NAMESPACE})
    end_boundary = _text(root, ".//t:TimeTrigger/t:StartBoundary")
    end_trigger = None
    if end_boundary:
        parsed_end = datetime.fromisoformat(end_boundary)
        if parsed_end.tzinfo is None:
            parsed_end = parsed_end.replace(tzinfo=SHANGHAI)
        end_trigger = parsed_end.astimezone(timezone.utc)
    return SchedulerInspection(
        name=_safe_task_name(name),
        exists=True,
        enabled=(enabled_text or "true").casefold() == "true",
        run_as=_text(root, ".//t:Principals/t:Principal/t:UserId"),
        executable=_text(root, ".//t:Actions/t:Exec/t:Command"),
        arguments=_text(root, ".//t:Actions/t:Exec/t:Arguments") or "",
        working_directory=_text(root, ".//t:Actions/t:Exec/t:WorkingDirectory"),
        daily_trigger_time=trigger_time,
        daily_trigger_enabled=(
            _xml_bool(root, ".//t:CalendarTrigger/t:Enabled")
            if calendar is not None
            else None
        ),
        login_trigger=login is not None,
        login_trigger_enabled=(
            _xml_bool(root, ".//t:LogonTrigger/t:Enabled")
            if login is not None
            else None
        ),
        login_trigger_user_id=(
            _text(root, ".//t:LogonTrigger/t:UserId")
            if login is not None
            else None
        ),
        end_trigger_at=end_trigger,
        end_trigger_enabled=(
            _xml_bool(root, ".//t:TimeTrigger/t:Enabled")
            if ending is not None
            else None
        ),
        principal_id=(
            root.find(".//t:Principals/t:Principal", {"t": TASK_NAMESPACE}).get("id")
            if root.find(".//t:Principals/t:Principal", {"t": TASK_NAMESPACE})
            is not None
            else None
        ),
        logon_type=_text(root, ".//t:Principals/t:Principal/t:LogonType"),
        run_level=_text(root, ".//t:Principals/t:Principal/t:RunLevel"),
        actions_context=(
            root.find(".//t:Actions", {"t": TASK_NAMESPACE}).get("Context")
            if root.find(".//t:Actions", {"t": TASK_NAMESPACE}) is not None
            else None
        ),
        start_when_available=(
            (_text(root, "./t:Settings/t:StartWhenAvailable") or "false").casefold()
            == "true"
        ),
        restart_interval=_text(
            root, "./t:Settings/t:RestartOnFailure/t:Interval"
        ),
        restart_count=int(restart_count) if restart_count else None,
        execution_time_limit=_text(root, "./t:Settings/t:ExecutionTimeLimit"),
        multiple_instances_policy=_text(
            root, "./t:Settings/t:MultipleInstancesPolicy"
        ),
    )


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class WindowsSchedulerGateway:
    """Explicit schtasks.exe adapter; construction and import have no side effects."""

    def __init__(
        self,
        *,
        process_runner: ProcessRunner = subprocess.run,
        schtasks_path: str | Path | None = None,
    ):
        self.process_runner = process_runner
        self.schtasks_path = (
            str(Path(schtasks_path).resolve()) if schtasks_path is not None else None
        )

    def _schtasks(self) -> str:
        return self.schtasks_path or _system_executable("schtasks.exe")

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if os.name != "nt":
            raise SchedulerGatewayError("Windows Task Scheduler is unavailable")
        completed = self.process_runner(
            [self._schtasks(), *arguments],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise SchedulerGatewayError("Windows Task Scheduler operation failed")
        return completed

    def create_or_update(self, expected: ExpectedSchedulerTask) -> SchedulerInspection:
        raw = scheduler_task_xml(expected)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="excel-merge-m3-", suffix=".xml", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self._run(("/Create", "/TN", expected.name, "/XML", str(temporary), "/F"))
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        actual = self.inspect(expected.name)
        if not self.validate(expected, actual).valid:
            raise SchedulerGatewayError("Windows Task Scheduler validation failed")
        return actual

    def enable(self, name: str) -> SchedulerInspection:
        name = _safe_task_name(name)
        self._run(("/Change", "/TN", name, "/ENABLE"))
        return self.inspect(name)

    def disable(self, name: str) -> SchedulerInspection:
        name = _safe_task_name(name)
        self._run(("/Change", "/TN", name, "/DISABLE"))
        return self.inspect(name)

    def delete(self, name: str) -> SchedulerInspection:
        name = _safe_task_name(name)
        try:
            current = self.inspect(name)
        except SchedulerGatewayError:
            self._run(("/Delete", "/TN", name, "/F"))
            remaining = self.inspect(name)
            if remaining.exists:
                raise SchedulerGatewayError("Windows Task Scheduler deletion failed")
            return remaining
        if current.exists:
            self._run(("/Delete", "/TN", name, "/F"))
        remaining = self.inspect(name)
        if remaining.exists:
            raise SchedulerGatewayError("Windows Task Scheduler deletion failed")
        return remaining

    def run_now(self, name: str) -> SchedulerInspection:
        name = _safe_task_name(name)
        self._run(("/Run", "/TN", name))
        return self.inspect(name)

    def inspect(self, name: str) -> SchedulerInspection:
        name = _safe_task_name(name)
        if os.name != "nt":
            raise SchedulerGatewayError("Windows Task Scheduler is unavailable")
        completed = self.process_runner(
            [self._schtasks(), "/Query", "/TN", name, "/XML"],
            check=False,
            capture_output=True,
            text=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            diagnostic = b" ".join(
                part if isinstance(part, bytes) else str(part).encode()
                for part in (completed.stdout or b"", completed.stderr or b"")
            )
            decoded = []
            for encoding in ("utf-8", "mbcs", "utf-16", "cp936"):
                try:
                    decoded.append(diagnostic.decode(encoding).casefold())
                except (LookupError, UnicodeError):
                    continue
            missing_markers = (
                "cannot find",
                "not exist",
                "找不到",
            )
            if any(marker in text for marker in missing_markers for text in decoded):
                return SchedulerInspection(name=name, exists=False)
            raise SchedulerGatewayError("Windows Task Scheduler inspection failed")
        try:
            return parse_scheduler_task_xml(name, completed.stdout)
        except ET.ParseError as error:
            raise SchedulerGatewayError(
                "Windows Task Scheduler XML inspection failed"
            ) from error

    def validate(
        self,
        expected: ExpectedSchedulerTask,
        actual: SchedulerInspection | None = None,
    ) -> SchedulerValidation:
        return validate_scheduler_task(expected, actual or self.inspect(expected.name))


class MonitorSchedulerService:
    def __init__(
        self,
        store: MonitorStore,
        gateway: SchedulerGateway,
        *,
        database_path: str | Path,
        working_directory: str | Path,
        python_executable: str | Path = sys.executable,
        run_as: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.gateway = gateway
        self.database_path = _absolute(database_path)
        self.working_directory = _absolute(working_directory)
        self.python_executable = _absolute(python_executable)
        self.run_as = run_as or current_windows_user()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc)

    def expected(self, task: TaskRecord) -> ExpectedSchedulerTask:
        self._validate_task_identity(task)
        expected = monitor_expected_task(
            task,
            python_executable=self.python_executable,
            database_path=self.database_path,
            working_directory=self.working_directory,
            run_as=self.run_as,
        )
        if expected.name != task.windows_task_name:
            raise SchedulerGatewayError("stored scheduler identity is invalid")
        return expected

    @staticmethod
    def _validate_task_identity(task: TaskRecord) -> None:
        if task.windows_task_name != monitor_task_name(task.task_id):
            raise SchedulerGatewayError("stored scheduler identity is invalid")

    def _final_action(
        self, task: TaskRecord, generation: int
    ) -> ExpectedSchedulerTask:
        self._validate_task_identity(task)
        expected = monitor_expected_task(
            task,
            python_executable=self.python_executable,
            database_path=self.database_path,
            working_directory=self.working_directory,
            run_as=self.run_as,
            action_generation=generation,
        )
        if expected.name != task.windows_task_name:
            raise SchedulerGatewayError("stored scheduler identity is invalid")
        return replace(expected, enabled=True, end_trigger_at=None)

    def _pending_chain_generation(self, task: TaskRecord) -> int | None:
        if task.lifecycle not in {"paused", "ended"}:
            return None
        terminal_cutoff = (
            task.paused_at if task.lifecycle == "paused" else task.end_at
        )
        due = self.store.list_due_runs(task.task_id, self._now())
        pending_chain = [
            run
            for run in due
            if (
                run.status in {"queued", "running"}
                or (
                    run.status == "failed"
                    and any(error.retryable for error in run.errors)
                    and self.store.automatic_retry_count(run.run_id) < RESTART_COUNT
                )
            )
            and terminal_cutoff is not None
            and run.end_at <= terminal_cutoff
        ]
        if not pending_chain:
            return None
        return task.generation

    @staticmethod
    def _should_remove(task: TaskRecord) -> bool:
        return (
            task.lifecycle in {"ended", "archived"}
            or task.scheduler_desired_state == "removed"
        )

    def _repair_latest(self, task_id: str, stale_generation: int) -> None:
        latest = self.store.get_task(task_id)
        if latest is not None and latest.generation != stale_generation:
            self.sync_task(task_id, expected_generation=latest.generation, repair_stale=False)

    def sync_task(
        self,
        task_id: str,
        *,
        expected_generation: int | None = None,
        repair_stale: bool = True,
        trigger_final: bool = True,
    ) -> SchedulerSyncResult:
        task_id = str(UUID(task_id))
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        self._validate_task_identity(task)
        generation = task.generation
        if expected_generation is not None and generation != expected_generation:
            return SchedulerSyncResult(task_id, expected_generation, "stale", True)
        try:
            final_generation = self._pending_chain_generation(task)
            if final_generation is not None:
                temporary = self._final_action(task, final_generation)
                actual = self.gateway.create_or_update(temporary)
                if trigger_final:
                    self.gateway.run_now(temporary.name)
                validation = self.gateway.validate(temporary, actual)
                if not validation.valid:
                    raise SchedulerGatewayError("scheduler task validation failed")
                status = "pending"
            elif self._should_remove(task):
                actual = self.gateway.delete(task.windows_task_name)
                if actual.exists:
                    raise SchedulerGatewayError("scheduler task remains installed")
                status = "not_present"
            else:
                expected = self.expected(task)
                actual = self.gateway.create_or_update(expected)
                validation = self.gateway.validate(expected, actual)
                if not validation.valid:
                    raise SchedulerGatewayError("scheduler task validation failed")
                status = "synced"
            current = self.store.get_task(task_id)
            if current is None or current.generation != generation:
                if repair_stale:
                    self._repair_latest(task_id, generation)
                return SchedulerSyncResult(task_id, generation, "stale", True)
            try:
                self.store.update_task(
                    task_id,
                    {
                        "scheduler_sync_status": status,
                        "scheduler_last_synced_at": self._now(),
                        "scheduler_error": None,
                    },
                    self._now(),
                    expected_generation=generation,
                    expected_scheduler_sync_status=task.scheduler_sync_status,
                )
            except MonitorStateConflict:
                latest = self.store.get_task(task_id)
                if (
                    repair_stale
                    and latest is not None
                    and latest.generation != generation
                ):
                    self._repair_latest(task_id, generation)
                    return SchedulerSyncResult(task_id, generation, "stale", True)
                if (
                    repair_stale
                    and latest is not None
                    and latest.scheduler_sync_status in {"error", "drifted"}
                ):
                    self.inspect_task(task_id)
                    reconciled = self.store.get_task(task_id)
                    return SchedulerSyncResult(
                        task_id,
                        generation,
                        (
                            reconciled.scheduler_sync_status
                            if reconciled is not None
                            else "stale"
                        ),
                        reconciled is None,
                    )
                return SchedulerSyncResult(
                    task_id,
                    generation,
                    latest.scheduler_sync_status if latest is not None else "stale",
                    latest is None,
                )
            return SchedulerSyncResult(task_id, generation, status)
        except (SchedulerGatewayError, OSError, ET.ParseError):
            current = self.store.get_task(task_id)
            if current is None or current.generation != generation:
                if repair_stale:
                    self._repair_latest(task_id, generation)
                return SchedulerSyncResult(task_id, generation, "stale", True)
            if task.scheduler_sync_status in {"synced", "not_present"}:
                self.inspect_task(task_id)
                inspected = self.store.get_task(task_id)
                return SchedulerSyncResult(
                    task_id,
                    generation,
                    (
                        inspected.scheduler_sync_status
                        if inspected is not None
                        else "stale"
                    ),
                    inspected is None,
                )
            try:
                self.store.update_task(
                    task_id,
                    {
                        "scheduler_sync_status": "error",
                        "scheduler_error": scheduler_public_error(),
                    },
                    self._now(),
                    expected_generation=generation,
                    expected_scheduler_sync_status=task.scheduler_sync_status,
                )
            except MonitorStateConflict:
                latest = self.store.get_task(task_id)
                if (
                    repair_stale
                    and latest is not None
                    and latest.generation != generation
                ):
                    self._repair_latest(task_id, generation)
                    return SchedulerSyncResult(task_id, generation, "stale", True)
                return SchedulerSyncResult(
                    task_id,
                    generation,
                    latest.scheduler_sync_status if latest is not None else "stale",
                    latest is None,
                )
            return SchedulerSyncResult(task_id, generation, "error")

    def inspect_task(self, task_id: str) -> SchedulerValidation:
        task = self.store.get_task(str(UUID(task_id)))
        if task is None:
            raise KeyError(task_id)
        self._validate_task_identity(task)
        try:
            actual = self.gateway.inspect(task.windows_task_name)
            final_generation = self._pending_chain_generation(task)
            if final_generation is not None:
                validation = self.gateway.validate(
                    self._final_action(task, final_generation), actual
                )
                success_status = "pending"
            elif self._should_remove(task):
                validation = SchedulerValidation(
                    not actual.exists,
                    () if not actual.exists else ("unexpected_presence",),
                )
                success_status = "not_present"
            else:
                validation = self.gateway.validate(self.expected(task), actual)
                success_status = "synced"
            try:
                self.store.update_task(
                    task.task_id,
                    {
                        "scheduler_sync_status": (
                            success_status if validation.valid else "drifted"
                        ),
                        "scheduler_last_synced_at": self._now(),
                        "scheduler_error": (
                            None if validation.valid else scheduler_public_error(drift=True)
                        ),
                    },
                    self._now(),
                    expected_generation=task.generation,
                    expected_scheduler_sync_status=task.scheduler_sync_status,
                )
            except MonitorStateConflict:
                return SchedulerValidation(False, ("stale_generation",))
            return validation
        except (SchedulerGatewayError, OSError, ET.ParseError):
            try:
                self.store.update_task(
                    task.task_id,
                    {
                        "scheduler_sync_status": "error",
                        "scheduler_error": scheduler_public_error(),
                    },
                    self._now(),
                    expected_generation=task.generation,
                    expected_scheduler_sync_status=task.scheduler_sync_status,
                )
            except MonitorStateConflict:
                return SchedulerValidation(False, ("stale_generation",))
            return SchedulerValidation(False, ("inspection_failed",))

    def maintenance_expected(
        self,
        *,
        daily_trigger_time: time = DEFAULT_MAINTENANCE_TIME,
        name: str = MAINTENANCE_TASK_NAME,
    ) -> ExpectedSchedulerTask:
        return maintenance_expected_task(
            python_executable=self.python_executable,
            database_path=self.database_path,
            working_directory=self.working_directory,
            run_as=self.run_as,
            daily_trigger_time=daily_trigger_time,
            name=name,
        )

    def ensure_maintenance(
        self,
        *,
        daily_trigger_time: time = DEFAULT_MAINTENANCE_TIME,
        name: str = MAINTENANCE_TASK_NAME,
    ) -> SchedulerValidation:
        expected = self.maintenance_expected(
            daily_trigger_time=daily_trigger_time, name=name
        )
        actual = self.gateway.create_or_update(expected)
        validation = self.gateway.validate(expected, actual)
        if not validation.valid:
            raise SchedulerGatewayError("maintenance scheduler validation failed")
        return validation

    def inspect_maintenance(
        self,
        *,
        daily_trigger_time: time = DEFAULT_MAINTENANCE_TIME,
        name: str = MAINTENANCE_TASK_NAME,
    ) -> SchedulerValidation:
        expected = self.maintenance_expected(
            daily_trigger_time=daily_trigger_time, name=name
        )
        return self.gateway.validate(expected, self.gateway.inspect(expected.name))

    def delete_maintenance(self, *, name: str = MAINTENANCE_TASK_NAME) -> SchedulerInspection:
        return self.gateway.delete(_safe_task_name(name))


class ScheduledMonitorTaskService:
    """Persist a lifecycle change first, then reconcile its Windows trigger."""

    def __init__(
        self,
        tasks: MonitorTaskService,
        scheduler: MonitorSchedulerService,
    ):
        self.tasks = tasks
        self.scheduler = scheduler

    def _sync_current(self, task_id: str):
        task = self.tasks._require_task(task_id)
        self.scheduler.sync_task(task.task_id, expected_generation=task.generation)
        return self.tasks.to_public_task(self.tasks._require_task(task.task_id))

    def create(self, command: CreateMonitorTask):
        payload = self.tasks.create(command)
        return self._sync_current(str(payload.task_id))

    def modify_schedule(
        self,
        task_id: str,
        *,
        daily_trigger_time: time,
        end_at: datetime | None,
    ):
        payload = self.tasks.modify_schedule(
            task_id,
            daily_trigger_time=daily_trigger_time,
            end_at=end_at,
        )
        return self._sync_current(str(payload.task_id))

    def pause(self, task_id: str):
        payload = self.tasks.pause(task_id)
        return self._sync_current(str(payload.task_id))

    def resume(self, task_id: str):
        payload = self.tasks.resume(task_id)
        return self._sync_current(str(payload.task_id))

    def end(self, task_id: str):
        payload = self.tasks.end(task_id)
        return self._sync_current(str(payload.task_id))

    def archive(self, task_id: str):
        payload = self.tasks.archive(task_id)
        return self._sync_current(str(payload.task_id))
