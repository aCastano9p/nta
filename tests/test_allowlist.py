import json
from dataclasses import dataclass
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nta import allowlist
from nta.allowlist import (
    CsvRow,
    OpenFigiClient,
    active_on,
    check_cik,
    choose_figi,
    openfigi_job,
    parse_csv,
)
from nta.ratelimit import openfigi_bucket
from nta.schema import Instrument, OpenFigiResponse
from tests.fakes import FakeClock

D = date(2026, 10, 1)


def _equity(figi: str, name: str, sec_type: str = "Common Stock", composite: str | None = None):
    return {"figi": figi, "name": name, "exchCode": "US", "compositeFIGI": composite or figi,
            "marketSector": "Equity", "securityType2": sec_type}


FIGI_DB = {
    "AAPL": {"data": [_equity("BBG000B9XRY4", "APPLE INC")]},
    "MSFT": {"data": [_equity("BBG000BPH459", "MICROSOFT CORP")]},
    "BRK/B": {"data": [_equity("BBG000DWG505", "BERKSHIRE HATHAWAY INC-CL B")]},
    "SPY": {"data": [_equity("BBG000BDTBL9", "SPDR S&P 500", sec_type="ETP")]},
    "AMBIG": {"data": [_equity("BBG000000001", "A"), _equity("BBG000000002", "B")]},
    "ERR": {"error": "Invalid idValue"},
}


class FakeOpenFigi:
    def __init__(self) -> None:
        self.requests: list[list[dict]] = []
        self.keys: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content)
        keyed = "X-OPENFIGI-APIKEY" in request.headers
        assert len(jobs) <= (100 if keyed else 10)
        self.keys.append(request.headers.get("X-OPENFIGI-APIKEY"))
        self.requests.append(jobs)
        out = [FIGI_DB.get(j["idValue"], {"warning": "No identifier found."}) for j in jobs]
        return httpx.Response(200, json=out)


@pytest.fixture
def fake_figi():
    return FakeOpenFigi()


@pytest.fixture
def figi_client(fake_figi: FakeOpenFigi, session_factory: sessionmaker[Session]) -> OpenFigiClient:
    clock = FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(fake_figi.handler))
    return OpenFigiClient(http, openfigi_bucket(clock), clock, session_factory)


# ---------- pure ----------

def test_parse_csv_pads_cik_and_rejects_bad_rows() -> None:
    text = ("Ticker,Name,CIK,extra\n"
            "aapl,Apple Inc.,320193,x\n"
            "msft,,789019,\n"
            "BAD TICKER,Foo,1,\n"
            "XOM,Exxon,12ab,\n"
            "AAPL,Apple again,320193,\n")
    rows, rejected = parse_csv(text)
    assert rows == [CsvRow(2, "AAPL", "Apple Inc.", "0000320193")]
    assert [(r.line, r.reason) for r in rejected] == [
        (3, "missing_name"), (4, "invalid_ticker"), (5, "invalid_cik"), (6, "duplicate_in_csv")]


def test_parse_csv_requires_columns() -> None:
    with pytest.raises(ValueError, match="cik"):
        parse_csv("ticker,name\nAAPL,Apple\n")


def test_openfigi_job_share_class() -> None:
    assert openfigi_job("BRK.B")["idValue"] == "BRK/B"
    assert openfigi_job("BRK-B")["idValue"] == "BRK/B"


@pytest.mark.parametrize("item,expected", [
    (FIGI_DB["AAPL"], ("BBG000B9XRY4", None)),
    ({"warning": "No identifier found."}, (None, "figi_not_found")),
    (FIGI_DB["SPY"], (None, "figi_not_common_stock")),
    (FIGI_DB["AMBIG"], (None, "figi_ambiguous")),
    (FIGI_DB["ERR"], (None, "openfigi_error")),
    # Several listings sharing one composite FIGI is not ambiguous.
    ({"data": [_equity("BBG1", "X", composite="BBGC"), _equity("BBGC", "X")]}, ("BBGC", None)),
])
def test_choose_figi(item: dict, expected: tuple) -> None:
    c = choose_figi(item)
    assert (c.figi, c.reason) == expected


def test_check_cik() -> None:
    row = CsvRow(2, "BRK.B", "Berkshire", "0001067983")
    assert check_cik(row, {"BRK-B": "0001067983"}) is None
    assert check_cik(row, {"BRK-B": "0000000001"}).reason == "cik_mismatch"
    assert check_cik(row, {}).reason == "ticker_not_in_sec_list"


@dataclass
class R:
    id: str
    figi: str
    added_on: date
    removed_on: date | None = None
    ticker: str = "T"


def test_active_on_intervals() -> None:
    rows = [
        R("01", "F1", date(2026, 1, 1)),                              # added
        R("02", "F1", date(2026, 1, 1), removed_on=date(2026, 3, 1)),  # removed Mar 1
        R("03", "F1", date(2026, 6, 1)),                              # re-added Jun 1
        R("04", "F2", date(2026, 2, 1)),
    ]
    figis = lambda d: sorted(r.figi for r in active_on(rows, d))  # noqa: E731
    assert figis(date(2025, 12, 31)) == []
    assert figis(date(2026, 1, 15)) == ["F1"]
    assert figis(date(2026, 2, 28)) == ["F1", "F2"]
    assert figis(date(2026, 3, 1)) == ["F2"]          # removal day is exclusive
    assert figis(date(2026, 6, 1)) == ["F1", "F2"]
    assert [r.id for r in active_on(rows, date(2026, 6, 2)) if r.figi == "F1"] == ["03"]


def test_active_on_same_day_remove_and_readd() -> None:
    d = date(2026, 11, 1)
    rows = [
        R("01", "F1", d),                    # added
        R("02", "F1", d, removed_on=d),      # removed the same day (empty interval)
        R("03", "F1", d),                    # re-added the same day
    ]
    assert [r.id for r in active_on(rows, d)] == ["03"]
    assert active_on(rows[:2], d) == []


# ---------- with DB ----------

CSV = ("ticker,name,cik\n"
       "AAPL,Apple Inc.,320193\n"
       "MSFT,Microsoft Corp,789019\n"
       "BRK.B,Berkshire Hathaway B,1067983\n"
       "SPY,SPDR S&P 500,884394\n"
       "NOPE,Unknown Co,1\n")


def test_load_stores_resolved_and_rejects_unresolved(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient, session: Session
) -> None:
    report = allowlist.load(session_factory, CSV, figi_client, D)
    assert report.added == ["AAPL", "MSFT", "BRK.B"]
    assert {(r.ticker, r.reason) for r in report.rejected} == {
        ("SPY", "figi_not_common_stock"), ("NOPE", "figi_not_found")}
    stored = {i.ticker: i for i in session.scalars(select(Instrument))}
    assert set(stored) == {"AAPL", "MSFT", "BRK.B"}
    assert stored["BRK.B"].figi == "BBG000DWG505"
    assert stored["AAPL"].cik == "0000320193"
    assert stored["AAPL"].aliases == ["APPLE INC", "Apple Inc."]


def test_reload_is_idempotent_and_uses_cache(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient,
    fake_figi: FakeOpenFigi, session: Session
) -> None:
    allowlist.load(session_factory, CSV, figi_client, D)
    assert len(fake_figi.requests) == 1
    report = allowlist.load(session_factory, CSV, figi_client, date(2026, 10, 2))
    assert report.added == []
    assert report.already_present == ["AAPL", "MSFT", "BRK.B"]
    assert len(fake_figi.requests) == 1  # all answers (incl. "not found") came from cache
    assert session.scalar(select(func.count()).select_from(Instrument)) == 3
    assert session.scalar(select(func.count()).select_from(OpenFigiResponse)) == 5


def test_errors_are_not_cached(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient, fake_figi: FakeOpenFigi
) -> None:
    csv_text = "ticker,name,cik\nERR,Err Co,1\n"
    allowlist.load(session_factory, csv_text, figi_client, D)
    allowlist.load(session_factory, csv_text, figi_client, D)
    assert len(fake_figi.requests) == 2


def test_batches_of_ten(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient, fake_figi: FakeOpenFigi
) -> None:
    lines = "".join(f"T{i},Co {i},{i + 1}\n" for i in range(23))
    allowlist.load(session_factory, "ticker,name,cik\n" + lines, figi_client, D)
    assert [len(r) for r in fake_figi.requests] == [10, 10, 3]


def test_cik_mismatch_rejected_before_figi_lookup(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient, fake_figi: FakeOpenFigi
) -> None:
    sec = {"AAPL": "0000320193", "MSFT": "0000000042"}
    report = allowlist.load(session_factory, "ticker,name,cik\nAAPL,Apple,320193\nMSFT,MS,789019\n",
                            figi_client, D, sec_ciks=sec)
    assert report.added == ["AAPL"]
    assert [(r.ticker, r.reason) for r in report.rejected] == [("MSFT", "cik_mismatch")]
    assert [j["idValue"] for j in fake_figi.requests[0]] == ["AAPL"]


def test_remove_then_readd(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient, session: Session
) -> None:
    allowlist.load(session_factory, CSV, figi_client, D)
    assert allowlist.remove(session_factory, "msft", date(2026, 10, 5))
    assert not allowlist.remove(session_factory, "MSFT", date(2026, 10, 6))
    tickers = lambda d: [i.ticker for i in allowlist.current(session, d)]  # noqa: E731
    assert "MSFT" in tickers(date(2026, 10, 4))
    assert "MSFT" not in tickers(date(2026, 10, 5))
    report = allowlist.load(session_factory, CSV, figi_client, date(2026, 10, 7))
    assert report.added == ["MSFT"]
    assert "MSFT" in tickers(date(2026, 10, 7))
    assert "MSFT" not in tickers(date(2026, 10, 6))


def test_remove_and_readd_same_day(
    session_factory: sessionmaker[Session], figi_client: OpenFigiClient, session: Session
) -> None:
    allowlist.load(session_factory, CSV, figi_client, D)
    assert allowlist.remove(session_factory, "AAPL", D)
    assert "AAPL" not in [i.ticker for i in allowlist.current(session, D)]
    report = allowlist.load(session_factory, CSV, figi_client, D)
    assert report.added == ["AAPL"]
    assert "AAPL" in [i.ticker for i in allowlist.current(session, D)]


def test_api_key_raises_batch_size(fake_figi: FakeOpenFigi, session_factory: sessionmaker[Session]) -> None:
    clock = FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(fake_figi.handler))
    client = OpenFigiClient(http, openfigi_bucket(clock, keyed=True), clock, session_factory, api_key="k123")
    lines = "".join(f"T{i},Co {i},{i + 1}\n" for i in range(230))
    allowlist.load(session_factory, "ticker,name,cik\n" + lines, client, D)
    assert [len(r) for r in fake_figi.requests] == [100, 100, 30]
    assert fake_figi.keys == ["k123"] * 3


def test_keyed_bucket_respects_25_per_6_seconds() -> None:
    from tests.test_ratelimit_http import max_in_window
    clock = FakeClock()
    bucket = openfigi_bucket(clock, keyed=True)
    times = []
    for _ in range(100):
        bucket.acquire()
        times.append(clock.monotonic())
    assert max_in_window(times, 6.0) <= 25
    assert times[-1] < 30  # vs ~4.5 minutes without a key
