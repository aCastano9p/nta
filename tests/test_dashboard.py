import json
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from nta.dashboard import Dashboard, probe, px
from nta.demo import load_spec, recreate_demo_database, synthetic_spec
from nta.schema import Instrument, Label, SourceEvent, Direction, EventType
from tests.fakes import FakeClock

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "verdict" / "fixture.json").read_text())
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday 10:00 ET


@pytest.fixture
def dash(session_factory: sessionmaker[Session]):
    d = Dashboard(session_factory, db_name="nta_test", port=0, health_urls={},
                  clock=FakeClock(start=NOW)).start()
    yield d
    d.stop()


def get(d: Dashboard, path: str) -> tuple[int, dict, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{d.port}{path}", timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def test_empty_database_renders(dash) -> None:
    status, headers, body = get(dash, "/")
    assert status == 200
    assert "No filings yet" in body
    assert "script-src" not in headers["Content-Security-Policy"]
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_overview_and_verdict_from_fixture(dash, session_factory) -> None:
    load_spec(session_factory, FIXTURE)
    status, _, body = get(dash, "/")
    assert status == 200
    assert "VERDICT:" in body and ">STOP<" in body
    assert "AAPL" in body and "MSFT" in body
    assert "42.5 bps" in body  # median |1d| of material filings
    assert "6.1" in body       # material filings per month
    status, _, body = get(dash, "/verdict")
    assert status == 200 and "VERDICT: STOP" in body
    status, headers, body = get(dash, "/verdict.csv")
    assert status == 200 and headers["Content-Type"].startswith("text/csv")
    assert body.splitlines()[0].startswith("accession,ticker") and len(body.splitlines()) == 11


def test_untrusted_content_is_escaped(dash, session: Session) -> None:
    inst = Instrument(ticker="EVIL", figi="BBG000000000", cik="0000000001", name="<b>Evil</b> Corp",
                      added_on=date(2026, 1, 1))
    session.add(inst)
    session.flush()
    t = datetime(2026, 10, 30, 14, tzinfo=UTC)
    ev = SourceEvent(source_id="edgar", external_id="0000000001-26-000001", form_type="8-K",
                     cik=inst.cik, instrument_id=inst.id, published_at=t, first_observed_at=t, ingested_at=t,
                     url="javascript:alert(1)", items=["<script>alert(1)</script>"],
                     raw="<html><body><p>Item 8.01 <img src=x onerror=alert(2)> &lt;script&gt;x</p></body></html>")
    session.add(ev)
    session.flush()
    session.add(Label(source_event_id=ev.id, material=True, direction=Direction.up,
                      event_type=EventType.other, labeled_at=t, note="<iframe src=//evil>"))
    session.commit()
    _, _, overview = get(dash, "/")
    assert "<script>alert(1)</script>" not in overview
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in overview
    _, _, page = get(dash, f"/filing/{ev.id}")
    assert "<iframe" not in page and "&lt;iframe" in page
    assert "<b>Evil</b>" not in page and "&lt;b&gt;Evil&lt;/b&gt;" in page
    assert 'href="javascript' not in page  # non-SEC URLs are never links
    assert "<img" not in page
    assert "&lt;script&gt;x" in page  # decoded text is re-escaped, not rendered


def test_not_found(dash) -> None:
    assert get(dash, "/nope")[0] == 404
    assert get(dash, "/filing/not-a-ulid")[0] == 404
    assert get(dash, "/filing/01M6CWRH77ZGFS569FFH87TPZF")[0] == 404


def test_synthetic_demo_renders(dash, session_factory) -> None:
    spec = synthetic_spec(seed=3)
    load_spec(session_factory, spec)
    status, _, body = get(dash, "/")
    assert status == 200 and "no clean stop" in body  # the demo includes one crashed run
    assert 'href="https://demo.invalid' not in body


def test_probe_reports_down_service() -> None:
    h = probe("EDGAR poller", "http://127.0.0.1:1/healthz", timeout=0.2)
    assert (h.state, h.detail) == ("down", "not running")


def test_price_format() -> None:
    assert px(Decimal("99.990000")) == "99.99"
    assert px(Decimal("100.000000")) == "100.00"
    assert px(Decimal("0.123400")) == "0.1234"
    assert px(None) == ""


def test_demo_rebuild_refuses_other_databases() -> None:
    with pytest.raises(RuntimeError):
        recreate_demo_database("postgresql+psycopg://nta:nta@127.0.0.1:55432/nta_demo")


def test_only_weekend_filings_render(dash, session: Session) -> None:
    inst = Instrument(ticker="PG", figi="BBG000BR2TH3", cik="0000080424", name="P&G", added_on=date(2026, 1, 1))
    session.add(inst)
    session.flush()
    sunday = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    session.add(SourceEvent(source_id="edgar", external_id="0001628280-26-068591", form_type="8-K",
                            cik=inst.cik, instrument_id=inst.id, published_at=sunday, first_observed_at=sunday,
                            ingested_at=sunday, url="https://www.sec.gov/x"))
    session.commit()
    status, _, body = get(dash, "/")
    assert status == 200 and "2026-11-01" in body
