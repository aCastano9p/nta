"""Evaluation. Lifecycle numbers (hand-computed):
entry 49 @ 200.20 against a decision quote with last 200.00 (IEX: mid = last) -> slippage +10.00 bps;
stop exit 49 @ 197.40 -> gross 49 x (197.40 - 200.20) = -137.20; notional 49 x 200.20 = 9,809.80;
fees 0.5 bps x 9,809.80 = 0.49; net -137.69; -137.69 / 9,809.80 = -140.36 bps; tail needs a loss
beyond 2 x 250 = 500 -> no.
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nta.decide import Reason
from nta.evaluate import (
    CRITERIA,
    Evaluator,
    agreement,
    cohen_kappa,
    propose_guardrails,
    quality,
)
from nta.extract import LlmGateway
from nta.label import choose_review, run_review
from nta.policy import DEFAULT_POLICY_PATH, load_policy, policy_hash
from nta.schema import (
    Decision,
    Estimate,
    Grade,
    Grader,
    GuardrailProposal,
    Outcome,
    Postmortem,
    Quote,
    QuoteKind,
)
from nta.simulate import simulate_long
from tests.fakes import FakeClock
from tests.llm_fakes import FakeBackend, message
from tests.test_manager import open_position
from tests.test_risk import all_rows

GRADE = {"credible": 2, "novel": 2, "interpreted": 2, "actionable": 1, "within_policy": 2,
         "contrary_evidence_handled": 1, "rationale": "Clear guidance raise, plan within policy."}
T0 = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def s(**kw):
    base = dict.fromkeys(CRITERIA, 2)
    return {**base, **kw}


@pytest.mark.parametrize("scores,expected", [
    (s(), "sound"),                                                       # 12
    (s(credible=1, novel=1, interpreted=1), "sound"),                     # 9
    (s(credible=1, novel=1, interpreted=1, actionable=1), "insufficient"),  # 8
    (dict.fromkeys(CRITERIA, 1), "unsound"),                              # 6
    (s(novel=0), "unsound"),                                              # 10 but a zero
])
def test_quality_thresholds(scores, expected) -> None:
    assert quality(scores) == expected


def test_cohen_kappa_known_values() -> None:
    # po = 0.75; rater A: a=0.5, rater B: a=0.25 -> pe = 0.5 x 0.25 + 0.5 x 0.75 = 0.5 -> kappa 0.5
    assert cohen_kappa([("a", "a"), ("a", "b"), ("b", "b"), ("b", "b")]) == pytest.approx(0.5)
    assert cohen_kappa([("a", "a"), ("b", "b")]) == 1.0
    assert cohen_kappa([]) is None


def test_simulate_long() -> None:
    t = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
    kw = dict(entry=Decimal(100), stop=Decimal(98), take_profit=Decimal(104), start=T0, expiry=t(60))
    r = simulate_long(prices=[(t(5), Decimal(101)), (t(10), Decimal("97.5"))], **kw)
    # gapped through the stop: filled at the observed 97.50, not at 98
    assert (r.exit_price, r.exit_reason, r.mfe_bps, r.mae_bps) == (Decimal("97.5"), "stop", Decimal(100), Decimal(-250))
    r = simulate_long(prices=[(t(5), Decimal(105))], **kw)
    assert (r.exit_price, r.exit_reason) == (Decimal(104), "take_profit")
    r = simulate_long(prices=[(t(5), Decimal(101)), (t(55), Decimal("100.5"))], **kw)
    assert (r.exit_price, r.exit_reason, r.uncertain) == (Decimal("100.5"), "expiry", True)  # 50-minute gap
    assert simulate_long(prices=[(t(61), Decimal(1))], **kw).exit_reason == "no_data"


class FakeBench:
    def __init__(self):
        self.calls = []

    def spy_return_bps(self, start, end):
        self.calls.append((start, end))
        return Decimal("12.5")


def lifecycle(session, sf):
    agent, risk, broker, mgr, inst = open_position(session, sf)
    agent.alpaca.default = (None, None, "197.50")
    agent.clock.advance(5)
    mgr.step()
    risk.clock.advance(10)
    risk.quotes = lambda sym, c=risk.clock: __import__("tests.test_manager", fromlist=["_q"])._q("197.50", c)
    risk.tick()
    sell = [o for o in broker.orders.values() if o.side == "sell"][0]
    broker.fill(sell.client_order_id, "49", "197.40")
    risk.tick()
    return agent, risk


def evaluator(sf, now, backend=None, bench=None):
    clock = FakeClock(now)
    gw = LlmGateway(sf, clock, backend=backend or FakeBackend(answer=lambda q: message(GRADE)), daily_usd=Decimal(5))
    return Evaluator(sf, clock, gw, load_policy(), benchmark=bench or FakeBench())


def test_full_lifecycle_is_graded_and_measured(session, session_factory) -> None:
    lifecycle(session, session_factory)
    entry = [d for d in all_rows(session_factory, Decision) if d.kind == "entry"][0]
    assert evaluator(session_factory, T0 + timedelta(hours=1)).run() == 0      # horizon not passed yet
    backend = FakeBackend(answer=lambda q: message(GRADE))
    bench = FakeBench()
    ev = evaluator(session_factory, datetime(2026, 11, 6, 17, 0, tzinfo=UTC), backend, bench)
    assert ev.run() == 1
    [pm] = all_rows(session_factory, Postmortem)
    x, e = pm.execution, pm.economic
    assert x["fill_ratio"] == "1.0000" and x["slippage_vs_mid_bps"] == "10.00" and x["entry_vs_plan_bps"] == "0.00"
    assert x["exit_triggers"] == ["stop"] and x["decide_to_submit_s"] >= 0 and x["submit_to_fill_s"] >= 0
    assert (e["gross_pnl"], e["fees"], e["net_pnl"], e["net_bps"]) == ("-137.20", "0.49", "-137.69", "-140.36")
    assert e["tail"] is False and e["spy_bps"] == "12.5" and e["open_qty"] == "0"
    assert e["mae_bps"].startswith("-") and len(bench.calls) == 1
    [g] = all_rows(session_factory, Grade)
    assert g.grader == Grader.llm and g.total == 10 and not g.has_zero and g.packet_hash == pm.packet_hash
    assert (pm.quality, pm.result, pm.quadrant) == ("sound", "loss", "sound_loss")
    # the packet is frozen at decided_at: nothing observed later
    for q in pm.packet["quotes"]:
        assert datetime.fromisoformat(q["observed_at"]) <= entry.decided_at
    assert pm.packet["llm_calls"]["extract"]["request_hash"]
    # the grader saw no outcome
    [req] = [q for q in backend.requests if "review past" in q["system"]]
    content = req["messages"][0]["content"]
    for leak in ("197.4", "net_pnl", "fill", "137"):
        assert leak not in content, leak
    assert "<untrusted_item>" in content and content.count("</untrusted_item>") == 1
    # idempotent
    assert ev.run() == 0


def test_estimate_ledger_for_late_abstention(session, session_factory) -> None:
    from tests.agent_fakes import MARKET_T, add_equity, add_filing, add_instrument, rig
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(hours=2))
    add_filing(session, inst, MARKET_T - timedelta(minutes=90))      # 90 min old: late
    session.commit()
    rig(session_factory).pipeline.step(None)
    [d] = all_rows(session_factory, Decision)
    assert d.outcome == Outcome.abstain and Reason.late in d.reasons
    # later recorded prices touch the take-profit (204.00)
    with session_factory() as s, s.begin():
        for i, px in enumerate(["201", "203", "204.10"], start=1):
            t = MARKET_T + timedelta(minutes=5 * i)
            s.add(Quote(instrument_id=inst.id, ts=t, first_observed_at=t, last=Decimal(px), source="alpaca_iex",
                        kind=QuoteKind.periodic))
    evaluator(session_factory, datetime(2026, 11, 6, 17, 0, tzinfo=UTC)).run()
    [est] = all_rows(session_factory, Estimate)
    # entry 200 -> take-profit 204: +200 bps - 15.50 bps cost = 184.50; qty 49 x 200 x 1.845 % = 180.81
    assert (est.reason, est.exit_reason, est.exit_price, est.net_bps) == ("late", "take_profit", Decimal("204"), Decimal("184.50"))
    assert est.pnl_usd == Decimal("180.81")
    [pm] = all_rows(session_factory, Postmortem)
    assert pm.result == "win" and pm.economic == {}


def test_guardrail_proposals_are_rows_and_the_policy_file_is_untouched(session, session_factory) -> None:
    before = policy_hash(DEFAULT_POLICY_PATH.read_text())
    lp = load_policy()
    rows = []
    with session_factory() as s, s.begin():
        from tests.factories import one_of_each
        base = one_of_each(s)["decisions"]
        for i in range(3):
            d = Decision(**{c.name: getattr(base, c.name) for c in Decision.__table__.columns
                            if c.name not in ("id", "created_at", "signal_id", "policy_version", "kind", "parent_decision_id")},
                         kind="close", parent_decision_id=base.id, policy_version=lp.hash)
            s.add(d)
            s.flush()
            rows.append(Postmortem(decision_id=d.id, evaluated_at=T0, packet_hash="x" * 64, packet={},
                                   economic={"tail": i < 2}))
        s.add_all(rows)
    [p] = propose_guardrails(session_factory, lp)
    assert p.rule == "tail_losses" and p.proposed == {"risk_unit_pct": "0.125"}
    assert propose_guardrails(session_factory, lp) == []          # once per policy version
    assert len([g for g in all_rows(session_factory, GuardrailProposal) if g.policy_version == lp.hash]) == 1
    assert policy_hash(DEFAULT_POLICY_PATH.read_text()) == before


def test_no_code_writes_files_except_the_verdict_csv() -> None:
    """Nothing writes the policy file (or any file) except a human; the verdict CSV export is
    the only file write in the package."""
    writers = set()
    for path in (Path(__file__).resolve().parent.parent / "nta").glob("*.py"):
        src = path.read_text()
        if re.search(r"\.write_text\(|\.write_bytes\(|open\([^)]*['\"][wax]\+?b?['\"]", src):
            writers.add(path.name)
    assert writers == {"verdict.py"}


def test_review_sample_and_human_grades(session, session_factory) -> None:
    graded = [(f"D{i:02d}", i == 7) for i in range(20)]
    picked = choose_review(graded, seed=7)
    assert "D07" in picked and len(picked) in (4, 5)              # 20 % of 20 = 4, plus the zero
    assert choose_review(graded, seed=7) == picked

    lifecycle(session, session_factory)
    evaluator(session_factory, datetime(2026, 11, 6, 17, 0, tzinfo=UTC)).run()
    answers = iter(["2", "2", "1", "1", "2", "0"])
    out = []
    n = run_review(session_factory, FakeClock(T0), seed=7, input_fn=lambda p: next(answers), print_fn=out.append)
    assert n == 1
    text = "\n".join(out)
    assert "AAPL" in text and "197.4" not in text and "net" not in text.lower().replace("internet", "")
    human = [g for g in all_rows(session_factory, Grade) if g.grader == Grader.human]
    assert human[0].scores["contrary_evidence_handled"] == 0 and human[0].has_zero
    with session_factory() as s:
        a = agreement(s)
    assert a["n"] == 1 and a["quality_kappa"] is not None
