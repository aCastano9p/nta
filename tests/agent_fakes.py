"""Shared builders for agent / risk / replay tests."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from nta.agent import LiveQuotes, Pipeline
from nta.alpaca import Snapshot
from nta.extract import LlmGateway, ModelConfig
from nta.policy import load_policy
from nta.schema import Instrument, PositionSnapshot, SourceEvent
from tests.fakes import FakeClock
from tests.llm_fakes import FakeBackend

# Wednesday 2026-11-04 11:00 New York
MARKET_T = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
FILING_HTML = ("<html><body><p>Item 2.02 Results of Operations and Financial Condition.</p>"
               "<p>Apple Inc. raised its full-year revenue guidance to $420 billion.</p></body></html>")


class StubAlpaca:
    """Stands in for AlpacaData: a settable price per symbol."""

    source = "alpaca_iex"

    def __init__(self, clock: FakeClock, last: str = "200", bid: str | None = None, ask: str | None = None) -> None:
        self.clock, self.requests = clock, 0
        self.prices: dict[str, tuple[str | None, str | None, str | None]] = {}
        self.default = (bid, ask, last)

    def snapshots(self, symbols):
        self.requests += 1
        out = {}
        for sym in symbols:
            bid, ask, last = self.prices.get(sym, self.default)
            if last is None and bid is None:
                continue
            d = lambda v: Decimal(v) if v is not None else None  # noqa: E731
            out[sym] = Snapshot(sym, self.clock.now() - timedelta(milliseconds=500), d(bid), d(ask), d(last))
        return out


def add_instrument(s, ticker="AAPL", cik="0000320193", name="Apple Inc.") -> Instrument:
    inst = Instrument(ticker=ticker, figi=f"BBG{ticker:0>9}"[:12], cik=cik, name=name, aliases=[],
                      added_on=date(2026, 10, 1))
    s.add(inst)
    s.flush()
    return inst


def add_filing(s, inst: Instrument, observed: datetime, accession="0000320193-26-000011", raw=FILING_HTML,
               form="8-K", items=("2.02",)) -> SourceEvent:
    ev = SourceEvent(source_id="edgar", external_id=accession, form_type=form, cik=inst.cik, instrument_id=inst.id,
                     published_at=observed - timedelta(seconds=30), first_observed_at=observed, ingested_at=observed,
                     url=f"https://www.sec.gov/Archives/edgar/data/320193/{accession.replace('-', '')}/",
                     items=list(items), raw=raw, raw_hash="0" * 64)
    s.add(ev)
    s.flush()
    return ev


def add_equity(s, taken_at: datetime, equity="100000") -> None:
    s.add(PositionSnapshot(account="T", taken_at=taken_at, equity=Decimal(equity), positions={}, mismatch=False))
    s.flush()


@dataclass
class Rig:
    clock: FakeClock
    backend: FakeBackend
    alpaca: StubAlpaca
    pipeline: Pipeline


def rig(sf, *, start: datetime = MARKET_T, backend: FakeBackend | None = None, daily="5", replay=False) -> Rig:
    clock = FakeClock(start)
    backend = backend or FakeBackend()
    alpaca = StubAlpaca(clock)
    gw = LlmGateway(sf, clock, backend=None if replay else backend, daily_usd=Decimal(daily), replay=replay)
    pipe = Pipeline(sf=sf, clock=clock, gateway=gw, models=ModelConfig(), policy=load_policy(),
                    quotes=LiveQuotes(sf, alpaca, clock), code_sha="test-sha")
    return Rig(clock, backend, alpaca, pipe)
