import json
import urllib.error
import urllib.request

from decimal import Decimal



from nta.decide import load_kill_state
from nta.health import HealthServer

from nta.schema import Incident, KillState, KillSwitchEvent, Order, PositionSnapshot, RiskReview, RiskVerdict
from tests.broker_fakes import FakeBroker
from tests.test_risk import all_rows, close_for, filled_position, service, trade_decision


def snaps(sf, account="T"):
    return [x for x in all_rows(sf, PositionSnapshot) if x.account == account]


def test_startup_reconcile_runs_before_any_order(session, session_factory) -> None:
    trade_decision(session, session_factory)
    broker = FakeBroker()
    real_account = broker.account
    broker.account = lambda: (_ for _ in ()).throw(ConnectionError("broker down"))
    risk = service(session_factory, broker)
    risk.tick()
    assert broker.attempts == 0 and all_rows(session_factory, RiskReview) == []   # nothing sent, nothing reviewed
    assert risk.health()["unhealthy"]
    broker.account = real_account
    risk.tick()
    fixture_snap, snap = snaps(session_factory)  # the fixture's equity row, then the reconcile
    assert snap.run_id is None and snap.taken_at > fixture_snap.taken_at
    assert snap.mismatch is False and snap.equity == Decimal("100000")
    assert len(broker.submissions) == 1
    assert snap.created_at <= all_rows(session_factory, Order)[0].created_at


def test_matching_fills_are_synced_before_comparing(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    risk.clock.advance(31)
    risk.tick()
    last = snaps(session_factory)[-1]
    assert last.mismatch is False and last.positions == {"AAPL": "49"} and last.log_positions == {"AAPL": "49"}


def test_reconcile_cadence_is_30_seconds(session, session_factory) -> None:
    broker = FakeBroker()
    risk = service(session_factory, broker)
    risk.tick()
    risk.clock.advance(10)
    risk.tick()
    assert len(snaps(session_factory)) == 1
    risk.clock.advance(21)
    risk.tick()
    assert len(snaps(session_factory)) == 2


def test_mismatch_pauses_within_one_cycle_entries_stop_closes_continue(session, session_factory) -> None:
    d, broker, risk = filled_position(session, session_factory)
    broker.extra_positions = {"MSFT": Decimal(3)}       # someone traded account T by hand
    risk.clock.advance(31)
    risk.tick()                                         # one reconcile cycle
    with session_factory() as s:
        assert load_kill_state(s, risk.clock.now()) == KillState.PAUSE
    [inc] = all_rows(session_factory, Incident)
    assert inc.kind == "reconcile_mismatch" and inc.detail["positions"]["MSFT"] == {"broker": "3", "log": "0"}
    [k] = [k for k in all_rows(session_factory, KillSwitchEvent)]
    assert k.actor == "reconciler" and k.incident_id == inc.id
    h = risk.health()
    assert h["unhealthy"] and h["kill_switch"] == "PAUSE" and h["open_incidents"][0]["kind"] == "reconcile_mismatch"
    # a new entry is refused...
    new_entry = trade_like(session_factory, d)
    # ...while a stop-loss close on the existing position still goes out
    close_for(session_factory, d, 49, risk.clock.now())
    risk.tick()
    reviews = {r.decision_id: r for r in all_rows(session_factory, RiskReview)}
    assert reviews[new_entry].verdict == RiskVerdict.reject and "kill_switch" in reviews[new_entry].reasons
    assert [o.qty for o in broker.orders.values() if o.side == "sell"] == [Decimal(49)]
    # the incident is not duplicated on later cycles
    risk.clock.advance(31)
    risk.tick()
    assert len(all_rows(session_factory, Incident)) == 1


def trade_like(sf, d):
    """A second entry decision for the same signal's event (another signal id is not needed
    for the risk test): copy the trade decision as a close-free entry on a fresh signal."""
    from nta.schema import Decision, Signal
    with sf() as s, s.begin():
        sig = s.get(Signal, d.signal_id)
        sig2 = Signal(**{c.name: getattr(sig, c.name) for c in Signal.__table__.columns
                         if c.name not in ("id", "created_at", "source_event_id")},
                      source_event_id=_another_event(s, d.source_event_id))
        s.add(sig2)
        s.flush()
        cols = {c.name: getattr(d, c.name) for c in Decision.__table__.columns if c.name not in ("id", "created_at")}
        cols.update(signal_id=sig2.id, source_event_id=sig2.source_event_id)
        row = Decision(**cols)
        s.add(row)
        s.flush()
        return row.id


def _another_event(s, ev_id):
    from nta.schema import SourceEvent
    ev = s.get(SourceEvent, ev_id)
    cols = {c.name: getattr(ev, c.name) for c in SourceEvent.__table__.columns if c.name not in ("id", "created_at")}
    cols["external_id"] = ev.external_id + "-2"
    e2 = SourceEvent(**cols)
    s.add(e2)
    s.flush()
    return e2.id


def test_unknown_open_order_is_a_mismatch(session, session_factory) -> None:
    broker = FakeBroker()
    broker.submit_order(client_order_id="manual-1", symbol="KO", side="buy", qty=1, limit_price=Decimal(60))
    service(session_factory, broker).tick()
    [inc] = all_rows(session_factory, Incident)
    assert inc.detail["unknown_open_orders"] == ["manual-1"]


def test_watchdog_raises_severity_1_on_account_d_activity(session, session_factory) -> None:
    broker, d_account = FakeBroker(), FakeBroker()
    risk = service(session_factory, broker)
    risk.watchdog = d_account
    risk.tick()
    assert all_rows(session_factory, Incident) == [] and snaps(session_factory, "D")[0].mismatch is False
    d_account.extra_positions = {"AAPL": Decimal(1)}
    risk.clock.advance(31)
    risk.tick()
    [inc] = all_rows(session_factory, Incident)
    assert inc.severity == 1 and inc.kind == "account_d_activity"
    with session_factory() as s:
        assert load_kill_state(s, risk.clock.now()) == KillState.PAUSE
    risk.clock.advance(31)
    risk.tick()
    assert len(all_rows(session_factory, Incident)) == 1
    # after a human resume the still-present activity is reported again
    with session_factory() as s, s.begin():
        s.add(KillSwitchEvent(state=KillState.RUN, actor="human", reason="checked", at=risk.clock.now()))
    risk.clock.advance(31)
    risk.tick()
    assert len(all_rows(session_factory, Incident)) == 2


def test_healthz_is_503_while_an_incident_is_open(session_factory) -> None:
    broker = FakeBroker()
    broker.extra_positions = {"MSFT": Decimal(1)}
    risk = service(session_factory, broker)
    risk.tick()
    server = HealthServer(risk.health, port=0, stale_after_s=30).start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/healthz", timeout=2)
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as err:
            assert err.code == 503
            body = json.loads(err.read())
            assert body["open_incidents"][0]["kind"] == "reconcile_mismatch"
    finally:
        server.stop()


