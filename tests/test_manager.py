from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from nta.agent import PositionManager
from nta.decide import close_trigger
from nta.schema import (
    Decision,
    KillState,
    KillSwitchEvent,
    Outcome,
    Quote,
    QuoteKind,
    RiskReview,
    RiskVerdict,
)
from tests.agent_fakes import MARKET_T, add_equity, add_filing, add_instrument, rig
from tests.broker_fakes import FakeBroker
from tests.test_risk import all_rows, service

PLAN = {"stop": "198.00", "take_profit": "204.00", "expiry": "2026-11-06T16:00:00+00:00"}


@pytest.mark.parametrize("price,now,expected", [
    ("199", MARKET_T, None),
    ("198.00", MARKET_T, "stop"),
    ("150", MARKET_T, "stop"),
    ("204.00", MARKET_T, "take_profit"),
    ("199", datetime(2026, 11, 6, 16, 0, tzinfo=UTC), "expiry"),
    (None, MARKET_T, None),
])
def test_close_trigger(price, now, expected) -> None:
    assert close_trigger(PLAN, Decimal(price) if price else None, now) == expected


def open_position(session, sf):
    """Agent decides a trade at MARKET_T; risk fills it 49 @ 200.20 a couple of seconds later."""
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    add_filing(session, inst, MARKET_T - timedelta(seconds=10))
    session.commit()
    agent = rig(sf)
    agent.pipeline.step(None)
    broker = FakeBroker()
    risk = service(sf, broker)
    risk.tick()
    broker.fill(broker.only().client_order_id, "49", "200.20")
    risk.tick()
    agent.clock.advance(10)
    risk.clock.advance(8)
    return agent, risk, broker, PositionManager(agent.pipeline), inst


def closes(sf):
    return [d for d in all_rows(sf, Decision) if d.kind == "close"]


def test_stop_fires_once(session, session_factory) -> None:
    agent, _, _, mgr, _ = open_position(session, session_factory)
    assert mgr.step() == 0                      # 200 is between stop and take-profit
    agent.alpaca.default = (None, None, "197.99")
    agent.clock.advance(5)
    assert mgr.step() == 1
    [c] = closes(session_factory)
    assert c.outcome == Outcome.trade and c.reasons == ["stop"] and c.side == "sell" and c.qty == 49
    assert c.plan["exit_ref"] == "197.99" and c.decided_at == agent.clock.now()
    manage = [q for q in all_rows(session_factory, Quote) if q.kind == QuoteKind.manage]
    assert manage and manage[-1].source_event_id == c.source_event_id
    agent.clock.advance(5)
    assert mgr.step() == 0                      # pending close: not repeated


def test_take_profit_and_expiry(session, session_factory) -> None:
    agent, _, _, mgr, _ = open_position(session, session_factory)
    agent.alpaca.default = (None, None, "204.01")
    assert mgr.step() == 1 and closes(session_factory)[0].reasons == ["take_profit"]


def test_expiry_fires_in_session_after_the_horizon(session, session_factory) -> None:
    agent, _, _, mgr, _ = open_position(session, session_factory)
    agent.clock.advance((datetime(2026, 11, 6, 16, 0, 5, tzinfo=UTC) - agent.clock.now()).total_seconds())
    assert mgr.step() == 1 and closes(session_factory)[0].reasons == ["expiry"]


def test_no_checks_outside_market_hours_or_in_halt(session, session_factory) -> None:
    agent, risk, _, mgr, _ = open_position(session, session_factory)
    agent.alpaca.default = (None, None, "150")
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.HALT, actor="human", reason="t", at=agent.clock.now()))
    requests = agent.alpaca.requests
    assert mgr.step() == 0 and agent.alpaca.requests == requests
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.RUN, actor="human", reason="t", at=agent.clock.now()))
    agent.clock.advance(8 * 3600)               # 19:00 New York
    assert mgr.step() == 0 and agent.alpaca.requests == requests


def test_rejected_close_is_retried_after_a_minute(session, session_factory) -> None:
    agent, risk, broker, mgr, _ = open_position(session, session_factory)
    agent.alpaca.default = (None, None, "197")
    mgr.step()
    risk.quotes = lambda sym: None              # risk cannot price the exit: rejects
    risk.tick()
    [rev] = [r for r in all_rows(session_factory, RiskReview) if r.verdict == RiskVerdict.reject]
    assert "quote_missing" in rev.reasons
    agent.clock.advance(30)
    assert mgr.step() == 0
    agent.clock.advance(31)
    assert mgr.step() == 1 and len(closes(session_factory)) == 2


def test_mismatch_pauses_entries_but_the_stop_loss_still_fires(session, session_factory) -> None:
    agent, risk, broker, mgr, inst = open_position(session, session_factory)
    # 1. someone trades account T by hand -> one reconcile cycle -> PAUSE
    broker.extra_positions = {"MSFT": Decimal(5)}
    risk.clock.advance(31)
    risk.tick()
    with session_factory() as s:
        assert s.scalars(select(KillSwitchEvent)).one().state == KillState.PAUSE
    # 2. a new filing arrives after the PAUSE: the agent does not trade
    agent.clock.advance((risk.clock.now() - agent.clock.now()).total_seconds() + 5)
    with session_factory() as s, s.begin():
        s.add(_filing_row(inst, agent.clock.now() - timedelta(seconds=5)))
    agent.pipeline.step(None)
    new_entry = [d for d in all_rows(session_factory, Decision) if d.kind == "entry"][-1]
    assert new_entry.outcome != Outcome.trade
    assert "kill_switch" in new_entry.reasons
    # 3. the existing position's stop is hit: close decision -> risk -> sell order at the broker
    agent.alpaca.default = (None, None, "197.50")
    agent.clock.advance(5)
    assert mgr.step() == 1
    risk.quotes = lambda sym, c=risk.clock: _q("197.50", c)
    risk.tick()
    sells = [o for o in broker.orders.values() if o.side == "sell"]
    assert len(sells) == 1 and sells[0].qty == 49
    buys = [o for o in broker.orders.values() if o.side == "buy"]
    assert len(buys) == 1                       # no new entry was sent


def _filing_row(inst, observed):
    from nta.schema import SourceEvent
    from tests.agent_fakes import FILING_HTML
    return SourceEvent(source_id="edgar", external_id="0000320193-26-000099", form_type="8-K", cik=inst.cik,
                       instrument_id=inst.id, published_at=observed, first_observed_at=observed, ingested_at=observed,
                       url="https://www.sec.gov/x", items=["1.01"],
                       raw=FILING_HTML.replace("2.02 Results of Operations", "1.01 Entry into an Agreement"),
                       raw_hash="0" * 64)


def _q(last, clock):
    from nta.decide import QuoteView
    return QuoteView(None, None, Decimal(last), clock.now() - timedelta(milliseconds=200), clock.now(), "alpaca_iex_t")
