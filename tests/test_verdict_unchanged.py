"""Phase 0's verdict is the go/no-go gate: phase 1 rows must not change its output."""

import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from nta.demo import load_spec
from nta.schema import (
    Decision,
    Instrument,
    KillState,
    KillSwitchEvent,
    PositionSnapshot,
    Quote,
    QuoteKind,
    SourceEvent,
)
from nta.verdict import run
from tests.agent_fakes import rig
from tests.test_verdict import END, FIXTURE, START


def test_verdict_output_is_unchanged_by_phase_1_rows(session_factory, session, tmp_path) -> None:
    load_spec(session_factory, FIXTURE)
    before_text, _ = run(session, START, END, tmp_path / "before.csv")
    before_csv = (tmp_path / "before.csv").read_text()

    # Phase 1 runs over the same data: signals, decisions, decision quotes on the same
    # filings, LLM calls, manage quotes, an X post, snapshots and kill switch rows.
    with session_factory() as s:
        events = list(s.scalars(select(SourceEvent).order_by(SourceEvent.first_observed_at)))
        inst = s.scalars(select(Instrument)).first()
    last = events[-1].first_observed_at
    with session_factory() as s, s.begin():
        s.add(PositionSnapshot(account="T", taken_at=events[0].first_observed_at - timedelta(hours=1),
                               equity=Decimal(100000), positions={}, mismatch=False))
        s.add(KillSwitchEvent(state=KillState.PAUSE, actor="reconciler", reason="t", at=events[3].first_observed_at))
        s.add(SourceEvent(source_id="x", external_id="x:1", form_type="post", cik=inst.cik, instrument_id=inst.id,
                          published_at=last, first_observed_at=last, ingested_at=last, url="https://x.com/i/1",
                          raw=json.dumps({"text": "AAPL guidance raised"})))
    r = rig(session_factory, start=last + timedelta(seconds=5))
    assert r.pipeline.step(None)[1] == len(events) + 1
    for ev in events:
        r.pipeline.quotes.manage_quote(ev.id, "AAPL", ev.instrument_id)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Decision)) == len(events) + 1
        assert s.scalar(select(func.count()).select_from(Quote).where(Quote.kind == QuoteKind.decision)) > 0

    after_text, _ = run(session, START, END, tmp_path / "after.csv")
    assert after_text.replace("after.csv", "X") == before_text.replace("before.csv", "X")
    assert (tmp_path / "after.csv").read_text() == before_csv
    assert "VERDICT: " in after_text
