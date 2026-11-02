"""Decision engine. Expected numbers are worked out by hand in each test.

Base case: equity 100,000; risk unit 0.25 % -> 250 USD; last trade 200.00 (IEX policy, so the
reference price is the last trade); no 5-minute history -> no ATR, so stop distance =
1 % of 200 = 2.00; shares by risk = 250 / 2 = 125; entry limit = 200 x 1.001 = 200.20;
position cap 10 % = 10,000 / 200.20 = 49.95 -> 49 shares (capped by "position").
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nta.decide import (
    Exposure,
    MarketInputs,
    Position,
    QuoteView,
    Reason,
    SignalView,
    Versions,
    atr_5m,
    check_already_priced,
    decide_entry,
    expiry_after,
    load_exposure,
    load_kill_state,
    load_ref_at_observe,
    realized_daily_vol,
)
from nta.marketcal import nyse_sessions
from nta.policy import DEFAULT_POLICY_PATH, SourceKind, load_policy, parse_policy
from nta.schema import (
    Decision,
    DecisionKind,
    Fill,
    KillState,
    KillSwitchEvent,
    Outcome,
    PositionSnapshot,
    Quote,
    QuoteKind,
    Side,
    SignalStatus,
)
from tests.factories import one_of_each

POLICY = load_policy().policy
SIP = parse_policy(DEFAULT_POLICY_PATH.read_text().replace("quote_source: iex", "quote_source: sip")).policy
# Wednesday 2026-11-04, 11:00 New York (16:00 UTC)
AS_OF = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
SESSIONS = nyse_sessions(AS_OF.date() - timedelta(days=7), AS_OF.date() + timedelta(days=21))
V = Versions("sha", "p" * 64, "triage_v1+extract_v1", "h+s", None)


def sig(**kw) -> SignalView:
    base = dict(source_event_id="E1", instrument_id="I1", symbol="AAPL", cluster_id="E1",
                source_kind=SourceKind.filing, first_observed_at=AS_OF - timedelta(seconds=20),
                status=SignalStatus.extracted, direction="up", p_up=0.7, is_repeat=False, corroborated=True)
    return SignalView(**{**base, **kw})


def quote(last="200", bid=None, ask=None, age_s=1.0) -> QuoteView:
    d = lambda v: Decimal(v) if v is not None else None  # noqa: E731
    return QuoteView(d(bid), d(ask), d(last), AS_OF - timedelta(seconds=age_s), AS_OF, "alpaca_iex", "Q1")


def market(**kw) -> MarketInputs:
    base = dict(as_of=AS_OF, quote=quote(), ref_at_observe=Decimal("200"), recent_prices=[],
                exposure=Exposure(Decimal("100000")), kill_state=KillState.RUN, sessions=SESSIONS)
    return MarketInputs(**{**base, **kw})


def decide(s=None, m=None, policy=POLICY):
    return decide_entry(s or sig(), m or market(), policy, V, AS_OF)


def test_trade_with_hand_computed_plan() -> None:
    d = decide()
    assert d.outcome == Outcome.trade and d.reasons == ()
    assert d.side == Side.buy and d.qty == 49
    p = d.plan
    assert (p["ref_price"], p["entry"], p["stop"], p["take_profit"]) == ("200", "200.20", "198.00", "204.00")
    assert p["stop_distance"] == "2.00" and p["risk_usd"] == "250.00" and p["capped_by"] == ["position"]
    # expiry: 2 trading days later (Friday 11:00 New York)
    assert p["expiry"] == "2026-11-06T16:00:00+00:00"
    # 5 bps assumed spread + 2 x 5 bps slippage + 0.5 bps fees
    assert d.expected_cost_bps == Decimal("15.50")
    assert d.spread_unverified is True and d.quote["last"] == "200"


def test_atr_widens_the_stop() -> None:
    # 14 alternating moves of 3.00 -> ATR 3.00 -> stop distance 2 x 3 = 6.00 > 2.00
    t0 = AS_OF - timedelta(minutes=5 * 15)
    prices = [(t0 + timedelta(minutes=5 * i), Decimal("200") + (3 if i % 2 else 0)) for i in range(15)]
    assert atr_5m(prices, 14) == Decimal("3.0000")
    d = decide(m=market(recent_prices=prices))
    assert d.plan["stop"] == "194.00" and d.plan["take_profit"] == "212.00"
    # shares by risk = 250 / 6 = 41, under the position cap of 49
    assert d.qty == 41 and d.plan["capped_by"] == []


@pytest.mark.parametrize("kw,reason", [
    (dict(direction="unclear"), Reason.unclear),
    (dict(p_up=0.55), Reason.low_conviction),
    (dict(is_repeat=True), Reason.repeat),
    (dict(corroborated=False), Reason.uncorroborated),
    (dict(first_observed_at=AS_OF - timedelta(minutes=61)), Reason.late),
])
def test_judgment_reasons_abstain(kw, reason) -> None:
    d = decide(s=sig(**kw))
    assert d.outcome == Outcome.abstain and reason in d.reasons and d.side is None and d.qty is None


def test_down_is_long_only_with_a_hypothetical_short() -> None:
    d = decide(s=sig(direction="down"))
    assert d.outcome == Outcome.abstain and d.reasons == (Reason.long_only,)
    h = d.hypothetical
    assert h["side"] == "sell" and h["stop"] == "202.00" and h["take_profit"] == "196.00" and h["entry"] == "199.80"


@pytest.mark.parametrize("m,reason", [
    (dict(as_of=datetime(2026, 11, 7, 16, 0, tzinfo=UTC)), Reason.market_closed),      # Saturday
    (dict(as_of=datetime(2026, 11, 4, 14, 32, tzinfo=UTC)), Reason.scheduled_blackout),  # 09:32 NY
    (dict(as_of=datetime(2026, 11, 4, 20, 55, tzinfo=UTC)), Reason.scheduled_blackout),  # 15:55 NY
    (dict(kill_state=KillState.PAUSE), Reason.kill_switch),
    (dict(quote=quote(age_s=6)), Reason.quote_stale),
    (dict(quote=quote(last=None)), Reason.quote_missing),
    (dict(exposure=Exposure(None)), Reason.no_equity),
])
def test_market_state_reasons_wait(m, reason) -> None:
    as_of = m.get("as_of", AS_OF)
    s = sig(first_observed_at=as_of - timedelta(seconds=20))
    q = m.pop("quote", None) or QuoteView(None, None, Decimal("200"), as_of - timedelta(seconds=1), as_of, "alpaca_iex")
    d = decide(s=s, m=market(quote=q, **m))
    assert d.outcome == Outcome.wait and reason in d.reasons, d.reasons


def test_abstain_wins_over_wait() -> None:
    d = decide(s=sig(corroborated=False), m=market(kill_state=KillState.PAUSE))
    assert d.outcome == Outcome.abstain and set(d.reasons) == {Reason.uncorroborated, Reason.kill_switch}


def test_spread_is_checked_only_on_sip() -> None:
    wide = quote(bid="190", ask="210")   # 1000 bps
    assert decide(m=market(quote=wide)).outcome == Outcome.trade          # IEX: last trade, flagged
    d = decide(m=market(quote=wide, ref_at_observe=Decimal("210")), policy=SIP)
    assert d.outcome == Outcome.wait and d.reasons == (Reason.spread_wide,) and not d.spread_unverified
    tight = quote(bid="199.98", ask="200.02")  # 2 bps
    d = decide(m=market(quote=tight), policy=SIP)
    assert d.outcome == Outcome.trade and d.plan["ref_price"] == "200.02"  # SIP plans at the ask
    assert d.expected_cost_bps == Decimal("12.50")  # 2 + 10 + 0.5


def test_already_priced() -> None:
    # limit = 1.5 x 2 % x sqrt(600 / 23400) = 0.4804 %
    assert check_already_priced(Decimal("100"), Decimal("100.47"), Decimal("0.02"), 600, POLICY) is None
    assert check_already_priced(Decimal("100"), Decimal("100.49"), Decimal("0.02"), 600, POLICY) == Reason.already_priced
    assert check_already_priced(Decimal("100"), Decimal("99.51"), Decimal("0.02"), 600, POLICY) == Reason.already_priced
    # elapsed floored at 300 s: 1.5 x 2 % x sqrt(300 / 23400) = 0.3397 %
    assert check_already_priced(Decimal("100"), Decimal("100.33"), Decimal("0.02"), 5, POLICY) is None
    assert check_already_priced(None, Decimal("1"), Decimal("0.02"), 5, POLICY) is None
    d = decide(m=market(ref_at_observe=Decimal("195")))  # +2.56 % in 20 s
    assert d.reasons == (Reason.already_priced,)


def test_caps() -> None:
    pos = lambda sym, cost, sector="health_care", cluster="X": Position(sym, Decimal(10), Decimal(cost), sector, cluster)  # noqa: E731
    five = Exposure(Decimal("100000"), tuple(pos(f"S{i}", "1000") for i in range(5)))
    d = decide(m=market(exposure=five))
    assert d.reasons == (Reason.cap_hit,) and d.plan["qty"] == 0 and "max_open_positions" in d.plan["capped_by"]
    # tech sector already at 24,000: room 1,000 / 200.20 = 4 shares
    tech = Exposure(Decimal("100000"), (pos("MSFT", "24000", "information_technology"),))
    d = decide(m=market(exposure=tech))
    assert d.outcome == Outcome.trade and d.qty == 4 and d.plan["capped_by"] == ["position", "sector"]
    # same cluster already at 14,000: cluster room 1,000 -> 4 shares
    clus = Exposure(Decimal("100000"), (pos("JNJ", "14000", cluster="E1"),))
    assert decide(m=market(exposure=clus)).qty == 4


def test_signal_level_abstentions_fetch_nothing() -> None:
    for status, reason in [(SignalStatus.llm_budget, "llm_budget"), (SignalStatus.llm_invalid, "llm_invalid"),
                           (SignalStatus.triage_rejected, "not_material")]:
        d = decide_entry(sig(status=status, direction=None, p_up=None), None, POLICY, V, AS_OF)
        assert d.outcome == Outcome.abstain and d.reasons == (reason,) and d.quote is None and d.plan is None


def test_content_hash_covers_content_not_ids_or_code() -> None:
    a = decide()
    b = decide_entry(sig(), market(quote=QuoteView(*[getattr(quote(), f) for f in
                                                     ("bid", "ask", "last", "ts", "observed_at", "source")], "OTHER")),
                     POLICY, Versions("other-sha", V.policy_version, V.prompt_version, V.model_id, "SNAP"), AS_OF)
    assert a.content_hash == b.content_hash
    c = decide_entry(sig(), market(), POLICY, Versions("sha", "q" * 64, V.prompt_version, V.model_id, None), AS_OF)
    assert c.content_hash != a.content_hash
    assert decide(m=market(quote=quote(last="200.01"))).content_hash != a.content_hash


def test_vol_and_expiry_helpers() -> None:
    t0 = AS_OF - timedelta(hours=3)
    flat = [(t0 + timedelta(minutes=5 * i), Decimal("100")) for i in range(30)]
    assert realized_daily_vol(flat, Decimal("2")) == 0
    assert realized_daily_vol(flat[:5], Decimal("2")) == Decimal("0.02")  # too few returns
    fri = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)  # Friday 14:00 NY
    assert expiry_after(SESSIONS + nyse_sessions(AS_OF.date() + timedelta(days=22), AS_OF.date() + timedelta(days=40)),
                        fri, 2) == datetime(2026, 11, 10, 19, 0, tzinfo=UTC)  # Tuesday 14:00 NY
    # Wednesday 25 Nov 14:00 NY: +1 session skips Thanksgiving to Friday's 13:00 early close
    # (clamped to 12:55 NY); +2 sessions is Monday 30 Nov 14:00 NY.
    wed = datetime(2026, 11, 25, 19, 0, tzinfo=UTC)
    later = nyse_sessions(wed.date(), wed.date() + timedelta(days=14))
    assert expiry_after(later, wed, 1) == datetime(2026, 11, 27, 17, 55, tzinfo=UTC)
    assert expiry_after(later, wed, 2) == datetime(2026, 11, 30, 19, 0, tzinfo=UTC)


# ---------- loaders ----------

def test_point_in_time_loaders(session) -> None:
    rows = one_of_each(session)
    inst, ev, entry = rows["instruments"], rows["source_events"], rows["decisions"]
    t0 = rows["fills"].first_observed_at
    # factory: one buy fill of 10 @ 1.5 for the entry decision
    close = Decision(kind=DecisionKind.close, parent_decision_id=entry.id, source_event_id=ev.id,
                     instrument_id=inst.id, symbol="AAPL", outcome=Outcome.trade, reasons=[], side=Side.sell, qty=4,
                     exposure={}, plan={"reason": "stop"}, decided_at=t0, code_sha="x", policy_version="p" * 64,
                     prompt_version="v", model_id="m", content_hash="d" * 64)
    session.add(close)
    session.flush()
    session.add(Fill(order_id=rows["orders"].id, decision_id=close.id, symbol="AAPL", side=Side.sell, qty=4,
                     price=2, cum_qty=4, first_observed_at=t0 + timedelta(minutes=1)))
    session.add(PositionSnapshot(account="T", taken_at=t0 + timedelta(minutes=2), equity=90000, positions={},
                                 mismatch=False))
    session.add(KillSwitchEvent(state=KillState.RUN, actor="human", reason="resume", at=t0 + timedelta(minutes=3)))
    session.flush()
    before = load_exposure(session, t0, POLICY)
    assert before.equity == Decimal("100000")
    assert [(p.symbol, p.qty, p.cost, p.cluster_id) for p in before.positions] == [("AAPL", 10, Decimal("15.00"), entry.cluster_id)]
    after = load_exposure(session, t0 + timedelta(minutes=2), POLICY)
    assert after.equity == Decimal("90000")
    assert [(p.qty, p.cost) for p in after.positions] == [(6, Decimal("9.00"))]
    assert load_kill_state(session, t0) == KillState.PAUSE          # factory row
    assert load_kill_state(session, t0 + timedelta(minutes=3)) == KillState.RUN
    assert load_ref_at_observe(session, inst.id, ev.id, ev.first_observed_at, t0) == Decimal("1.5")
    q = Quote(instrument_id=inst.id, source_event_id=ev.id, ts=t0, first_observed_at=t0, last=3,
              source="alpaca_iex", kind=QuoteKind.decision)
    session.add(q)
    session.flush()
    assert load_ref_at_observe(session, inst.id, ev.id, ev.first_observed_at, t0) == Decimal("1.5")  # observe wins
