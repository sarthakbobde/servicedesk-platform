"""Working-time engine worked examples (Phase 1 task 8, F4.7, F6.3). Pure functions: no database needed."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ticketing import worktime as wt

UTC = timezone.utc
WEEKDAYS_9_17 = {d: ((540, 1020),) for d in range(1, 6)}


def u(*args):
    return datetime(*args, tzinfo=UTC)


def cal(windows=None, holidays=None, tz="UTC"):
    return wt.CalendarSpec(time_zone=tz, windows=windows if windows is not None else WEEKDAYS_9_17,
                           holidays=holidays or {})


# 2026-11-16 is a Monday.
@pytest.mark.parametrize("start,minutes,expected", [
    (u(2026, 11, 16, 16, 0), 120, u(2026, 11, 17, 10, 0)),   # rolls past the end of day
    (u(2026, 11, 20, 16, 30), 60, u(2026, 11, 23, 9, 30)),   # Friday to Monday over the weekend
    (u(2026, 11, 21, 12, 0), 30, u(2026, 11, 23, 9, 30)),    # starts on Saturday
    (u(2026, 11, 16, 7, 0), 60, u(2026, 11, 16, 10, 0)),     # starts before opening
    (u(2026, 11, 16, 10, 0), 480 * 5, u(2026, 11, 23, 10, 0)),  # exactly one working week
])
def test_due_times_standard_week(start, minutes, expected):
    assert wt.add_working_minutes(cal(), start, minutes) == expected


def test_whole_day_holiday_is_skipped():
    c = cal(holidays={date(2026, 11, 17): None})
    assert wt.add_working_minutes(c, u(2026, 11, 16, 16, 0), 120) == u(2026, 11, 18, 10, 0)


def test_partial_holiday_blocks_only_its_window():
    c = cal(holidays={date(2026, 11, 17): ((540, 720),)})  # Tuesday 09:00-12:00 off
    assert wt.add_working_minutes(c, u(2026, 11, 16, 16, 0), 120) == u(2026, 11, 17, 13, 0)


def test_overnight_window_runs_past_midnight():
    night = {d: ((1320, 360),) for d in range(1, 6)}  # 22:00 to 06:00
    c = cal(windows=night)
    assert wt.add_working_minutes(c, u(2026, 11, 16, 23, 0), 240) == u(2026, 11, 17, 3, 0)
    assert wt.is_working(c, u(2026, 11, 17, 5, 59))
    assert not wt.is_working(c, u(2026, 11, 17, 6, 0))
    # Friday's night shift ends Saturday 06:00; the next starts Monday 22:00.
    assert wt.add_working_minutes(c, u(2026, 11, 21, 5, 0), 120) == u(2026, 11, 23, 23, 0)


def test_overnight_window_and_holiday_on_the_next_day():
    night = {d: ((1320, 360),) for d in range(1, 6)}
    c = cal(windows=night, holidays={date(2026, 11, 17): None})
    # Monday 22:00-24:00 counts (120 min). The holiday blocks all of Tuesday's local date, including both the
    # 00:00-06:00 tail of Monday's shift and 22:00-24:00 of Tuesday's. Work resumes Wednesday 00:00.
    assert wt.working_minutes(c, u(2026, 11, 16, 22, 0), u(2026, 11, 18, 1, 0)) == 180
    assert wt.add_working_minutes(c, u(2026, 11, 16, 22, 0), 150) == u(2026, 11, 18, 0, 30)


def test_time_zone_kolkata():
    c = cal(windows={d: ((540, 1080),) for d in range(1, 6)}, tz="Asia/Kolkata")
    # 03:00 UTC = 08:30 IST, so work starts at 09:00 IST = 03:30 UTC; +60 = 04:30 UTC.
    assert wt.add_working_minutes(c, u(2026, 11, 16, 3, 0), 60) == u(2026, 11, 16, 4, 30)


def test_dst_change_new_york():
    c = cal(tz="America/New_York")
    # Friday 2026-10-30 16:00 EDT (20:00 UTC) + 120 min: 60 on Friday, 60 on Monday 2026-11-02 after DST ended.
    start = datetime(2026, 10, 30, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    due = wt.add_working_minutes(c, start, 120)
    assert due == u(2026, 11, 2, 15, 0)  # 10:00 EST
    assert due.astimezone(ZoneInfo("America/New_York")).hour == 10


def test_working_minutes_between_instants():
    c = cal()
    assert wt.working_minutes(c, u(2026, 11, 20, 16, 0), u(2026, 11, 23, 10, 0)) == 120
    assert wt.working_minutes(c, u(2026, 11, 21, 0, 0), u(2026, 11, 22, 23, 0)) == 0
    assert wt.working_minutes(c, u(2026, 11, 23, 10, 0), u(2026, 11, 20, 16, 0)) == 0


def test_full_day_and_on_shift():
    always = cal(windows={d: ((0, 1440),) for d in range(1, 8)})
    assert wt.add_working_minutes(always, u(2026, 11, 21, 23, 0), 90) == u(2026, 11, 22, 0, 30)
    assert wt.is_working(always, u(2026, 11, 22, 3, 0))
    assert not wt.is_working(cal(), u(2026, 11, 21, 12, 0))
    assert wt.is_working(cal(), u(2026, 11, 16, 9, 0))
    assert not wt.is_working(cal(), u(2026, 11, 16, 17, 0))


def test_calendar_without_hours_never_reaches_due():
    assert wt.add_working_minutes(cal(windows={}), u(2026, 11, 16, 9, 0), 10) is None
    assert not cal(windows={}).has_hours


def test_overlapping_windows_not_counted_twice():
    c = cal(windows={1: ((540, 720), (600, 780))})
    assert wt.working_minutes(c, u(2026, 11, 16, 0, 0), u(2026, 11, 17, 0, 0)) == 240


def test_preview_two_weeks():
    rows = wt.preview(cal(holidays={date(2026, 11, 17): None}), date(2026, 11, 16), 14)
    assert len(rows) == 14
    assert rows[0]["minutes"] == 480 and rows[0]["windows"] == ["09:00-17:00"]
    assert rows[1]["minutes"] == 0
    assert rows[5]["minutes"] == 0  # Saturday


def test_hhmm_parsing():
    assert wt.parse_hhmm("24:00") == 1440
    assert wt.parse_hhmm("09:30") == 570
    with pytest.raises(ValueError):
        wt.parse_hhmm("24:30")
