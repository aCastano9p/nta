"""Minimal row builders for tests. One valid row per table."""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from nta.schema import (
    Decision,
    DecisionKind,
    Direction,
    Estimate,
    Fill,
    Grade,
    Grader,
    GuardrailProposal,
    Incident,
    KillState,
    KillSwitchEvent,
    LlmCall,
    Order,
    OrderEvent,
    Outcome,
    PolicyVersion,
    PositionSnapshot,
    Postmortem,
    RiskReview,
    RiskVerdict,
    Side,
    Signal,
    SignalStatus,
    XPoll,
    EventType,
    Instrument,
    Label,
    OpenFigiResponse,
    Quote,
    QuoteKind,
    QuoteSchedule,
    Run,
    RunStop,
    SourceEvent,
)

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


def one_of_each(s: Session) -> dict[str, object]:
    inst = Instrument(ticker="AAPL", figi="BBG000B9XRY4", cik="0000320193", name="Apple Inc.",
                      aliases=["Apple"], added_on=date(2026, 10, 1))
    run = Run(process="poll_edgar", code_sha="abc", config_hash="def", started_at=T0)
    s.add_all([inst, run])
    s.flush()
    ev = SourceEvent(source_id="edgar", external_id="0000320193-26-000001", form_type="8-K",
                     cik="0000320193", instrument_id=inst.id, published_at=T0,
                     first_observed_at=T0, ingested_at=T0, url="https://www.sec.gov/x",
                     items=["2.02"], raw="hello", raw_hash="0" * 64, run_id=run.id)
    figi = OpenFigiResponse(request_key='{"idType":"TICKER"}', response={"data": []},
                            first_observed_at=T0)
    stop = RunStop(run_id=run.id, stopped_at=T0, reason="test")
    s.add_all([ev, figi, stop])
    s.flush()
    sched = QuoteSchedule(source_event_id=ev.id, instrument_id=inst.id, kind=QuoteKind.mark_1h,
                          due_at=T0)
    quote = Quote(instrument_id=inst.id, source_event_id=ev.id, ts=T0, first_observed_at=T0,
                  bid=1, ask=2, last=1.5, source="alpaca_iex", kind=QuoteKind.observe)
    label = Label(source_event_id=ev.id, material=True, direction=Direction.up,
                  event_type=EventType.guidance, labeled_at=T0)
    call = LlmCall(request_hash="a" * 64, purpose="extract", prompt_version="extract_v1",
                   model_id="claude-sonnet-5-5", request={"m": 1}, response={"r": 1}, valid=True,
                   latency_ms=900, cost_usd=0.01, first_observed_at=T0)
    s.add_all([sched, quote, label, call])
    s.flush()
    sig = Signal(source_event_id=ev.id, instrument_id=inst.id, origin="edgar", source_class="primary",
                 source_weight=1, headline="apple 8-k items 2.02", minhash=[1, 2], event_type="guidance",
                 cluster_id="0" * 26, is_repeat=False, corroboration=1, corroborated=True,
                 status=SignalStatus.extracted, extract_call_id=call.id, extraction={"direction": "up"},
                 ready_at=T0)
    s.add(sig)
    s.flush()
    dec = Decision(kind=DecisionKind.entry, signal_id=sig.id, source_event_id=ev.id, instrument_id=inst.id,
                   symbol="AAPL", outcome=Outcome.trade, reasons=[], side=Side.buy, qty=10,
                   quote={"last": "1.5"}, exposure={}, plan={"entry": "1.5"}, decided_at=T0,
                   code_sha="abc", policy_version="p" * 64, prompt_version="v1", model_id="m",
                   content_hash="c" * 64)
    incident = Incident(severity=1, kind="reconcile_mismatch", process="risk", detail={}, at=T0)
    policy = PolicyVersion(policy_hash="p" * 64, text="risk_unit_pct: 0.25", signature="s" * 64,
                           process="risk", run_id=run.id)
    snap = PositionSnapshot(account="T", taken_at=T0, equity=100000, positions={}, mismatch=False,
                            run_id=run.id)
    proposal = GuardrailProposal(rule="tail_losses", policy_version="p" * 64, current={"a": 1},
                                 proposed={"a": 0.5}, evidence={})
    s.add_all([dec, incident, policy, snap, proposal])
    s.flush()
    kill = KillSwitchEvent(state=KillState.PAUSE, actor="reconciler", reason="test",
                           incident_id=incident.id, at=T0)
    review = RiskReview(decision_id=dec.id, verdict=RiskVerdict.approve, reasons=[], quote={},
                        checks={}, policy_version="p" * 64, reviewed_at=T0)
    order = Order(decision_id=dec.id, leg=1, client_order_id=f"nta-{dec.id}-1", symbol="AAPL",
                  side=Side.buy, qty=10, limit_price=1.5, hmac="h" * 64, policy_version="p" * 64)
    grade = Grade(decision_id=dec.id, grader=Grader.llm, scores={"credible": 2}, total=2,
                  has_zero=False, packet_hash="k" * 64, llm_call_id=call.id, graded_at=T0)
    pm = Postmortem(decision_id=dec.id, evaluated_at=T0, packet_hash="k" * 64, packet={})
    est = Estimate(decision_id=dec.id, reason="late", plan={}, evaluated_at=T0)
    s.add_all([kill, review, order, grade, pm, est])
    s.flush()
    oev = OrderEvent(order_id=order.id, event="submitted", detail={}, first_observed_at=T0)
    fill = Fill(order_id=order.id, decision_id=dec.id, symbol="AAPL", side=Side.buy, qty=10, price=1.5,
                cum_qty=10, first_observed_at=T0)
    xpoll = XPoll(author_id="12345678", status="ok", posts_returned=2, posts_billable=2, cost_usd=0.01,
                  newest_id="1900000000000000001", first_observed_at=T0)
    s.add_all([oev, fill, xpoll])
    s.flush()
    return {
        "instruments": inst, "runs": run, "source_events": ev, "openfigi_responses": figi,
        "run_stops": stop, "quote_schedule": sched, "quotes": quote, "labels": label,
        "llm_calls": call, "signals": sig, "decisions": dec, "incidents": incident,
        "policy_versions": policy, "position_snapshots": snap, "guardrail_proposals": proposal,
        "kill_switch_events": kill, "risk_reviews": review, "orders": order, "grades": grade,
        "postmortems": pm, "estimates": est, "order_events": oev, "fills": fill, "x_polls": xpoll,
    }
