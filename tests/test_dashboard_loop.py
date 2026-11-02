from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nta.dashboard import Dashboard
from nta.schema import (
    Decision,
    Estimate,
    Incident,
    KillState,
    KillSwitchEvent,
    Postmortem,
    XPoll,
)
from sqlalchemy import select
from tests import replay_scenario as sc
from tests.fakes import FakeClock
from tests.test_dashboard import get

NOW = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
XSS = '<script>alert("x")</script>'


@pytest.fixture
def dash(session_factory):
    d = Dashboard(session_factory, db_name="nta_test", port=0, health_urls={}, clock=FakeClock(start=NOW)).start()
    yield d
    d.stop()


def seed(sf):
    sc.load(sf)
    with sf() as s, s.begin():
        entry = s.scalars(select(Decision).where(Decision.kind == "entry", Decision.outcome == "trade")).one()
        abstain = s.scalars(select(Decision).where(Decision.reasons.op("?")("long_only"))).one()
        inc = Incident(severity=2, kind="reconcile_mismatch", process="risk", detail={"note": XSS}, at=NOW)
        s.add(inc)
        s.flush()
        s.add(KillSwitchEvent(state=KillState.PAUSE, actor="reconciler", reason=XSS, at=NOW, incident_id=inc.id))
        s.add(Postmortem(decision_id=entry.id, evaluated_at=NOW, packet_hash="p" * 64, packet={}, quality="sound",
                         result="loss", quadrant="sound_loss"))
        s.add(Estimate(decision_id=abstain.id, reason="late", plan={}, entry_price=Decimal(200), exit_price=Decimal(204),
                       exit_reason="take_profit", net_bps=Decimal("184.5"), pnl_usd=Decimal("180.81"), evaluated_at=NOW))
        s.add(XPoll(author_id="111", status="ok", posts_returned=3, posts_billable=3, cost_usd=Decimal("0.015"),
                    first_observed_at=NOW - timedelta(minutes=5)))


def test_loop_pages_render(dash, session_factory) -> None:
    seed(session_factory)
    pages = {}
    for path in ("/loop", "/decisions", "/orders", "/positions", "/incidents", "/estimates", "/spend"):
        status, headers, body = get(dash, path)
        assert status == 200, (path, body[:500])
        assert "<script" not in body and "script-src" not in headers["Content-Security-Policy"]
        assert '<meta http-equiv="refresh"' in body
        pages[path] = body
    loop = pages["/loop"]
    assert "PAUSE" in loop and "1 open incident" in loop
    assert "5 of 5 decisions" in loop                             # 4 entries with a quote + the stop close
    assert "100%" in loop                                         # every decision with a quote is IEX-flagged
    assert "sound_loss" in loop and "$0.0150" in loop             # quadrant counts, X spend today
    d = pages["/decisions"]
    for code in ("long_only", "repeat", "not_material", "llm_invalid", "market_closed", "stop"):
        assert code in d
    assert "approve" in d                                         # risk verdict column
    assert "nta-" in pages["/orders"] and "197.4000" in pages["/orders"] and "200.2000" in pages["/orders"]
    assert "match" in pages["/positions"] and "No open positions" in pages["/positions"]
    inc = pages["/incidents"]
    assert "reconcile_mismatch" in inc and "open" in inc and "&lt;script&gt;" in inc
    assert "184.5" in pages["/estimates"] and "take_profit" in pages["/estimates"]
    spend = pages["/spend"]
    assert "claude-sonnet-5-5" in spend and "extract" in spend


def test_loop_pages_render_on_an_empty_database(dash) -> None:
    for path in ("/loop", "/decisions", "/orders", "/positions", "/incidents", "/estimates", "/spend"):
        status, _, body = get(dash, path)
        assert status == 200 and "RUN" in get(dash, "/loop")[2]
