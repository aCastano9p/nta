from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from nta.marketcal import (
    Session,
    is_open,
    nyse_sessions,
    periodic_slot,
    roll_forward,
    schedule_quotes,
)
from nta.schema import QuoteKind as K

ET = ZoneInfo("America/New_York")
SESSIONS = nyse_sessions(date(2026, 10, 20), date(2026, 12, 31))


def et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def sched(t: datetime) -> dict:
    return {k: v.astimezone(ET).strftime("%a %m-%d %H:%M") for k, v in schedule_quotes(SESSIONS, t).items()}


def test_calendar_knows_thanksgiving_and_early_close() -> None:
    days = {s.open.astimezone(ET).date(): s for s in SESSIONS}
    assert date(2026, 11, 26) not in days  # Thanksgiving
    assert days[date(2026, 11, 27)].close.astimezone(ET).hour == 13  # early close


def test_weekend_marks_exact() -> None:
    s = sched(et(2026, 11, 20, 15, 30))
    assert s[K.observe] == "Fri 11-20 15:30"
    assert s[K.mark_1h] == "Mon 11-23 09:30"
    assert s[K.mark_1d] == "Mon 11-23 15:30"
    # 5 sessions after Fri 11-20: Mon 23, Tue 24, Wed 25, Fri 27 (half day), Mon 30.
    assert s[K.mark_5d] == "Mon 11-30 15:30"


def test_after_close_before_holiday() -> None:
    # Wednesday 17:00 ET before Thanksgiving: observe at Friday's open, Thursday skipped.
    s = sched(et(2026, 11, 25, 17, 0))
    assert s == {
        K.observe: "Fri 11-27 09:30",
        K.mark_1h: "Fri 11-27 10:30",
        K.mark_1d: "Mon 11-30 09:30",
        K.mark_5d: "Fri 12-04 09:30",
    }


def test_early_close_clamps_and_rolls() -> None:
    # Wednesday 15:00 ET: 1h = 16:00 = close (exclusive) -> next open, which is Friday.
    # 1d = Friday 15:00, but Friday closes at 13:00 -> clamped to 12:59.
    s = sched(et(2026, 11, 25, 15, 0))
    assert s[K.mark_1h] == "Fri 11-27 09:30"
    assert s[K.mark_1d] == "Fri 11-27 12:59"


def test_weekend_and_holiday_filings_wait_for_open() -> None:
    assert sched(et(2026, 11, 21, 10, 0))[K.observe] == "Mon 11-23 09:30"   # Saturday
    assert sched(et(2026, 11, 26, 11, 0))[K.observe] == "Fri 11-27 09:30"   # Thanksgiving
    assert sched(et(2026, 11, 23, 9, 0))[K.observe] == "Mon 11-23 09:30"    # pre-market


def test_marks_keep_local_time_across_dst_change() -> None:
    # Friday Oct 30 15:00 EDT; DST ends Sunday Nov 1. 1d should be Monday 15:00 EST.
    assert sched(et(2026, 10, 30, 15, 0))[K.mark_1d] == "Mon 11-02 15:00"


def test_is_open_boundaries() -> None:
    assert is_open(SESSIONS, et(2026, 11, 23, 9, 30))
    assert not is_open(SESSIONS, et(2026, 11, 23, 16, 0))  # close is exclusive
    assert not is_open(SESSIONS, et(2026, 11, 26, 12, 0))


def test_short_session_list_raises() -> None:
    one = [Session(datetime(2026, 1, 2, 14, 30, tzinfo=UTC), datetime(2026, 1, 2, 21, tzinfo=UTC))]
    with pytest.raises(ValueError):
        schedule_quotes(one, datetime(2026, 1, 2, 15, tzinfo=UTC))
    with pytest.raises(ValueError):
        roll_forward(one, datetime(2026, 1, 3, tzinfo=UTC))


def test_periodic_slot() -> None:
    assert periodic_slot(datetime(2026, 11, 23, 14, 37, 12, tzinfo=UTC)) == \
        datetime(2026, 11, 23, 14, 35, tzinfo=UTC)
