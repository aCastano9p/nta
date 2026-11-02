"""The verdict must reproduce answers worked out by hand from the fixture.

Fixture (tests/fixtures/verdict/fixture.json): 10 filings, Oct 1-30 2026 (30 days), all
labeled, 6 material. Hand calculation (linear-interpolated percentiles):
  material |1d| bps: 10 20 25 60 80 150 -> median 42.5, IQR 21.25-75.0
  material |1h| bps: 5 10 20 40 50 100  -> median 30.0, IQR 12.5-47.5
  material |5d| bps: 15 30 100 120 200 (filing 6's 5d mark was recorded late) -> 100 [30-120]
  all |1d|: 1 3 4 10 10 20 25 60 80 150 -> median 15.0
  guidance |1d|: 80 150 -> 115.0; up: 20 80 150 -> 80.0; down: 25 60 -> 42.5
  latency s: 10 12 15 18 20 22 25 30 40 60 -> p50 21.0, p95 40 + 0.55*20 = 51.0
  in-session filings 9 (filing 7 is after hours), observe > 5 s late: 1 (filing 3)
  material per month = 10 x 0.6 x 30.4375 / 30 = 6.0875 < 60 -> STOP
"""

import csv
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from nta.demo import load_spec
from nta.schema import QuoteKind
from nta.verdict import (
    QuotePoint,
    Record,
    compute,
    format_report,
    load_records,
    pct,
    price_and_source,
    run,
    wilson,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "verdict" / "fixture.json").read_text())
START, END = date(2026, 10, 1), date(2026, 10, 30)


@pytest.fixture
def report_and_records(session_factory: sessionmaker[Session], session: Session):
    load_spec(session_factory, FIXTURE)
    records = load_records(session, START, END)
    return compute(records, START, END), records


def group(report, name):
    return dict(report.groups)[name]


def test_counts_and_rate(report_and_records) -> None:
    r, _ = report_and_records
    assert (r.days, r.n_filings, r.n_labeled, r.n_material) == (30, 10, 10, 6)
    assert r.n_by_form == {"8-K": 10}
    assert r.share == pytest.approx(0.6)
    assert r.per_month == pytest.approx(6.0875)
    lo, hi = r.share_ci
    assert (lo, hi) == (pytest.approx(0.3127, abs=1e-4), pytest.approx(0.8318, abs=1e-4))


def test_move_statistics(report_and_records) -> None:
    r, _ = report_and_records
    m = group(r, "material")
    assert (m["1d"].n, m["1d"].median, m["1d"].q1, m["1d"].q3) == (6, pytest.approx(42.5), pytest.approx(21.25), pytest.approx(75.0))
    assert (m["1h"].median, m["1h"].q1, m["1h"].q3) == (pytest.approx(30.0), pytest.approx(12.5), pytest.approx(47.5))
    assert (m["5d"].n, m["5d"].median, m["5d"].q1, m["5d"].q3) == (5, pytest.approx(100.0), pytest.approx(30.0), pytest.approx(120.0))
    assert group(r, "all filings")["1d"].median == pytest.approx(15.0)
    assert group(r, "material, guidance")["1d"].median == pytest.approx(115.0)
    assert group(r, "material, up")["1d"].median == pytest.approx(80.0)
    assert group(r, "material, down")["1d"].median == pytest.approx(42.5)
    assert group(r, "material, unclear")["1d"].median == pytest.approx(10.0)
    assert r.excluded_late == {"1h": 0, "1d": 0, "5d": 1}
    assert r.missing == {"1h": 0, "1d": 0, "5d": 1}


def test_latency_and_observe_timeliness(report_and_records) -> None:
    r, _ = report_and_records
    assert r.latency_p50 == pytest.approx(21.0)
    assert r.latency_p95 == pytest.approx(51.0)
    assert (r.n_in_session, r.n_observe_late, r.n_observe_missing, r.n_after_hours) == (9, 1, 0, 1)


def test_verdict_line(report_and_records) -> None:
    r, _ = report_and_records
    assert r.material_median_1d == pytest.approx(42.5)
    assert r.verdict == "STOP"
    assert r.reasons == ["material filings per month 6.1 < 60"]
    text = format_report(r)
    assert text.splitlines()[-1] == "VERDICT: STOP"
    assert "material                    30.0 [12.5-47.5] (6)" in text
    assert "p50 21.0 s, p95 51.0 s (n=10)" in text
    assert "1 of 9 filings observed during market hours (11.1%)" in text


def test_csv_export(session_factory, session, tmp_path) -> None:
    load_spec(session_factory, FIXTURE)
    out = tmp_path / "v.csv"
    text, _ = run(session, START, END, out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 10 and str(out) in text
    first = rows[0]
    assert (first["ticker"], first["move_1d_bps"], first["pre_filing_price"], first["material"]) == \
        ("AAPL", "80.0", "99.5000", "True")
    sixth = rows[5]
    assert sixth["move_5d_bps"] == "" and sixth["price_5d"] != ""  # late mark: price kept, move not used
    eighth = rows[7]
    assert eighth["observe_price"] == "100.0000"  # bid/ask missing -> last


def test_empty_database(session) -> None:
    text, report = run(session, None, None, None)
    assert report is None and text.endswith("VERDICT: STOP")


# ---------- pure ----------

T = datetime(2026, 10, 1, 14, tzinfo=UTC)


def rec(i: int, material: bool | None, d1: float, *, direction="up", event_type="guidance") -> Record:
    obs = QuotePoint(Decimal(100), T, T)
    marks = {QuoteKind.mark_1d: QuotePoint(Decimal(100) * (1 + Decimal(str(d1)) / 10000), T, T)}
    return Record(str(i), f"acc{i}", "AAPL", "8-K", (), T - timedelta(seconds=10), T, True, obs, marks,
                  material=material, direction=direction if material else None,
                  event_type=event_type if material is not None else None)


def test_continue_when_both_conditions_pass() -> None:
    # 100 filings in 30 days, 80 labeled, 60 material -> 100 x 0.75 x 30.4375/30 = 76.1/month.
    recs = [rec(i, True, 45.0 if i % 2 else -50.0) for i in range(60)]
    recs += [rec(100 + i, False, 5.0) for i in range(20)] + [rec(200 + i, None, 1.0) for i in range(20)]
    r = compute(recs, START, END)
    assert r.per_month == pytest.approx(76.09375)
    assert r.material_median_1d == pytest.approx(47.5)
    assert (r.verdict, r.reasons) == ("CONTINUE", [])


def test_stop_when_moves_too_small() -> None:
    recs = [rec(i, True, 12.0) for i in range(200)]
    r = compute(recs, START, END)
    assert r.verdict == "STOP"
    assert r.reasons == ["median |1d move| of material filings 12.0 bps < 30 bps"]


def test_pct_and_wilson() -> None:
    assert pct([], 0.5) is None
    assert pct([7.0], 0.95) == 7.0
    assert pct([1, 2, 3, 4], 0.5) == 2.5
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)


@pytest.mark.parametrize("bid,ask,last,expected", [
    ("99.99", "100.01", "100.00", ("100.00", "mid")),     # 2 bps: tight
    ("99.90", "100.10", "100.05", ("100.00", "mid")),     # exactly 20 bps: still mid
    ("323.89", "341.20", "341.18", ("341.18", "last")),   # JPM seen live: 520 bps -> last trade
    ("99.00", "101.00", None, (None, None)),              # wide and no trade: no price
    (None, "100.01", "100.00", ("100.00", "last")),       # one side missing
    ("101.00", "100.00", "100.50", ("100.50", "last")),   # crossed
    (None, None, None, (None, None)),
])
def test_price_rule(bid, ask, last, expected) -> None:
    d = lambda v: None if v is None else Decimal(v)  # noqa: E731
    price, source = price_and_source(d(bid), d(ask), d(last))
    assert (None if price is None else f"{price:.2f}", source) == expected


def test_wide_quotes_are_priced_from_last_trade(session_factory, session, tmp_path) -> None:
    spec = json.loads(json.dumps(FIXTURE))
    spec["filings"][0]["quote_style"] = "wide"  # filing 1: every quote has a 600 bps lopsided spread
    load_spec(session_factory, spec)
    records = load_records(session, START, END)
    first = next(r for r in records if r.accession.endswith("000001"))
    assert first.observe.source == "last"
    assert first.move_bps(QuoteKind.mark_1d) == pytest.approx(80.0)  # the mid would give ~81.6
    report = compute(records, START, END)
    assert report.material_median_1d == pytest.approx(42.5)  # unchanged hand-checked answer
    assert report.price_sources == {"mid": 31, "last": 8}  # 39 quotes; filing 8 last-only, filing 1 wide
    out = tmp_path / "v.csv"
    run(session, START, END, out)
    row = next(csv.DictReader(out.open()))
    assert (row["observe_price_source"], row["price_1d_source"]) == ("last", "last")
