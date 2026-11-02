import hashlib
import json
import urllib.error
import urllib.request
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from nta.edgar import EdgarPoller
from nta.health import HealthServer
from nta.ratelimit import sec_bucket
from nta.runs import start_run, stop_run
from nta.schema import Instrument, Run, RunStop, SourceEvent
from tests.edgar_fakes import E, FakeSec, acc, minutes_before
from tests.fakes import FakeClock
from tests.test_ratelimit_http import max_in_window

UA = "Test Runner test@example.com"
AAPL_CIK = "0000320193"
MSFT_CIK = "0000789019"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sec(clock: FakeClock) -> FakeSec:
    return FakeSec(clock)


@pytest.fixture
def instruments(session: Session) -> dict[str, Instrument]:
    rows = {
        "AAPL": Instrument(ticker="AAPL", figi="BBG000B9XRY4", cik=AAPL_CIK, name="Apple",
                           added_on=date(2026, 1, 1)),
        "MSFT": Instrument(ticker="MSFT", figi="BBG000BPH459", cik=MSFT_CIK, name="Microsoft",
                           added_on=date(2026, 1, 1)),
    }
    session.add_all(rows.values())
    session.commit()
    return rows


def make_poller(sec: FakeSec, clock: FakeClock, sf: sessionmaker[Session],
                run_id: str | None = None) -> EdgarPoller:
    http = httpx.Client(transport=httpx.MockTransport(sec.handler))
    return EdgarPoller(http=http, bucket=sec_bucket(clock), clock=clock, session_factory=sf,
                       user_agent=UA, run_id=run_id)


def standard_feed(clock: FakeClock) -> list[E]:
    t = clock.now()
    return [
        E(acc(1), AAPL_CIK, minutes_before(t, 1), company="Apple Inc.", items=("2.02", "9.01")),
        E(acc(2), "0000000042", minutes_before(t, 2)),                       # not allowlisted
        E(acc(3), "0000000043", minutes_before(t, 3)),                       # co-registrant pair:
        E(acc(3), MSFT_CIK, minutes_before(t, 3), form="8-K"),               # 2nd CIK allowlisted
        E(acc(4), AAPL_CIK, minutes_before(t, 4), form="8-K/A"),             # amendment accepted
        E(acc(5), AAPL_CIK, minutes_before(t, 5), form="8-K12B"),            # other form ignored
    ]


def rows(session: Session) -> list[SourceEvent]:
    session.expire_all()
    return list(session.scalars(select(SourceEvent).order_by(SourceEvent.external_id)))


def test_stores_allowlisted_filings(sec, clock, session_factory, session, instruments, capsys):
    sec.set_feed(standard_feed(clock))
    observed_at = clock.now()
    stats = make_poller(sec, clock, session_factory, run_id=None).poll_once()

    stored = rows(session)
    assert [r.external_id for r in stored] == [acc(1), acc(3), acc(4)]
    assert (stats.stored, stats.not_allowlisted, stats.other_form) == (3, 1, 1)
    aapl = stored[0]
    assert aapl.instrument_id == instruments["AAPL"].id
    assert aapl.form_type == "8-K"
    assert aapl.first_observed_at == observed_at
    assert aapl.published_at == observed_at - timedelta(minutes=1)
    assert aapl.items == ["2.02", "9.01", "8.01"]  # feed items, then any extra from the index
    assert aapl.primary_doc_url.endswith(f"/{acc(1)}-doc.htm")
    assert "/ix?doc=" not in aapl.primary_doc_url
    assert "<script>alert(1)</script>" in aapl.raw  # stored verbatim, never rendered
    assert aapl.raw_hash == hashlib.sha256(aapl.raw.encode()).hexdigest()
    msft = stored[1]
    assert (msft.cik, msft.instrument_id) == (MSFT_CIK, instruments["MSFT"].id)
    assert stored[2].form_type == "8-K/A"

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    latency = {r["accession"]: r["poll_latency_s"] for r in logs if r["event"] == "filing_stored"}
    assert latency == {acc(1): 60.0, acc(3): 180.0, acc(4): 240.0}


def test_reingesting_same_feed_is_noop(sec, clock, session_factory, session, instruments):
    sec.set_feed(standard_feed(clock))
    poller = make_poller(sec, clock, session_factory)
    poller.poll_once()
    before = rows(session)
    index_fetches = sec.count("-index.htm")

    clock.advance(5)
    stats = poller.poll_once()

    assert [(r.id, r.first_observed_at) for r in rows(session)] == \
        [(r.id, r.first_observed_at) for r in before]
    assert stats.stored == 0
    assert stats.duplicate_seen == 5  # every accession in the feed (5 distinct) was seen before
    assert poller.duplicate_seen_total == 5
    assert sec.count("-index.htm") == index_fetches  # no re-fetching


def test_restart_never_restamps_first_observed_at(sec, clock, session_factory, session, instruments):
    sec.set_feed(standard_feed(clock))
    run_a = start_run(session_factory, "poll_edgar", {"x": 1}, clock, sha="test")
    make_poller(sec, clock, session_factory, run_id=run_a).poll_once()
    stamped = {r.external_id: r.first_observed_at for r in rows(session)}
    # Simulated crash: no run_stops row for run A. Restart an hour later with fresh memory.
    clock.advance(3600)
    run_b = start_run(session_factory, "poll_edgar", {"x": 1}, clock, sha="test")
    poller_b = make_poller(sec, clock, session_factory, run_id=run_b)
    index_fetches = sec.count("-index.htm")
    stats = poller_b.poll_once()

    after = rows(session)
    assert {r.external_id: r.first_observed_at for r in after} == stamped
    assert {r.run_id for r in after} == {run_a}
    assert stats.stored == 0
    assert stats.duplicate_seen == 3  # the 3 stored accessions are known from the DB
    assert sec.count("-index.htm") == index_fetches


def test_concurrent_insert_is_a_duplicate_not_an_error(sec, clock, session_factory, session, instruments):
    sec.set_feed(standard_feed(clock)[:1])
    a = make_poller(sec, clock, session_factory)
    b = make_poller(sec, clock, session_factory)  # same start state: neither has seen acc(1)
    assert a.poll_once().stored == 1
    stats = b.poll_once()
    assert (stats.stored, stats.duplicate_seen) == (0, 1)
    assert len(rows(session)) == 1


def test_document_fetch_failure_still_stores_the_observation(sec, clock, session_factory, session, instruments):
    sec.set_feed(standard_feed(clock)[:1])
    sec.fail["-doc.htm"] = [404]
    observed_at = clock.now()
    stats = make_poller(sec, clock, session_factory).poll_once()
    (row,) = rows(session)
    assert stats.stored == 1
    assert row.raw is None and row.raw_hash is None
    assert row.first_observed_at == observed_at


def test_feed_429_backs_off_then_succeeds(sec, clock, session_factory, session, instruments):
    sec.set_feed(standard_feed(clock)[:1])
    sec.fail["browse-edgar"] = [429, 503]
    stats = make_poller(sec, clock, session_factory).poll_once()
    assert stats.stored == 1
    assert sec.count("browse-edgar") == 3
    assert clock.sleeps  # backed off


def test_burst_of_filings_stays_within_sec_rate(sec, clock, session_factory, session, instruments):
    t = clock.now()
    sec.set_feed([E(acc(i), AAPL_CIK, minutes_before(t, i)) for i in range(1, 16)])
    sec.fail["-index.htm"] = [429, 429, 500]  # retries in the middle of the burst
    stats = make_poller(sec, clock, session_factory).poll_once()
    assert stats.stored == 15
    times = [t for _, t in sec.requests]
    assert len(times) == 1 + 15 * 2 + 3
    assert max_in_window(times, 1.0) <= 10


def test_backfill_pages_when_feed_may_have_a_gap(sec, clock, session_factory, session, instruments):
    t = clock.now()
    # Stored earlier: an AAPL filing from 3 hours ago (the resume anchor).
    sec.set_feed([E(acc(9000), AAPL_CIK, t - timedelta(hours=3))])
    make_poller(sec, clock, session_factory).poll_once()
    # Since then 150 filings arrived; page 1 holds the newest 100, page 2 the rest.
    newer = [E(acc(i), "0000000042", t - timedelta(minutes=i)) for i in range(1, 151)]
    newer[120] = E(acc(121), MSFT_CIK, t - timedelta(minutes=121))  # only on page 2
    sec.set_feed(newer[:100], start=0)
    sec.set_feed(newer[100:] + [E(acc(9000), AAPL_CIK, t - timedelta(hours=3))], start=100)

    poller = make_poller(sec, clock, session_factory)  # restart: memory empty
    stats = poller.poll_once()
    assert stats.pages == 2
    assert acc(121) in {r.external_id for r in rows(session)}

    clock.advance(5)  # steady state: page 1 overlaps what we saw, no extra page
    assert poller.poll_once().pages == 1


def test_no_backfill_on_empty_database(sec, clock, session_factory, instruments):
    t = clock.now()
    sec.set_feed([E(acc(i), "0000000042", t - timedelta(minutes=i)) for i in range(1, 101)])
    assert make_poller(sec, clock, session_factory).poll_once().pages == 1


def test_allowlist_is_point_in_time(sec, clock, session_factory, session, instruments):
    # MSFT removed yesterday (New York date) -> its filing is not stored today.
    m = instruments["MSFT"]
    session.add(Instrument(ticker="MSFT", figi=m.figi, cik=m.cik, name=m.name,
                           added_on=m.added_on, removed_on=date(2026, 9, 30)))
    session.commit()
    t = clock.now()
    sec.set_feed([E(acc(1), MSFT_CIK, t)])
    stats = make_poller(sec, clock, session_factory).poll_once()
    assert (stats.stored, stats.not_allowlisted) == (0, 1)


def test_healthz_reports_seconds_since_last_success(sec, clock, session_factory, instruments):
    sec.set_feed([])
    poller = make_poller(sec, clock, session_factory)
    server = HealthServer(poller.health, port=0, stale_after_s=60).start()
    url = f"http://127.0.0.1:{server.port}/healthz"
    try:
        with pytest.raises(urllib.error.HTTPError) as err:  # never polled yet -> 503
            urllib.request.urlopen(url, timeout=5)
        assert err.value.code == 503
        poller.poll_once()
        clock.advance(12.5)
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read())
        assert resp.status == 200
        assert body["seconds_since_last_success"] == 12.5
        assert body["ok"] is True
        clock.advance(60)
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url, timeout=5)
        assert err.value.code == 503
    finally:
        server.stop()


def test_run_rows(session_factory, session, clock):
    run_id = start_run(session_factory, "poll_edgar", {"interval_s": 5}, clock, sha="abc")
    stop_run(session_factory, run_id, "signal SIGTERM", clock)
    run = session.get(Run, run_id)
    assert run.process == "poll_edgar" and run.code_sha == "abc" and len(run.config_hash) == 64
    assert session.scalar(select(func.count()).select_from(RunStop)
                          .where(RunStop.run_id == run_id)) == 1


def test_cli_refuses_to_start_without_sec_user_agent() -> None:
    from nta.cli import app
    result = CliRunner().invoke(app, ["poll", "edgar", "--once"], env={"SEC_USER_AGENT": ""})
    assert result.exit_code == 2
    assert "SEC_USER_AGENT" in result.output


def test_cli_rejects_malformed_sec_user_agent() -> None:
    from nta.cli import app
    result = CliRunner().invoke(app, ["poll", "edgar", "--once"],
                                env={"SEC_USER_AGENT": "alejo@example.com"})
    assert result.exit_code == 2
    assert "<name> <email>" in result.output
