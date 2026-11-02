import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from typer.testing import CliRunner

from nta.agent import source_meta_for
from nta.cli import app
from nta.schema import Decision, Outcome, Signal, SourceEvent, XPoll
from nta.xpoll import XPoller, x_bucket
from nta.xsource import Registry, cashtags, map_instrument
from tests.agent_fakes import MARKET_T, add_equity, add_filing, add_instrument, rig
from tests.fakes import FakeClock

SRC = {"author_id": "111", "handle": "wire", "display_name": "The Wire", "class": "aggregator", "weight": "0.5",
       "event_types": ["guidance", "m_and_a"]}


def registry(*extra) -> Registry:
    return Registry.model_validate({"sources": [SRC, *extra]})


def test_registry_rules() -> None:
    assert registry().sources[0].source_class == "aggregator"
    with pytest.raises(ValidationError):
        Registry.model_validate({"sources": [{**SRC, "weight": "0.6"}]})        # > 0.5 needs primary
    Registry.model_validate({"sources": [{**SRC, "class": "primary", "weight": "1"}]})
    with pytest.raises(ValidationError):
        Registry.model_validate({"sources": [{**SRC, "author_id": "@wire"}]})   # numeric ids only


def test_cashtag_mapping() -> None:
    assert cashtags("Big day for $AAPL and $brk.b, not US$5") == {"AAPL", "BRK.B"}
    t = {"AAPL": "I1", "MSFT": "I2"}
    assert map_instrument("$AAPL raises guidance", t) == "I1"
    assert map_instrument("$AAPL vs $MSFT", t) is None and map_instrument("$ZZZZ", t) is None


class FakeX:
    def __init__(self, posts_per_call: list[list[dict]], user=None):
        self.calls: list[httpx.Request] = []
        self.pages = posts_per_call
        self.user = user or {"id": "111", "username": "wire", "name": "The Wire"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        posts = self.pages.pop(0) if self.pages else []
        body = {"data": posts, "includes": {"users": [self.user]},
                "meta": {"result_count": len(posts), **({"newest_id": posts[0]["id"]} if posts else {})}}
        return httpx.Response(200, json=body)


def post(i: int, text: str = "$AAPL raises full-year guidance") -> dict:
    return {"id": str(1900000000000000000 + i), "text": text, "author_id": "111",
            "created_at": MARKET_T.strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def poller(sf, fake, daily="3", clock=None):
    clock = clock or FakeClock(MARKET_T)
    return XPoller(sf=sf, http=httpx.Client(transport=httpx.MockTransport(fake)), bucket=x_bucket(clock), clock=clock,
                   registry=registry(), bearer="token", daily_usd=Decimal(daily))


def test_poll_stores_posts_maps_cashtags_and_bills(session, session_factory) -> None:
    inst = add_instrument(session)
    session.commit()
    fake = FakeX([[post(2), post(1, "no ticker here")], []])
    p = poller(session_factory, fake)
    p.poll_once()
    p.poll_once()
    first, second = fake.calls
    assert first.url.path == "/2/users/111/tweets" and "start_time" in first.url.params
    assert second.url.params["since_id"] == post(2)["id"] and first.headers["authorization"] == "Bearer token"
    with session_factory() as s:
        evs = {e.external_id: e for e in s.scalars(select(SourceEvent).where(SourceEvent.source_id == "x"))}
        polls = list(s.scalars(select(XPoll).order_by(XPoll.first_observed_at, XPoll.id)))
    a = evs[post(2)["id"]]
    assert a.instrument_id == inst.id and a.cik == inst.cik and a.form_type == "post"
    assert json.loads(a.raw)["username"] == "wire" and a.url == f"https://x.com/wire/status/{post(2)['id']}"
    assert evs[post(1)["id"]].instrument_id is None and evs[post(1)["id"]].cik == "0000000000"
    assert [(x.posts_returned, x.posts_billable, x.cost_usd) for x in polls] == [
        (2, 2, Decimal("0.010000")), (0, 0, Decimal("0"))]


def test_repeated_post_within_24h_is_not_billed_twice(session, session_factory) -> None:
    fake = FakeX([[post(1)], [post(1)]])
    p = poller(session_factory, fake)
    p.poll_once()
    p.poll_once()
    with session_factory() as s:
        billed = [x.posts_billable for x in s.scalars(select(XPoll).order_by(XPoll.first_observed_at, XPoll.id))]
        n = len(list(s.scalars(select(SourceEvent).where(SourceEvent.source_id == "x"))))
    assert billed == [1, 0] and n == 1


def test_daily_cap_stops_polling(session, session_factory) -> None:
    fake = FakeX([[post(i) for i in range(5)], [post(10)]])
    p = poller(session_factory, fake, daily="0.03")
    p.poll_once()            # 5 posts = 0.025; 0.005 left, less than a minimum page of 5
    assert fake.calls[0].url.params["max_results"] == "6"
    p.poll_once()
    p.poll_once()
    assert len(fake.calls) == 1
    with session_factory() as s:
        statuses = [x.status for x in s.scalars(select(XPoll).order_by(XPoll.first_observed_at, XPoll.id))]
    assert statuses == ["ok", "budget_skipped"]
    assert p.health()["spent_today_usd"] == "0.025000"


def test_x_is_off_by_default() -> None:
    res = CliRunner().invoke(app, ["poll", "x", "--once"], env={"X_ENABLED": "false"})
    assert res.exit_code == 0 and "disabled" in res.output


def x_event(session, inst, observed, text, *, username="wire", name="The Wire", n=1):
    raw = json.dumps({"id": str(n), "text": text, "author_id": "111", "username": username, "name": name,
                      "created_at": observed.isoformat()})
    ev = SourceEvent(source_id="x", external_id=str(n), form_type="post", cik=inst.cik, instrument_id=inst.id,
                     published_at=observed, first_observed_at=observed, ingested_at=observed,
                     url=f"https://x.com/{username}/status/{n}", raw=raw, raw_hash="0" * 64)
    session.add(ev)
    session.commit()
    return ev


def decide_one(session_factory, reg):
    r = rig(session_factory)
    r.pipeline.source_meta = source_meta_for(reg)
    r.pipeline.step(None)
    with session_factory() as s:
        return s.scalars(select(Signal)).all(), s.scalars(select(Decision)).all()


def test_lone_aggregator_post_is_uncorroborated(session, session_factory) -> None:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    x_event(session, inst, MARKET_T - timedelta(seconds=5), "$AAPL raises full-year guidance")
    [sig], [dec] = decide_one(session_factory, registry())
    assert (sig.origin, sig.source_class, sig.source_weight) == ("x:111", "aggregator", Decimal("0.500"))
    assert not sig.corroborated and dec.outcome == Outcome.abstain and "uncorroborated" in dec.reasons


def test_changed_handle_is_quarantined(session, session_factory) -> None:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    x_event(session, inst, MARKET_T - timedelta(seconds=5), "$AAPL raises guidance", username="wire_official")
    [sig], _ = decide_one(session_factory, registry())
    assert sig.source_weight == 0 and "source_quarantined" in sig.flags


def test_event_type_outside_the_allowed_list_never_trades(session, session_factory) -> None:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=1))
    x_event(session, inst, MARKET_T - timedelta(seconds=5), "$AAPL raises guidance")
    reg = Registry.model_validate({"sources": [{**SRC, "class": "primary", "weight": "1", "event_types": ["legal"]}]})
    [sig], [dec] = decide_one(session_factory, reg)
    assert sig.corroborated and "event_type_not_allowed" in sig.flags and dec.reasons == ["source_not_allowed"]


def test_post_citing_a_filing_joins_its_cluster(session, session_factory) -> None:
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(minutes=2))
    filing = add_filing(session, inst, MARKET_T - timedelta(minutes=1))
    session.commit()
    x_event(session, inst, MARKET_T - timedelta(seconds=5), f"Apple 8-K out: {filing.external_id} big guidance raise")
    sigs, decs = decide_one(session_factory, registry())
    post_sig = [x for x in sigs if x.origin == "x:111"][0]
    assert post_sig.cluster_id == filing.id and post_sig.is_repeat and post_sig.corroborated
    assert post_sig.corroboration == 2
    post_dec = [d for d in decs if d.source_event_id == post_sig.source_event_id][0]
    assert post_dec.outcome == Outcome.abstain and "repeat" in post_dec.reasons
