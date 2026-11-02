from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from nta.alpaca import AlpacaData, parse_snapshots
from nta.config import ConfigError, alpaca_credentials
from nta.quotes import QuoteRecorder
from nta.ratelimit import alpaca_bucket
from nta.schema import Instrument, Quote, QuoteKind, QuoteSchedule, SourceEvent
from tests.fakes import FakeClock

THU_10AM_ET = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)  # market open (EDT)


class FakeAlpaca:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.prices = {"AAPL": (Decimal("100.00"), Decimal("100.10"), Decimal("100.05")),
                       "MSFT": (Decimal("400.00"), Decimal("400.40"), Decimal("400.20"))}
        self.omit: set[str] = set()
        self.requests: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "data.alpaca.markets"
        assert request.url.path == "/v2/stocks/snapshots"
        assert request.headers["APCA-API-KEY-ID"] == "kid"
        assert request.headers["APCA-API-SECRET-KEY"] == "sec"
        assert request.url.params["feed"] == "iex"
        symbols = request.url.params["symbols"].split(",")
        self.requests.append(symbols)
        t = self.clock.now().isoformat().replace("+00:00", "Z")
        body = {}
        for sym in symbols:
            if sym in self.prices and sym not in self.omit:
                bid, ask, last = self.prices[sym]
                body[sym] = {"latestQuote": {"t": t, "bp": float(bid), "ap": float(ask)},
                             "latestTrade": {"t": t, "p": float(last)}}
        return httpx.Response(200, json=body)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=THU_10AM_ET)


@pytest.fixture
def alpaca(clock: FakeClock) -> FakeAlpaca:
    return FakeAlpaca(clock)


@pytest.fixture
def insts(session: Session) -> dict[str, Instrument]:
    rows = {t: Instrument(ticker=t, figi=f"BBG00000{t:0<4}", cik=cik, name=t, added_on=date(2026, 1, 1))
            for t, cik in (("AAPL", "0000320193"), ("MSFT", "0000789019"))}
    session.add_all(rows.values())
    session.commit()
    return rows


def recorder(alpaca: FakeAlpaca, clock: FakeClock, sf: sessionmaker[Session]) -> QuoteRecorder:
    http = httpx.Client(transport=httpx.MockTransport(alpaca.handler))
    return QuoteRecorder(session_factory=sf, alpaca=AlpacaData(http, alpaca_bucket(clock), clock, "kid", "sec"),
                         clock=clock)


def add_event(session: Session, inst: Instrument, observed: datetime, n: int = 1) -> SourceEvent:
    ev = SourceEvent(source_id="edgar", external_id=f"0000000000-26-{n:06d}", form_type="8-K",
                     cik=inst.cik, instrument_id=inst.id, published_at=observed - timedelta(seconds=30),
                     first_observed_at=observed, ingested_at=observed, url="https://www.sec.gov/x")
    session.add(ev)
    session.commit()
    return ev


def quotes(session: Session, kind: QuoteKind | None = None) -> list[Quote]:
    session.expire_all()
    q = select(Quote).order_by(Quote.id)
    if kind:
        q = q.where(Quote.kind == kind)
    return list(session.scalars(q))


def test_observe_within_two_seconds_during_market_hours(alpaca, clock, session_factory, session, insts):
    ev = add_event(session, insts["AAPL"], clock.now())
    clock.advance(0.4)  # recorder notices on its next tick
    recorder(alpaca, clock, session_factory).tick()
    (obs,) = quotes(session, QuoteKind.observe)
    assert obs.source_event_id == ev.id
    assert (obs.bid, obs.ask, obs.last) == (Decimal("100.00"), Decimal("100.10"), Decimal("100.05"))
    assert (obs.first_observed_at - ev.first_observed_at).total_seconds() <= 2.0
    kinds = set(session.scalars(select(QuoteSchedule.kind).where(QuoteSchedule.source_event_id == ev.id)))
    assert kinds == {QuoteKind.observe, QuoteKind.mark_1h, QuoteKind.mark_1d, QuoteKind.mark_5d}


def test_marks_recorded_when_due_and_only_once(alpaca, clock, session_factory, session, insts):
    add_event(session, insts["AAPL"], clock.now())
    rec = recorder(alpaca, clock, session_factory)
    rec.tick()
    clock.advance(3600)
    rec.tick()
    rec.tick()
    assert [q.kind for q in quotes(session) if q.source_event_id] == [QuoteKind.observe, QuoteKind.mark_1h]
    clock.advance(23 * 3600)  # Friday 10:00 ET = next session, same time
    rec.tick()
    assert QuoteKind.mark_1d in {q.kind for q in quotes(session)}


def test_after_hours_filing_waits_for_next_open(alpaca, clock, session_factory, session, insts):
    clock.advance(7.5 * 3600)  # Thursday 17:30 ET
    add_event(session, insts["AAPL"], clock.now())
    rec = recorder(alpaca, clock, session_factory)
    rec.tick()
    assert quotes(session) == [] and alpaca.requests == []
    clock.advance(16 * 3600)  # Friday 09:30 ET
    rec.tick()
    assert [q.kind for q in quotes(session) if q.source_event_id] == [QuoteKind.observe]


def test_schedule_survives_restart(alpaca, clock, session_factory, session, insts):
    add_event(session, insts["AAPL"], clock.now())
    recorder(alpaca, clock, session_factory).tick()
    clock.advance(3600)
    recorder(alpaca, clock, session_factory).tick()  # fresh process, schedule from DB
    assert QuoteKind.mark_1h in {q.kind for q in quotes(session)}
    assert session.scalar(select(func.count()).select_from(QuoteSchedule)) == 4


def test_periodic_once_per_slot_and_across_restart(alpaca, clock, session_factory, session, insts):
    rec = recorder(alpaca, clock, session_factory)
    rec.tick()
    assert len(quotes(session, QuoteKind.periodic)) == 2
    clock.advance(60)
    rec.tick()
    recorder(alpaca, clock, session_factory).tick()  # restart inside the same 5-minute slot
    assert len(quotes(session, QuoteKind.periodic)) == 2
    clock.advance(240)  # 14:05 -> next slot
    rec.tick()
    assert len(quotes(session, QuoteKind.periodic)) == 4


def test_missing_symbol_is_retried_after_a_delay(alpaca, clock, session_factory, session, insts):
    alpaca.omit = {"AAPL"}
    add_event(session, insts["AAPL"], clock.now())
    rec = recorder(alpaca, clock, session_factory)
    rec.tick()
    n = len(alpaca.requests)
    clock.advance(1)
    rec.tick()
    assert len(alpaca.requests) == n  # waiting out the retry delay
    alpaca.omit = set()
    clock.advance(5)
    rec.tick()
    assert len(quotes(session, QuoteKind.observe)) == 1


def test_no_requests_while_market_closed(alpaca, clock, session_factory, session, insts):
    clock.advance(2 * 86400)  # Saturday
    add_event(session, insts["AAPL"], clock.now())
    recorder(alpaca, clock, session_factory).tick()
    assert alpaca.requests == []


def test_parse_snapshots() -> None:
    payload = {
        "AAPL": {"latestQuote": {"t": "2026-10-01T14:00:00.123456789Z", "bp": 100.0, "ap": 100.1},
                 "latestTrade": {"t": "2026-10-01T14:00:01Z", "p": 100.05}},
        "NOBID": {"latestQuote": {"t": "2026-10-01T14:00:00Z", "bp": 0, "ap": 5.0},
                  "latestTrade": {"t": "2026-10-01T13:59:00Z", "p": 4.9}},
        "CROSSED": {"latestQuote": {"t": "2026-10-01T14:00:00Z", "bp": 10, "ap": 9},
                    "latestTrade": {"t": "2026-10-01T14:00:00Z", "p": 9.5}},
        "EMPTY": {},
    }
    s = parse_snapshots(payload)
    assert s["AAPL"].ts == datetime(2026, 10, 1, 14, 0, 1, tzinfo=UTC)
    assert s["AAPL"].bid == Decimal("100.0")
    assert (s["NOBID"].bid, s["NOBID"].ask) == (None, Decimal("5.0"))
    assert (s["CROSSED"].bid, s["CROSSED"].ask, s["CROSSED"].last) == (None, None, Decimal("9.5"))
    assert "EMPTY" not in s


@pytest.mark.parametrize("url", ["https://paper-api.alpaca.markets", "https://api.alpaca.markets",
                                 "http://data.alpaca.markets"])
def test_trading_or_insecure_endpoints_are_refused(url: str) -> None:
    clock = FakeClock()
    with pytest.raises(ConfigError, match="data API only"):
        AlpacaData(httpx.Client(), alpaca_bucket(clock), clock, "k", "s", base_url=url)


def test_credentials_required() -> None:
    with pytest.raises(ConfigError):
        alpaca_credentials({"ALPACA_KEY_ID": "replace-me", "ALPACA_SECRET": "x"})
    from nta.cli import app
    result = CliRunner().invoke(app, ["quotes", "--once"], env={"ALPACA_KEY_ID": "", "ALPACA_SECRET": ""})
    assert result.exit_code == 2 and "ALPACA_KEY_ID" in result.output


def test_snapshot_chunks_are_fetched_in_parallel() -> None:
    import threading
    clock = FakeClock(start=THU_10AM_ET)
    barrier = threading.Barrier(3, timeout=5)  # only passes if 3 requests are in flight together
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbols = request.url.params["symbols"].split(",")
        seen.append(len(symbols))
        barrier.wait()
        t = clock.now().isoformat().replace("+00:00", "Z")
        return httpx.Response(200, json={s: {"latestTrade": {"t": t, "p": 1.0}} for s in symbols})

    a = AlpacaData(httpx.Client(transport=httpx.MockTransport(handler)), alpaca_bucket(clock), clock,
                   "kid", "sec")
    out = a.snapshots([f"S{i}" for i in range(250)])
    assert len(out) == 250
    assert sorted(seen) == [50, 100, 100]
    assert a.requests == 3
