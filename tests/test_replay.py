import os
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from nta.extract import CacheMiss, ModelConfig
from nta.policy import DEFAULT_POLICY_PATH, load_policy, parse_policy
from nta.replay import create_replay_schema, format_paths, format_result, run_replay, set_search_path
from nta.schema import Decision, LlmCall, Signal, SourceEvent
from tests import replay_scenario as sc

SCHEMA = "replay_test01"


def replay(connection, session_factory, *, schema=SCHEMA, policy=None, simulate=False, bars=None, day=sc.DAY):
    create_replay_schema(connection, schema)
    set_search_path(connection, schema)
    try:
        return run_replay(session_factory, run_id=schema[7:], schema=schema, start=day, end=day,
                          policy=policy or load_policy(), models=ModelConfig(), simulate=simulate, bars=bars)
    finally:
        set_search_path(connection, None)


def live_counts(sf):
    with sf() as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m)) for m in (Decision, Signal, LlmCall)}


@pytest.mark.skipif(not os.environ.get("NTA_REGEN_GOLDEN"), reason="set NTA_REGEN_GOLDEN=1 to rewrite the golden log")
def test_regenerate_golden_log(session, session_factory) -> None:
    sc.run_live_scenario(session, session_factory)
    sc.dump(session_factory)


def test_golden_log_replays_bit_identical(connection, session_factory) -> None:
    data = sc.load(session_factory)
    live = {(d["kind"], d["source_event_id"]): d["content_hash"] for d in data["decisions"]}
    assert len(live) == 7 and sum(1 for k, _ in live if k == "close") == 1
    before = live_counts(session_factory)
    r = replay(connection, session_factory)
    assert r.comparison is not None and r.comparison.identical, format_result(r)
    assert r.comparison.matched == 7
    assert {(c.kind, c.source_event_id): c.content_hash for c in r.decisions} == live
    assert r.quotes_used == {"decision": 4, "periodic": 0, "bar": 0, "none": 0}
    assert live_counts(session_factory) == before               # nothing written to live tables
    outcomes = sorted((c.kind, c.outcome.value, c.reasons) for c in r.decisions)
    assert ("entry", "trade", ()) in outcomes and ("close", "trade", ("stop",)) in outcomes
    assert ("entry", "abstain", ("long_only",)) in outcomes and ("entry", "abstain", ("repeat",)) in outcomes
    assert ("entry", "abstain", ("not_material",)) in outcomes and ("entry", "abstain", ("llm_invalid",)) in outcomes
    assert ("entry", "wait", ("market_closed",)) in outcomes


def test_freshly_recorded_day_replays_identically(connection, session, session_factory) -> None:
    sc.run_live_scenario(session, session_factory)
    r = replay(connection, session_factory)
    assert r.comparison.identical and r.comparison.matched == 7, format_result(r)


def test_replay_rows_live_in_their_own_schema(connection, session_factory) -> None:
    sc.load(session_factory)
    replay(connection, session_factory)
    n = connection.execute(text(f"SELECT count(*) FROM {SCHEMA}.decisions")).scalar_one()
    assert n == 7
    with pytest.raises(Exception, match="append-only"):
        with connection.begin_nested():
            connection.execute(text(f"DELETE FROM {SCHEMA}.decisions"))


def test_cache_miss_is_an_error(connection, session_factory) -> None:
    data = sc.load(session_factory)
    ev = data["source_events"][0]
    with session_factory() as s, s.begin():
        cols = {k: v for k, v in ev.items() if k not in ("id", "created_at")}
        cols.update(external_id="0000320193-26-000099", first_observed_at=ev["first_observed_at"],
                    raw=ev["raw"].replace("raised", "cut"))
        s.add(SourceEvent(**cols))
    with pytest.raises(CacheMiss):
        replay(connection, session_factory)


def test_counterfactual_policy_prints_two_paths(connection, session_factory) -> None:
    sc.load(session_factory)
    base = replay(connection, session_factory, schema="replay_base01", simulate=True)
    strict = parse_policy(DEFAULT_POLICY_PATH.read_text().replace("min_p_up: 0.6", "min_p_up: 0.9"))
    alt = replay(connection, session_factory, schema="replay_alt01", policy=strict, simulate=True)
    assert [c.outcome.value for c in base.decisions].count("trade") == 1
    assert [c.outcome.value for c in alt.decisions].count("trade") == 0
    assert base.comparison is None and base.book is not None and len(base.book.trades) == 1
    # simulated: entry at the 200.00 reference, the recorded 197.50 manage quote is below the 198.00 stop
    t = base.book.trades[0]
    assert (t.exit_reason, t.exit) == ("stop", Decimal("197.50"))
    text_ = format_paths(base, alt, sc.DAY, sc.DAY)
    assert "equity A" in text_ and "2026-11-04" in text_
    line = text_.splitlines()[-1]
    assert "100,000.00" in line and line.count("100,000.00") == 1     # A lost money, B did not trade


def test_fallback_to_periodic_then_bars(connection, session, session_factory) -> None:
    from nta.schema import Quote, QuoteKind
    from tests.agent_fakes import MARKET_T, add_equity, add_filing, add_instrument
    inst = add_instrument(session)
    add_equity(session, MARKET_T - timedelta(hours=1))
    ev = add_filing(session, inst, MARKET_T)
    session.commit()
    # LLM answers are recorded (as if live ran), but no decision quote was
    from tests.agent_fakes import rig
    r0 = rig(session_factory)
    r0.pipeline.make_signal(ev)

    class Bars:
        def first_bar_close(self, symbol, after):
            return (after + timedelta(minutes=1), Decimal("200"))
    r = replay(connection, session_factory, schema="replay_rec01")       # recorded mode: no fallback, like live
    assert r.quotes_used["none"] == 1 and r.decisions[0].reasons == ("quote_missing",)
    r = replay(connection, session_factory, bars=Bars(), schema="replay_bars01", simulate=True)
    [c] = r.decisions
    assert r.quotes_used["bar"] == 1 and c.quote["uncertain"] is True and c.quote["source"] == "alpaca_bars"
    with session_factory() as s, s.begin():
        t = MARKET_T + timedelta(minutes=2)
        s.add(Quote(instrument_id=inst.id, ts=t, first_observed_at=t, last=Decimal("200.1"), source="alpaca_iex",
                    kind=QuoteKind.periodic))
    r = replay(connection, session_factory, schema="replay_per01", simulate=True)
    assert r.quotes_used["periodic"] == 1 and r.decisions[0].quote["uncertain"] is False


def test_no_direct_wall_clock_outside_the_clock_module() -> None:
    root = Path(__file__).resolve().parent.parent / "nta"
    bad = []
    for path in root.glob("*.py"):
        if path.name == "clock.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"datetime\.now\(|date\.today\(|time\.time\(|time\.monotonic\(|utcnow\(", line):
                bad.append(f"{path.name}:{n}: {line.strip()}")
    assert bad == []


