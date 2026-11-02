"""A small live day, run through the real agent, risk service, fake broker and position
manager, used as the golden log for replay."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from nta.agent import PositionManager
from nta.decide import QuoteView
from nta.schema import (
    Base,
    Quote,
    QuoteKind,
    SourceEvent,
)
from tests.agent_fakes import MARKET_T, FILING_HTML, add_equity, add_instrument, rig
from tests.broker_fakes import FakeBroker
from tests.llm_fakes import DULL_TRIAGE, GOOD_EXTRACTION, GOOD_TRIAGE, FakeBackend, message
from tests.test_risk import service

GOLDEN = Path(__file__).parent / "fixtures" / "replay" / "golden.json"
DAY = date(2026, 11, 4)
TABLES = ["instruments", "source_events", "quotes", "llm_calls", "signals", "decisions", "risk_reviews", "orders",
          "order_events", "fills", "position_snapshots", "kill_switch_events", "incidents"]


def answer(q: dict[str, Any]) -> dict[str, Any]:
    content = q["messages"][0]["content"]
    if "screen" in q["system"]:
        return message(DULL_TRIAGE if "5.07" in content else GOOD_TRIAGE, model=q["model"])
    if "ticker: MSFT" in content and "8.01" in content:
        return message("not json at all", model=q["model"])
    if "ticker: MSFT" in content:
        return message({**GOOD_EXTRACTION, "direction": "down", "p_up_at_horizon": 0.3}, model=q["model"])
    return message(GOOD_EXTRACTION, model=q["model"])


def _filing(s: Session, inst, observed: datetime, accession: str, items: list[str], form: str = "8-K") -> SourceEvent:
    body = FILING_HTML.replace("2.02 Results of Operations and Financial Condition", f"{items[0]} disclosure")
    body = body.replace("Apple Inc.", inst.name)
    ev = SourceEvent(source_id="edgar", external_id=accession, form_type=form, cik=inst.cik, instrument_id=inst.id,
                     published_at=observed - timedelta(seconds=20), first_observed_at=observed, ingested_at=observed,
                     url=f"https://www.sec.gov/Archives/edgar/data/1/{accession.replace('-', '')}/", items=items,
                     raw=body, raw_hash="0" * 64)
    s.add(ev)
    s.commit()
    return ev


def run_live_scenario(session: Session, sf: sessionmaker[Session]) -> None:
    aapl = add_instrument(session)
    msft = add_instrument(session, "MSFT", "0000789019", "Microsoft Corp")
    ko = add_instrument(session, "KO", "0000021344", "Coca-Cola Co")
    add_equity(session, MARKET_T - timedelta(hours=1))
    for i in range(20):  # 5-minute history for ATR and vol
        t = MARKET_T - timedelta(minutes=5 * (20 - i))
        session.add(Quote(instrument_id=aapl.id, ts=t, first_observed_at=t, last=Decimal("199") + Decimal(i % 3) / 2,
                          source="alpaca_iex", kind=QuoteKind.periodic))
    session.commit()

    agent = rig(sf, backend=FakeBackend(answer=answer))
    broker = FakeBroker()
    risk = service(sf, broker)
    mgr = PositionManager(agent.pipeline)

    def at(t: datetime) -> None:
        agent.clock.advance((t - agent.clock.now()).total_seconds())
        risk.clock.advance(max(0.0, (t + timedelta(seconds=2) - risk.clock.now()).total_seconds()))

    # E1 AAPL results, in session -> trade, filled
    _filing(session, aapl, MARKET_T - timedelta(seconds=10), "0000320193-26-000011", ["2.02"])
    agent.pipeline.step(None)
    risk.tick()
    broker.fill(broker.only().client_order_id, "49", "200.20")
    at(MARKET_T + timedelta(seconds=30))
    risk.tick()
    # E2 MSFT, model says down -> long_only with a hypothetical short
    at(MARKET_T + timedelta(minutes=1))
    _filing(session, msft, agent.clock.now() - timedelta(seconds=3), "0000789019-26-000021", ["2.02"])
    agent.pipeline.step(None)
    # manage checks: no trigger at 199.10, stop at 197.50
    agent.alpaca.default = (None, None, "199.10")
    at(MARKET_T + timedelta(minutes=5))
    mgr.step(force=True)
    agent.alpaca.default = (None, None, "197.50")
    at(MARKET_T + timedelta(minutes=10))
    mgr.step(force=True)
    risk.quotes = lambda sym, c=risk.clock: QuoteView(None, None, Decimal("197.50"), c.now() - timedelta(milliseconds=200),
                                                       c.now(), "alpaca_iex_t")
    risk.tick()
    sell = [o for o in broker.orders.values() if o.side == "sell"][0]
    broker.fill(sell.client_order_id, "49", "197.40")
    at(MARKET_T + timedelta(minutes=11))
    risk.tick()
    # E3 AAPL 8-K/A of the same items -> repeat
    agent.alpaca.default = (None, None, "198.00")
    at(MARKET_T + timedelta(minutes=30))
    _filing(session, aapl, agent.clock.now() - timedelta(seconds=4), "0000320193-26-000012", ["2.02"], form="8-K/A")
    agent.pipeline.step(None)
    # E4 KO shareholder vote -> not material
    at(MARKET_T + timedelta(minutes=40))
    _filing(session, ko, agent.clock.now() - timedelta(seconds=4), "0000021344-26-000031", ["5.07"])
    agent.pipeline.step(None)
    # E5 MSFT other events, model answers garbage -> llm_invalid
    at(MARKET_T + timedelta(minutes=50))
    _filing(session, msft, agent.clock.now() - timedelta(seconds=4), "0000789019-26-000022", ["8.01"])
    agent.pipeline.step(None)
    # E6 AAPL Reg FD after the close -> wait (market_closed)
    at(MARKET_T + timedelta(hours=5, minutes=30))
    _filing(session, aapl, agent.clock.now() - timedelta(seconds=4), "0000320193-26-000013", ["7.01"])
    agent.pipeline.step(None)


def _json(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if hasattr(v, "value"):
        return v.value
    return v


def dump(sf: sessionmaker[Session], path: Path = GOLDEN) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with sf() as s:
        for name in TABLES:
            t = Base.metadata.tables[name]
            out[name] = [{c: _json(v) for c, v in row._mapping.items()} for row in s.execute(select(t).order_by(t.c.id))]
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


def load(sf: sessionmaker[Session], path: Path = GOLDEN) -> dict[str, list[dict]]:
    data = json.loads(path.read_text())
    with sf() as s, s.begin():
        for name in TABLES:
            if data[name]:
                s.execute(insert(Base.metadata.tables[name]), data[name])
    return data
