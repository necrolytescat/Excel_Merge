"""Deterministic Asia/Shanghai boundary planning for M3 monitoring."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_HISTORY = timedelta(days=30)


class MonitorScheduleError(ValueError):
    pass


class BoundaryType(str, Enum):
    START = "start"
    SCHEDULED = "scheduled"
    PAUSE = "pause"
    RESUME = "resume"
    END = "end"


@dataclass(frozen=True)
class BoundarySpec:
    boundary_at: datetime
    boundary_type: BoundaryType
    generation: int
    reason: str


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MonitorScheduleError("monitor time must include a UTC offset")
    return value.astimezone(timezone.utc)


def validate_creation_window(
    effective_at: datetime,
    end_at: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime | None, datetime]:
    effective = require_utc(effective_at)
    end = require_utc(end_at) if end_at is not None else None
    current = require_utc(now)
    if effective < current - MAX_HISTORY:
        raise MonitorScheduleError("effective_at cannot be more than 30 days in the past")
    if end is not None and end <= effective:
        raise MonitorScheduleError("end_at must be later than effective_at")
    return effective, end, current


def _local_cutoff(day, trigger: time) -> datetime:
    if trigger.tzinfo is not None or trigger.microsecond:
        raise MonitorScheduleError("daily trigger must be a whole-second wall time")
    return datetime.combine(day, trigger, SHANGHAI).astimezone(timezone.utc)


def scheduled_boundaries(
    *,
    after: datetime,
    due_at: datetime,
    trigger: time,
    generation: int,
    end_at: datetime | None = None,
) -> list[BoundarySpec]:
    """Return report-producing boundaries in ``(after, due_at]``.

    The configured end is a final cutoff. If it equals a scheduled cutoff, only
    the scheduled boundary is emitted so the logical run remains unique.
    """
    anchor = require_utc(after)
    due = require_utc(due_at)
    end = require_utc(end_at) if end_at is not None else None
    if due <= anchor:
        return []
    if anchor < due - MAX_HISTORY:
        raise MonitorScheduleError("missed cutoff recovery exceeds the 30 day limit")

    last_day = min(due, end) if end is not None else due
    day = anchor.astimezone(SHANGHAI).date()
    final_day = last_day.astimezone(SHANGHAI).date()
    result: list[BoundarySpec] = []
    while day <= final_day:
        cutoff = _local_cutoff(day, trigger)
        if anchor < cutoff <= due and (end is None or cutoff <= end):
            result.append(
                BoundarySpec(cutoff, BoundaryType.SCHEDULED, generation, "daily_schedule")
            )
        day += timedelta(days=1)

    if end is not None and anchor < end <= due and all(
        item.boundary_at != end for item in result
    ):
        result.append(BoundarySpec(end, BoundaryType.END, generation, "configured_end"))
    return sorted(result, key=lambda item: item.boundary_at)


def next_scheduled_cutoff(
    *,
    after: datetime,
    trigger: time,
    end_at: datetime | None = None,
) -> datetime | None:
    anchor = require_utc(after)
    end = require_utc(end_at) if end_at is not None else None
    day = anchor.astimezone(SHANGHAI).date()
    for _ in range(3):
        cutoff = _local_cutoff(day, trigger)
        if cutoff > anchor:
            if end is not None and end < cutoff:
                return end if end > anchor else None
            return cutoff
        day += timedelta(days=1)
    raise AssertionError("failed to calculate the next daily cutoff")
