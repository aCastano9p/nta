"""NYSE regular sessions and quote-mark scheduling.

The only I/O-ish part is nyse_sessions (pandas_market_calendars); everything else is pure
and works on a list of Session objects, so tests can use real holidays or hand-made lists.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from nta.schema import QuoteKind

MARK_CLAMP_BEFORE_CLOSE = timedelta(minutes=1)


@dataclass(frozen=True)
class Session:
    open: datetime   # UTC, inclusive
    close: datetime  # UTC, exclusive


@lru_cache(maxsize=32)
def _nyse_sessions_cached(start: date, end: date) -> tuple[Session, ...]:
    import pandas_market_calendars as pmc

    sched = pmc.get_calendar("XNYS").schedule(start_date=start.isoformat(), end_date=end.isoformat())
    return tuple(
        Session(o.to_pydatetime().astimezone(UTC), c.to_pydatetime().astimezone(UTC))
        for o, c in zip(sched["market_open"], sched["market_close"])
    )


def nyse_sessions(start: date, end: date) -> list[Session]:
    """Regular NYSE sessions with dates in [start, end], holidays and early closes applied."""
    return list(_nyse_sessions_cached(start, end))


def sessions_around(t: datetime, days_before: int = 7, days_after: int = 21) -> list[Session]:
    d = t.astimezone(UTC).date()
    return nyse_sessions(d - timedelta(days=days_before), d + timedelta(days=days_after))


# ---------- pure ----------

def session_index_at(sessions: list[Session], t: datetime) -> int | None:
    """Index of the session containing t (open <= t < close), else None."""
    i = bisect_right([s.open for s in sessions], t) - 1
    if i >= 0 and t < sessions[i].close:
        return i
    return None


def is_open(sessions: list[Session], t: datetime) -> bool:
    return session_index_at(sessions, t) is not None


def roll_forward(sessions: list[Session], t: datetime) -> tuple[datetime, int]:
    """t itself if it is inside a session, else the next session's open.
    Returns (time, session_index). Raises if the session list ends too early."""
    i = session_index_at(sessions, t)
    if i is not None:
        return t, i
    for j, s in enumerate(sessions):
        if s.open > t:
            return s.open, j
    raise ValueError(f"no session after {t.isoformat()}; extend the session list")


def schedule_quotes(sessions: list[Session], first_observed_at: datetime) -> dict[QuoteKind, datetime]:
    """Due times for the observe quote and the three marks.

    observe: first_observed_at if the market is open, else the next session open.
    mark_1h: observe + 1 hour, rolled to the next open if that falls outside a session.
    mark_1d / mark_5d: the same time of day, 1 / 5 sessions after the observe session,
        clamped to 1 minute before that session's close (early closes).
    Marks are anchored on the observe time, not on first_observed_at, so a filing made
    after the close still gets a real 1-hour move (open -> open + 1h) instead of zero.
    """
    observe, idx = roll_forward(sessions, first_observed_at)
    out = {QuoteKind.observe: observe}
    out[QuoteKind.mark_1h] = roll_forward(sessions, observe + timedelta(hours=1))[0]
    offset = observe - sessions[idx].open
    for kind, n in ((QuoteKind.mark_1d, 1), (QuoteKind.mark_5d, 5)):
        if idx + n >= len(sessions):
            raise ValueError("session list too short for mark_5d; extend it")
        s = sessions[idx + n]
        out[kind] = min(s.open + offset, s.close - MARK_CLAMP_BEFORE_CLOSE)
    return out


def periodic_slot(t: datetime, every: timedelta = timedelta(minutes=5)) -> datetime:
    """Start of the wall-clock slot containing t (e.g. 14:35:00 for 14:37:12)."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    n = (t - epoch) // every
    return epoch + n * every
