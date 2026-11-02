"""Quote recorder.

Every tick (0.5 s by default):
1. schedule: each source_event without schedule rows gets observe/mark_1h/mark_1d/mark_5d
   rows in quote_schedule (persisted, so restarts lose nothing);
2. execute: while the market is open, every due schedule row without a matching quote is
   fetched from Alpaca and stored (done = a quote exists for (source_event_id, kind));
3. periodic: once per 5-minute wall-clock slot during market hours, a quote for every
   allowlisted instrument.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from nta import allowlist
from nta.alpaca import SOURCE, AlpacaData, Snapshot, alpaca_symbol
from nta.clock import Clock
from nta.edgar import new_york_date
from nta.ids import new_ulid
from nta.logs import log
from nta.marketcal import Session, is_open, periodic_slot, schedule_quotes, sessions_around
from nta.schema import Instrument, Quote, QuoteKind, QuoteSchedule, SourceEvent
from nta.shutdown import StopFlag


@dataclass
class TickStats:
    scheduled_events: int = 0
    recorded: int = 0
    missing: int = 0
    periodic: int = 0


class QuoteRecorder:
    def __init__(self, *, session_factory: sessionmaker[DbSession], alpaca: AlpacaData,
                 clock: Clock, run_id: str | None = None,
                 periodic_every: timedelta = timedelta(minutes=5),
                 sessions_fn: Callable[[datetime], list[Session]] = sessions_around) -> None:
        self._sf, self._alpaca, self._clock = session_factory, alpaca, clock
        self.run_id = run_id
        self._periodic_every = periodic_every
        self._sessions_fn = sessions_fn
        self._sessions: list[Session] = []
        self._sessions_day = None
        self._lock = threading.Lock()
        self._retry_after: dict[str, float] = {}  # schedule id -> monotonic time
        self.missing_retry_s = 5.0
        self.ticks_ok = 0
        self.ticks_failed = 0
        self.last_success_monotonic: float | None = None
        self.last_success_at: datetime | None = None
        with self._sf() as s:
            last = s.scalar(select(func.max(Quote.first_observed_at))
                            .where(Quote.kind == QuoteKind.periodic))
        self._last_periodic_slot = periodic_slot(last, periodic_every) if last else None

    def _market_open(self, now: datetime) -> bool:
        day = now.date()
        if day != self._sessions_day:
            self._sessions = self._sessions_fn(now)
            self._sessions_day = day
        return is_open(self._sessions, now)

    # -- step 1 --
    def schedule_new_events(self) -> int:
        with self._sf() as s:
            pending = s.execute(
                select(SourceEvent.id, SourceEvent.instrument_id, SourceEvent.first_observed_at)
                .where(SourceEvent.instrument_id.is_not(None))
                .where(~exists().where(QuoteSchedule.source_event_id == SourceEvent.id))
                .order_by(SourceEvent.first_observed_at)
            ).all()
        for event_id, instrument_id, observed in pending:
            due = schedule_quotes(self._sessions_fn(observed), observed)
            rows = [{"id": new_ulid(), "source_event_id": event_id, "instrument_id": instrument_id,
                     "kind": kind.value, "due_at": t} for kind, t in due.items()]
            with self._sf() as s, s.begin():
                s.execute(insert(QuoteSchedule).values(rows).on_conflict_do_nothing(
                    constraint="uq_quote_schedule_event_kind"))
            log("quotes_scheduled", source_event_id=event_id,
                **{k.value: v.isoformat() for k, v in due.items()})
        return len(pending)

    # -- step 2 --
    def execute_due(self, now: datetime) -> tuple[int, int]:
        if not self._market_open(now):
            return 0, 0
        with self._sf() as s:
            due = s.execute(
                select(QuoteSchedule, Instrument.ticker, SourceEvent.first_observed_at)
                .join(Instrument, Instrument.id == QuoteSchedule.instrument_id)
                .join(SourceEvent, SourceEvent.id == QuoteSchedule.source_event_id)
                .where(QuoteSchedule.due_at <= now)
                .where(~exists().where(and_(Quote.source_event_id == QuoteSchedule.source_event_id,
                                            Quote.kind == QuoteSchedule.kind)))
                .order_by(QuoteSchedule.due_at)
            ).all()
        mono = self._clock.monotonic()
        due = [d for d in due if self._retry_after.get(d[0].id, 0.0) <= mono]
        if not due:
            return 0, 0
        snaps = self._alpaca.snapshots([alpaca_symbol(t) for _, t, _ in due])
        observed = self._clock.now()
        recorded = missing = 0
        rows = []
        for sched, ticker, event_observed in due:
            snap = snaps.get(alpaca_symbol(ticker))
            if snap is None:
                missing += 1
                self._retry_after[sched.id] = mono + self.missing_retry_s
                log("quote_missing", ticker=ticker, kind=sched.kind, source_event_id=sched.source_event_id)
                continue
            rows.append(self._row(snap, sched.instrument_id, sched.kind, sched.source_event_id, observed))
            self._retry_after.pop(sched.id, None)
            recorded += 1
            fields = {"delay_vs_due_s": round((observed - sched.due_at).total_seconds(), 3)}
            if sched.kind == QuoteKind.observe:
                fields["observe_delay_s"] = round((observed - event_observed).total_seconds(), 3)
            log("quote_recorded", ticker=ticker, kind=sched.kind, source_event_id=sched.source_event_id,
                bid=snap.bid, ask=snap.ask, last=snap.last, quote_ts=snap.ts, **fields)
        if rows:
            with self._sf() as s, s.begin():
                s.add_all(rows)
        return recorded, missing

    # -- step 3 --
    def maybe_periodic(self, now: datetime) -> int:
        if not self._market_open(now):
            return 0
        slot = periodic_slot(now, self._periodic_every)
        if self._last_periodic_slot is not None and slot <= self._last_periodic_slot:
            return 0
        with self._sf() as s:
            insts = allowlist.current(s, new_york_date(now))
        if not insts:
            self._last_periodic_slot = slot
            return 0
        snaps = self._alpaca.snapshots([alpaca_symbol(i.ticker) for i in insts])
        observed = self._clock.now()
        rows = [self._row(snaps[alpaca_symbol(i.ticker)], i.id, QuoteKind.periodic, None, observed)
                for i in insts if alpaca_symbol(i.ticker) in snaps]
        with self._sf() as s, s.begin():
            s.add_all(rows)
        self._last_periodic_slot = slot
        log("periodic_recorded", slot=slot, instruments=len(insts), recorded=len(rows))
        return len(rows)

    @staticmethod
    def _row(snap: Snapshot, instrument_id: str, kind: QuoteKind, source_event_id: str | None,
             observed: datetime) -> Quote:
        return Quote(instrument_id=instrument_id, source_event_id=source_event_id, ts=snap.ts,
                     first_observed_at=observed, bid=snap.bid, ask=snap.ask, last=snap.last,
                     source=SOURCE, kind=kind)

    def tick(self) -> TickStats:
        stats = TickStats()
        stats.scheduled_events = self.schedule_new_events()
        now = self._clock.now()
        stats.recorded, stats.missing = self.execute_due(now)
        stats.periodic = self.maybe_periodic(now)
        with self._lock:
            self.ticks_ok += 1
            self.last_success_monotonic = self._clock.monotonic()
            self.last_success_at = self._clock.now()
        return stats

    def run(self, stop: StopFlag, tick_s: float = 0.5) -> None:
        while not stop.is_set():
            started = self._clock.monotonic()
            try:
                self.tick()
            except Exception as exc:
                with self._lock:
                    self.ticks_failed += 1
                log("quotes_tick_failed", error=f"{type(exc).__name__}: {exc}")
            stop.wait(max(0.0, tick_s - (self._clock.monotonic() - started)))

    def health(self) -> dict:
        with self._lock:
            since = (None if self.last_success_monotonic is None
                     else round(self._clock.monotonic() - self.last_success_monotonic, 3))
            return {"seconds_since_last_success": since,
                    "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                    "ticks_ok": self.ticks_ok, "ticks_failed": self.ticks_failed,
                    "alpaca_requests": self._alpaca.requests, "run_id": self.run_id}
