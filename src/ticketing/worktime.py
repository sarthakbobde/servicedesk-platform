"""Working-time engine (Phase 1, task 8).

Pure functions over a `CalendarSpec`: no database, no wall clock. Instants are timezone-aware datetimes (UTC in
practice); the calendar's windows and holidays are interpreted in its own IANA time zone.

Rules
- A weekly window is (start_minute, end_minute) after local midnight. 0..1440 is a full day. An end earlier than
  (or equal to) the start runs past midnight into the next day.
- A holiday with no window blocks the whole local date (00:00 to 24:00). A holiday with a window blocks only that
  window on that date (a "partial day").
- Overlapping windows are merged, so time is never counted twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Iterator
from zoneinfo import ZoneInfo

MAX_DAYS = 3 * 366  # search horizon; a calendar with no working time inside it yields "never"
DAY = 1440


@dataclass(frozen=True)
class CalendarSpec:
    time_zone: str
    # weekday (1 = Monday .. 7 = Sunday) -> tuple of (start_minute, end_minute)
    windows: dict[int, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    # local date -> None (whole day) or tuple of blocked (start_minute, end_minute)
    holidays: dict[date, tuple[tuple[int, int], ...] | None] = field(default_factory=dict)

    def __hash__(self):  # dict fields: hash on a frozen repr so lru_cache can use the spec
        return hash((self.time_zone, tuple(sorted(self.windows.items())), tuple(sorted(self.holidays.items(),
                     key=lambda kv: kv[0]))))

    @property
    def tz(self) -> ZoneInfo:
        return _zone(self.time_zone)

    @property
    def has_hours(self) -> bool:
        return any(e != s or (s == 0 and e == DAY) for ws in self.windows.values() for s, e in ws)


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _local(d: date, minute: int, tz: ZoneInfo) -> datetime:
    extra_days, m = divmod(minute, DAY)
    day = d + timedelta(days=extra_days)
    return datetime.combine(day, time(m // 60, m % 60), tzinfo=tz).astimezone(timezone.utc)


def _subtract(intervals: list[tuple[datetime, datetime]], blocks: list[tuple[datetime, datetime]]):
    for bs, be in blocks:
        out = []
        for s, e in intervals:
            if be <= s or bs >= e:
                out.append((s, e))
                continue
            if s < bs:
                out.append((s, bs))
            if be < e:
                out.append((be, e))
        intervals = out
    return intervals


def _holiday_blocks(spec: CalendarSpec, d: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    if d not in spec.holidays:
        return []
    windows = spec.holidays[d]
    if windows is None:
        return [(_local(d, 0, tz), _local(d, DAY, tz))]
    return [(_local(d, s, tz), _local(d, e if e > s else e + DAY, tz)) for s, e in windows]


def _day_intervals(spec: CalendarSpec, d: date) -> list[tuple[datetime, datetime]]:
    """Working intervals (UTC) that START on local date d, holidays removed."""
    tz = spec.tz
    raw = []
    for s, e in spec.windows.get(d.isoweekday(), ()):
        if s == 0 and e == DAY:
            raw.append((_local(d, 0, tz), _local(d, DAY, tz)))
        elif e > s:
            raw.append((_local(d, s, tz), _local(d, e, tz)))
        else:  # overnight (or 24h starting at s)
            raw.append((_local(d, s, tz), _local(d, e + DAY, tz)))
    if not raw:
        return []
    blocks = _holiday_blocks(spec, d, tz) + _holiday_blocks(spec, d + timedelta(days=1), tz)
    return sorted(i for i in _subtract(raw, blocks) if i[1] > i[0])


def iter_intervals(spec: CalendarSpec, start: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Merged working intervals in time order, clipped to begin no earlier than `start`."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    local_day = start.astimezone(spec.tz).date() - timedelta(days=1)  # catch overnight windows from yesterday
    pending: tuple[datetime, datetime] | None = None
    for offset in range(MAX_DAYS):
        for s, e in _day_intervals(spec, local_day + timedelta(days=offset)):
            if e <= start:
                continue
            s = max(s, start)
            if pending is None:
                pending = (s, e)
            elif s <= pending[1]:
                pending = (pending[0], max(pending[1], e))
            else:
                yield pending
                pending = (s, e)
    if pending is not None:
        yield pending


def working_seconds(spec: CalendarSpec, a: datetime, b: datetime) -> int:
    if b <= a:
        return 0
    total = 0.0
    for s, e in iter_intervals(spec, a):
        if s >= b:
            break
        total += (min(e, b) - s).total_seconds()
    return int(total)


def working_minutes(spec: CalendarSpec, a: datetime, b: datetime) -> int:
    return working_seconds(spec, a, b) // 60


def add_working_seconds(spec: CalendarSpec, start: datetime, seconds: float) -> datetime | None:
    """The instant at which `seconds` of working time have elapsed after `start`. None if never (no hours)."""
    if seconds <= 0:
        # Zero remaining: due now if inside working time, else at the next working instant start.
        return start
    remaining = float(seconds)
    for s, e in iter_intervals(spec, start):
        length = (e - s).total_seconds()
        if remaining <= length:
            return s + timedelta(seconds=remaining)
        remaining -= length
    return None


def add_working_minutes(spec: CalendarSpec, start: datetime, minutes: float) -> datetime | None:
    return add_working_seconds(spec, start, minutes * 60)


def is_working(spec: CalendarSpec, instant: datetime) -> bool:
    for s, e in iter_intervals(spec, instant):
        return s <= instant < e
    return False


def next_working_start(spec: CalendarSpec, instant: datetime) -> datetime | None:
    for s, _ in iter_intervals(spec, instant):
        return s
    return None


def preview(spec: CalendarSpec, start_day: date, days: int = 14) -> list[dict]:
    """Per local day: the working windows and minutes (S24's two-week preview)."""
    tz = spec.tz
    begin = datetime.combine(start_day, time(0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(start_day + timedelta(days=days), time(0), tzinfo=tz).astimezone(timezone.utc)
    out = {start_day + timedelta(days=i): {"date": (start_day + timedelta(days=i)).isoformat(), "windows": [],
                                           "minutes": 0} for i in range(days)}
    for s, e in iter_intervals(spec, begin):
        if s >= end:
            break
        e = min(e, end)
        cursor = s
        while cursor < e:  # split at local midnight so each day shows its own share
            local = cursor.astimezone(tz)
            midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz).astimezone(timezone.utc)
            piece_end = min(e, midnight)
            row = out.get(local.date())
            if row is not None:
                row["windows"].append(f"{local:%H:%M}-{piece_end.astimezone(tz):%H:%M}".replace("-00:00", "-24:00"))
                row["minutes"] += int((piece_end - cursor).total_seconds() // 60)
            cursor = piece_end
    return list(out.values())


def parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes; '24:00' allowed."""
    hh, mm = value.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(value)
    return h * 60 + m


def fmt_hhmm(minute: int | None) -> str | None:
    if minute is None:
        return None
    return f"{minute // 60:02d}:{minute % 60:02d}"
