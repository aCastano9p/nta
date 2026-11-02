import subprocess
import sys
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from typer.testing import CliRunner

from nta.broker import client_order_id, sign_order, verify_order
from nta.cli import app
from nta.decide import DecisionContent, QuoteView, Versions, load_kill_state, to_row
from nta.policy import load_policy
from nta.risk import RiskService
from nta.schema import (
    Decision,
    Fill,
    Incident,
    KillState,
    KillSwitchEvent,
    Order,
    OrderEvent,
    Outcome,
    RiskReview,
    RiskVerdict,
    Side,
)
from tests.agent_fakes import MARKET_T, add_equity, add_filing, add_instrument, rig
from tests.broker_fakes import FakeBroker
from tests.fakes import FakeClock

KEY = "k" * 40


class Crash(BaseException):
    """Stands in for the process being killed at a given point."""


def quotes_at(clock, last="200", bid=None, ask=None):
    def fetch(symbol):
        d = lambda v: Decimal(v) if v is not None else None  # noqa: E731
        return QuoteView(d(bid), d(ask), d(last), clock.now() - timedelta(milliseconds=300), clock.now(), "alpaca_iex_t")
    return fetch


def service(sf, broker, clock=None, *, fault=None, policy=None, last="200"):
    clock = clock or FakeClock(MARKET_T + timedelta(seconds=2))
    return RiskService(sf=sf, clock=clock, broker=broker, quotes=quotes_at(clock, last), policy=policy or load_policy(),
                       signing_key=KEY, fault=fault, code_sha="test")


def trade_decision(session, sf) -> Decision:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    add_filing(session, inst, MARKET_T - timedelta(seconds=10))
    session.commit()
    rig(sf).pipeline.step(None)
    with sf() as s:
        d = s.scalars(select(Decision)).one()
    assert d.outcome == Outcome.trade and d.qty == 49
    return d


def all_rows(sf, model):
    with sf() as s:
        return list(s.scalars(select(model).order_by(model.id)))


def events(sf):
    return [e.event for e in all_rows(sf, OrderEvent)]


def test_approve_sign_submit(session, session_factory) -> None:
    d = trade_decision(session, session_factory)
    broker = FakeBroker()
    service(session_factory, broker).tick()
    [rev] = all_rows(session_factory, RiskReview)
    assert rev.verdict == RiskVerdict.approve and rev.reasons == [] and rev.quote["source"] == "alpaca_iex_t"
    [o] = all_rows(session_factory, Order)
    assert o.client_order_id == client_order_id(d.id, 1) == f"nta-{d.id}-1"
    assert (o.side, o.qty, o.limit_price) == (Side.buy, 49, Decimal("200.2000"))
    assert verify_order(KEY, d.id, "buy", 49, o.limit_price, o.hmac)
    assert broker.submissions == [o.client_order_id]
    assert events(session_factory) == ["submit_attempt", "submitted"]
    assert o.created_at >= d.created_at


def test_partial_fills_are_recorded_once(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.tick()
    cid = broker.only().client_order_id
    broker.fill(cid, "20", "200.10")
    risk.tick()
    risk.tick()  # no change: no duplicate fill
    broker.fill(cid, "29", "200.20")
    risk.tick()
    fills = all_rows(session_factory, Fill)
    assert [(f.qty, f.price, f.cum_qty) for f in fills] == [
        (Decimal(20), Decimal("200.100000"), Decimal(20)), (Decimal(29), Decimal("200.200000"), Decimal(49))]
    assert events(session_factory)[-2:] == ["status", "terminal"]
    risk.tick()
    assert len(all_rows(session_factory, Fill)) == 2


def test_order_without_valid_hmac_is_never_sent(session, session_factory) -> None:
    d = trade_decision(session, session_factory)
    with session_factory() as s, s.begin():  # injected straight into the table, bypassing review
        s.add(Order(decision_id=d.id, leg=1, client_order_id=client_order_id(d.id, 1), symbol="AAPL", side=Side.buy,
                    qty=1000, limit_price=Decimal("200.20"), hmac=sign_order("wrong-key" * 5, d.id, "buy", 1000,
                                                                              Decimal("200.20")),
                    policy_version=d.policy_version))
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.execute_pending()
    risk.execute_pending()
    assert broker.attempts == 0 and broker.lookups == 0
    assert events(session_factory) == ["hmac_invalid"]
    [inc] = all_rows(session_factory, Incident)
    assert inc.severity == 1 and inc.kind == "hmac_invalid"
    with session_factory() as s:
        assert load_kill_state(s, risk.clock.now()) == KillState.PAUSE


def test_timeout_after_accept_is_found_by_lookup_not_resent(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    broker.timeout_after_accept = True
    risk = service(session_factory, broker)
    risk.tick()
    risk.tick()
    assert broker.attempts == 1 and len(broker.submissions) == 1
    assert events(session_factory)[:2] == ["submit_attempt", "found_on_lookup"]


def test_timeout_before_accept_retries_after_lookup(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    broker.timeout_before_accept = True
    risk = service(session_factory, broker)
    risk.tick()
    assert events(session_factory) == ["submit_attempt", "submit_timeout"] and broker.submissions == []
    risk.tick()
    assert len(broker.submissions) == 1 and "submitted" in events(session_factory)


@pytest.mark.parametrize("point", ["after_order_row", "after_submit"])
def test_restart_produces_exactly_one_order(session, session_factory, point) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()

    def crash(p):
        if p == point:
            raise Crash(p)
    with pytest.raises(Crash):
        service(session_factory, broker, fault=crash).tick()
    assert len(all_rows(session_factory, Order)) == 1
    assert len(broker.submissions) == (1 if point == "after_submit" else 0)
    restarted = service(session_factory, broker)   # a fresh process
    restarted.tick()
    restarted.tick()
    assert len(broker.submissions) == 1 and broker.attempts == 1
    assert len(all_rows(session_factory, Order)) == 1
    final = [e for e in events(session_factory) if e in ("submitted", "found_on_lookup")]
    assert final == (["found_on_lookup"] if point == "after_submit" else ["submitted"])


def test_broker_reject_is_final(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    broker.reject_next = (403, '{"message":"insufficient buying power"}')
    risk = service(session_factory, broker)
    risk.tick()
    risk.tick()
    assert broker.attempts == 1 and events(session_factory) == ["submit_attempt", "broker_rejected"]


@pytest.mark.parametrize("setup,reason", [
    ("policy", "policy_mismatch"),
    ("stale", "decision_stale"),
    ("pause", "kill_switch"),
    ("moved", "price_above_limit"),
    ("full", "max_open_positions"),
    ("loss", "daily_loss_stop"),
])
def test_risk_rejects(session, session_factory, setup, reason) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    clock = FakeClock(MARKET_T + timedelta(seconds=2))
    kw = {}
    if setup == "policy":
        from nta.policy import DEFAULT_POLICY_PATH, parse_policy
        kw["policy"] = parse_policy(DEFAULT_POLICY_PATH.read_text() + "\n# edited\n")
    if setup == "stale":
        clock.advance(60)
    if setup == "pause":
        with session_factory() as s, s.begin():
            s.add(KillSwitchEvent(state=KillState.PAUSE, actor="human", reason="t", at=MARKET_T))
    if setup == "moved":
        kw["last"] = "200.50"
    if setup == "full":
        broker.extra_positions = {f"S{i}": Decimal(1) for i in range(5)}
    if setup == "loss":
        broker.equity = Decimal("98900")
    service(session_factory, broker, clock, **kw).tick()
    [rev] = all_rows(session_factory, RiskReview)
    assert rev.verdict == RiskVerdict.reject and reason in rev.reasons
    assert all_rows(session_factory, Order) == [] and broker.attempts == 0


def close_for(sf, entry: Decision, qty: int, when) -> Decision:
    c = DecisionContent(kind="close", source_event_id=entry.source_event_id, instrument_id=entry.instrument_id,
                        symbol=entry.symbol, cluster_id=entry.cluster_id, outcome=Outcome.trade, reasons=("stop",),
                        side=Side.sell, qty=qty, quote=None, exposure={}, plan={"trigger": "stop"},
                        expected_cost_bps=None, spread_unverified=True, hypothetical=None, decided_at=when,
                        versions=Versions("x", entry.policy_version, "v", "m", None),
                        parent_source_event_id=entry.source_event_id)
    row = to_row(c, parent_decision_id=entry.id)
    with sf() as s, s.begin():
        s.add(row)
    return row


def filled_position(session, session_factory):
    d = trade_decision(session, session_factory)
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.tick()
    broker.fill(broker.only().client_order_id, "49", "200.20")
    risk.tick()
    return d, broker, risk


def test_close_goes_through_in_pause_but_not_in_halt(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.PAUSE, actor="reconciler", reason="t", at=risk.clock.now()))
    close_for(session_factory, d, 49, risk.clock.now())
    risk.tick()
    sells = [o for o in broker.orders.values() if o.side == "sell"]
    assert len(sells) == 1 and sells[0].qty == 49 and sells[0].limit_price == Decimal("199.00")  # 200 x (1 - 50 bps)
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.HALT, actor="human", reason="t", at=risk.clock.now()))
    close_for(session_factory, d, 49, risk.clock.now())
    risk.tick()
    assert broker.cancels == [sells[0].id]                        # HALT cancels working orders
    assert len([o for o in broker.orders.values() if o.side == "sell"]) == 1


def test_close_never_sells_more_than_held(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    close_for(session_factory, d, 500, risk.clock.now())
    risk.tick()
    sells = [o for o in broker.orders.values() if o.side == "sell"]
    assert sells[0].qty == 49


def test_flatten_closes_every_position(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.FLATTEN, actor="human", reason="t", at=risk.clock.now()))
    risk.tick()
    risk.tick()
    closes = [x for x in all_rows(session_factory, Decision) if x.kind == "close"]
    assert len(closes) == 1 and closes[0].reasons == ["flatten"] and closes[0].parent_decision_id == d.id
    assert [o.qty for o in broker.orders.values() if o.side == "sell"] == [Decimal(49)]


def test_unfilled_entry_is_cancelled_after_ttl(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.tick()
    risk.clock.advance(61)
    risk.tick()
    assert broker.cancels == ["B1"] and "cancel_requested" in events(session_factory)
    risk.tick()
    assert events(session_factory).count("terminal") == 1


def test_risk_never_reads_free_text(session, session_factory, connection) -> None:
    trade_decision(session, session_factory)
    statements: list[str] = []
    event.listen(connection, "before_cursor_execute", lambda *a: statements.append(a[2].lower()))
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.tick()
    broker.fill(broker.only().client_order_id, "49", "200.20")
    risk.tick()
    risk.health()
    reads = [st for st in statements if st.lstrip().startswith("select")]
    assert reads
    for st in reads:
        for forbidden in ("signals", "llm_calls", "source_events", "labels", ".raw", "extraction", "headline",
                          "thesis", "kill_switch_events.reason", "policy_versions.text", ".note"):
            assert forbidden not in st, (forbidden, st)


def test_risk_module_does_not_load_the_llm_client() -> None:
    code = ("import sys, nta.risk, nta.broker, nta.cli; "
            "bad = [m for m in sys.modules if m.startswith('anthropic') or m in ('nta.extract', 'nta.agent')]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_risk_refuses_llm_credentials_and_shared_accounts() -> None:
    base = {"ALPACA_TRADING_KEY_ID": "t", "ALPACA_TRADING_SECRET": "t", "RISK_SIGNING_KEY": KEY,
            "ALPACA_KEY_ID": "d", "ALPACA_SECRET": "d"}
    res = CliRunner().invoke(app, ["risk", "--once"], env={**base, "ANTHROPIC_API_KEY": "sk-ant-x"})
    assert res.exit_code == 2 and "LLM credentials" in res.output
    res = CliRunner().invoke(app, ["risk", "--once"], env={**base, "ALPACA_KEY_ID": "t", "ANTHROPIC_API_KEY": ""})
    assert res.exit_code == 2 and "different paper accounts" in res.output
    res = CliRunner().invoke(app, ["risk", "--once"], env={**base, "RISK_SIGNING_KEY": "short", "ANTHROPIC_API_KEY": ""})
    assert res.exit_code == 2 and "RISK_SIGNING_KEY" in res.output


def test_kill_resume_needs_typed_confirmation(monkeypatch, session_factory) -> None:
    import nta.db
    monkeypatch.setattr(nta.db, "make_engine", lambda url=None: session_factory.kw["bind"])
    monkeypatch.setattr(nta.db, "make_session_factory", lambda engine: session_factory)
    r = CliRunner().invoke(app, ["kill", "pause", "-r", "testing"])
    assert r.exit_code == 0
    r = CliRunner().invoke(app, ["kill", "resume", "-r", "done"], input="yes\n")
    assert r.exit_code == 1 and "not resumed" in r.output
    r = CliRunner().invoke(app, ["kill", "resume", "-r", "done"], input="RESUME\n")
    assert r.exit_code == 0
    states = [(k.state, k.actor) for k in all_rows(session_factory, KillSwitchEvent)]
    assert states == [(KillState.PAUSE, "human"), (KillState.RUN, "human")]
