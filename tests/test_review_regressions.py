"""Regression tests for the issues an independent review of the money path found."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from nta.schema import Decision, Incident, KillState, KillSwitchEvent, Outcome, RiskReview, RiskVerdict
from tests import replay_scenario as sc
from tests.agent_fakes import MARKET_T, add_equity, add_filing, add_instrument, rig
from tests.broker_fakes import FakeBroker
from tests.test_replay import replay
from tests.test_risk import Crash, all_rows, close_for, events, filled_position, service, trade_decision


def closes(sf):
    return [d for d in all_rows(sf, Decision) if d.kind == "close"]


def test_flatten_does_not_write_a_close_every_tick(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.FLATTEN, actor="human", reason="t", at=risk.clock.now()))
    risk.quotes = lambda sym: None             # every close is rejected (quote_missing)
    for _ in range(10):
        risk.clock.advance(0.5)
        risk.tick()
    assert len(closes(session_factory)) == 1
    risk.clock.advance(60)
    risk.tick()
    assert len(closes(session_factory)) == 2   # retried after a minute
    risk.clock.advance(6 * 3600)               # after the close: nothing is written
    risk.tick()
    assert len(closes(session_factory)) == 2


def second_aapl_trade(session, sf, agent_clock_at):
    inst = session.scalars(select(__import__("nta.schema", fromlist=["Instrument"]).Instrument)).first()
    add_filing(session, inst, agent_clock_at - timedelta(seconds=3), accession="0000320193-26-000077",
               items=("1.01",), raw=sc.FILING_HTML.replace("2.02 Results of Operations and Financial Condition",
                                                          "1.01 Entry into a Material Definitive Agreement"))
    session.commit()


def test_risk_counts_approved_unfilled_entries_against_caps(session, session_factory) -> None:
    trade_decision(session, session_factory)
    # a second AAPL trade decision, written directly so the agent's own pending check is bypassed
    with session_factory() as s, s.begin():
        first = s.scalars(select(Decision)).one()
        cols = {c.name: getattr(first, c.name) for c in Decision.__table__.columns
                if c.name not in ("id", "created_at", "signal_id", "content_hash")}
        from nta.schema import Signal
        sig = s.get(Signal, first.signal_id)
        sig2 = Signal(**{c.name: getattr(sig, c.name) for c in Signal.__table__.columns
                         if c.name not in ("id", "created_at", "source_event_id")},
                      source_event_id=__import__("tests.test_reconcile", fromlist=["_another_event"])._another_event(s, first.source_event_id))
        s.add(sig2)
        s.flush()
        s.add(Decision(**{**cols, "signal_id": sig2.id, "source_event_id": sig2.source_event_id, "content_hash": "e" * 64}))
    broker = FakeBroker()
    service(session_factory, broker).tick()   # both reviewed in one tick, neither filled
    verdicts = sorted(r.verdict.value for r in all_rows(session_factory, RiskReview))
    assert verdicts == ["approve", "reject"]
    rejected = [r for r in all_rows(session_factory, RiskReview) if r.verdict == RiskVerdict.reject][0]
    assert "cap_hit" in rejected.reasons and len(broker.submissions) == 1


def test_agent_counts_its_own_pending_entries(session, session_factory) -> None:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    add_filing(session, inst, MARKET_T - timedelta(seconds=10))
    session.commit()
    agent = rig(session_factory)
    agent.pipeline.step(None)                 # trade, 49 shares, not filled
    agent.clock.advance(20)
    second_aapl_trade(session, session_factory, agent.clock.now())
    agent.pipeline.step(None)
    second = [d for d in all_rows(session_factory, Decision)][-1]
    assert second.outcome == Outcome.abstain and second.reasons == ["cap_hit"]
    assert second.exposure["positions"][0]["pending"] is True


def test_restart_after_accept_and_fill_is_not_a_mismatch(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()

    def crash(p):
        if p == "after_submit":
            raise Crash(p)
    with pytest.raises(Crash):
        service(session_factory, broker, fault=crash).tick()
    broker.fill(broker.only().client_order_id, "49", "200.20")   # filled while we were down
    service(session_factory, broker).tick()
    assert all_rows(session_factory, Incident) == []
    assert "found_on_lookup" in events(session_factory)


def test_fill_between_order_sync_and_positions_read_is_not_a_mismatch(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.tick()                                # order working, unfilled
    real = broker.positions
    state = {"n": 0}

    def positions():
        state["n"] += 1
        if state["n"] == 1:                    # the fill lands right after the sync
            broker.fill(broker.only().client_order_id, "49", "200.20")
        return real()
    broker.positions = positions
    risk.clock.advance(31)
    risk.tick()
    assert all_rows(session_factory, Incident) == []


def test_stale_approval_is_not_sent(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.reconcile_if_due()
    risk.review_pending()                      # approved and signed...
    risk.clock.advance(31)                     # ...then risk stalls
    risk.execute_pending()
    assert broker.submissions == [] and events(session_factory)[-1] == "blocked"


def test_two_simultaneous_closes_never_oversell(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    close_for(session_factory, d, 49, risk.clock.now())
    risk.tick()                                # first close working at the broker
    close_for(session_factory, d, 49, risk.clock.now())
    risk.tick()
    sells = [o for o in broker.orders.values() if o.side == "sell"]
    assert [o.qty for o in sells] == [Decimal(49)]
    last = all_rows(session_factory, RiskReview)[-1]
    assert last.verdict == RiskVerdict.reject and "no_position" in last.reasons


def test_live_quote_missing_replays_identically(connection, session, session_factory) -> None:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    add_filing(session, inst, MARKET_T - timedelta(seconds=10))
    session.commit()
    agent = rig(session_factory)
    agent.alpaca.default = (None, None, None)  # Alpaca returns nothing for the symbol
    agent.pipeline.step(None)
    [d] = all_rows(session_factory, Decision)
    assert d.reasons == ["quote_missing"] and d.decided_at == MARKET_T - timedelta(seconds=10)
    # a periodic quote exists that a fallback would have used; recorded replay must not
    from nta.schema import Quote, QuoteKind
    with session_factory() as s, s.begin():
        s.add(Quote(instrument_id=inst.id, ts=MARKET_T, first_observed_at=MARKET_T, last=Decimal(200),
                    source="alpaca_iex", kind=QuoteKind.periodic))
    r = replay(connection, session_factory, schema="replay_qm01")
    assert r.comparison.identical and r.comparison.matched == 1
