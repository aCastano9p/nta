"""Build databases from a filing spec: the hand-checked verdict fixture, or a synthetic
demo dataset for the dashboard. Demo data never goes into the real database: the CLI
writes it to a separate database (nta_demo) that it creates from scratch.

Spec format: see tests/fixtures/verdict/fixture.json.
"""

import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nta.alpaca import SOURCE
from nta.edgar import NEW_YORK, SOURCE_ID
from nta.marketcal import nyse_sessions, schedule_quotes, sessions_around
from nta.schema import (
    MARK_KINDS,
    Direction,
    EventType,
    Instrument,
    Label,
    Quote,
    QuoteKind,
    QuoteSchedule,
    Run,
    RunStop,
    SourceEvent,
)

OBSERVE_PRICE = Decimal("100.00")
HALF_SPREAD = Decimal("0.01")
DEMO_DOC = ("<html><body><p>FORM 8-K (demo data)</p><p>Item {items}: synthetic filing for the "
            "dashboard demo.</p><p>This text is generated, not from SEC.</p></body></html>")


def _quote(instrument_id: str, event_id: str | None, kind: QuoteKind, price: Decimal,
           at: datetime, style: str) -> Quote:
    if style == "last":
        bid = ask = None
    else:
        bid, ask = price - HALF_SPREAD, price + HALF_SPREAD
    return Quote(instrument_id=instrument_id, source_event_id=event_id, ts=at, first_observed_at=at,
                 bid=bid, ask=ask, last=price, source=SOURCE, kind=kind)


def load_spec(sf: sessionmaker[Session], spec: dict[str, Any]) -> None:
    window_start = date.fromisoformat(spec["window"]["start"])
    window_end = date.fromisoformat(spec["window"]["end"])
    with sf() as s, s.begin():
        insts = {}
        for i in spec["instruments"]:
            inst = Instrument(ticker=i["ticker"], figi=i["figi"], cik=i["cik"], name=i["name"],
                              aliases=[i["name"]], added_on=window_start - timedelta(days=30))
            s.add(inst)
            insts[i["ticker"]] = inst
        s.flush()
        for f in spec["filings"]:
            inst = insts[f["ticker"]]
            t0 = datetime.fromisoformat(f["observed"]).replace(tzinfo=NEW_YORK).astimezone(UTC)
            items = f.get("items", ["8.01"])
            ev = SourceEvent(
                source_id=SOURCE_ID, external_id=f.get("accession") or f"0000000000-26-{f['n']:06d}",
                form_type=f.get("form_type", "8-K"), cik=inst.cik, instrument_id=inst.id,
                published_at=t0 - timedelta(seconds=f["latency_s"]), first_observed_at=t0,
                ingested_at=t0 + timedelta(seconds=1),
                url=spec.get("url_base", "https://www.sec.gov/Archives/edgar/data") + f"/{int(inst.cik)}/demo{f['n']}-index.htm",
                items=items, raw=DEMO_DOC.format(items=", ".join(items)), raw_hash=None)
            s.add(ev)
            s.flush()
            due = schedule_quotes(sessions_around(t0), t0)
            for kind, at in due.items():
                s.add(QuoteSchedule(source_event_id=ev.id, instrument_id=inst.id, kind=kind, due_at=at))
            style = f.get("quote_style", "mid")
            s.add(_quote(inst.id, ev.id, QuoteKind.observe, OBSERVE_PRICE,
                         due[QuoteKind.observe] + timedelta(seconds=f["observe_delay_s"]), style))
            late = set(f.get("late_marks", []))
            for kind in MARK_KINDS:
                bps = f["moves_bps"].get(kind.value)
                if bps is None:
                    continue
                price = (OBSERVE_PRICE * (1 + Decimal(str(bps)) / 10000)).quantize(Decimal("0.0001"))
                delay = timedelta(minutes=30) if kind.value in late else timedelta(seconds=0.3)
                s.add(_quote(inst.id, ev.id, kind, price, due[kind] + delay, style))
            if f.get("pre_filing_price"):
                s.add(_quote(inst.id, None, QuoteKind.periodic, Decimal(f["pre_filing_price"]),
                             t0 - timedelta(minutes=5), "mid"))
            lab = f.get("label")
            if lab:
                s.add(Label(source_event_id=ev.id, labeler="human", material=lab["material"],
                            direction=Direction(lab["direction"]), event_type=EventType(lab["event_type"]),
                            labeled_at=datetime.combine(window_end + timedelta(days=1), time(12), tzinfo=UTC),
                            note=lab.get("note"), sample_seed=7))
        for r in spec.get("runs", []):
            run = Run(process=r["process"], code_sha="demo", config_hash="demo",
                      started_at=datetime.fromisoformat(r["started_at"]))
            s.add(run)
            s.flush()
            if r.get("stopped_at"):
                s.add(RunStop(run_id=run.id, stopped_at=datetime.fromisoformat(r["stopped_at"]),
                              reason=r["reason"]))


DEMO_TICKERS = [
    ("AAPL", "BBG000B9XRY4", "0000320193", "Apple Inc."),
    ("MSFT", "BBG000BPH459", "0000789019", "Microsoft Corp"),
    ("AMZN", "BBG000BVPV84", "0001018724", "Amazon.com Inc."),
    ("NVDA", "BBG000BBJQV0", "0001045810", "NVIDIA Corp"),
    ("JPM", "BBG000DMBXR2", "0000019617", "JPMorgan Chase & Co."),
    ("XOM", "BBG023CY9MM1", "0002115436", "ExxonMobil Holdings Corp"),
    ("LLY", "BBG000BNBDC2", "0000059478", "Eli Lilly and Co."),
    ("KO", "BBG000BMX289", "0000021344", "Coca-Cola Co."),
]
_ITEMS_BY_TYPE = {"guidance": ["2.02", "9.01"], "leadership": ["5.02"], "m_and_a": ["1.01", "2.01"],
                  "legal": ["8.01"], "financing": ["2.03", "9.01"], "product": ["7.01", "8.01"],
                  "other": ["5.07"]}


def synthetic_spec(seed: int = 1, start: date = date(2026, 9, 14), trading_days: int = 30,
                   filings_per_day: float = 3.0) -> dict[str, Any]:
    """A plausible-looking, clearly synthetic 30-trading-day dataset for the dashboard."""
    rng = random.Random(seed)
    sessions = nyse_sessions(start, start + timedelta(days=trading_days * 2))[:trading_days]
    filings, n = [], 0
    for sess in sessions:
        for _ in range(max(0, round(rng.gauss(filings_per_day, 1.5)))):
            n += 1
            day = sess.open.astimezone(NEW_YORK).date()
            if rng.random() < 0.6:  # most 8-Ks land after the close
                t = datetime.combine(day, time(16, rng.randint(1, 59), rng.randint(0, 59)))
            else:
                t = datetime.combine(day, time(rng.randint(9, 15), rng.randint(0, 59), rng.randint(0, 59)))
                t = max(t, datetime.combine(day, time(9, 31)))
            etype = rng.choices(list(_ITEMS_BY_TYPE), weights=[3, 2, 1, 1, 2, 1, 4])[0]
            material = rng.random() < {"guidance": 0.8, "m_and_a": 0.7, "legal": 0.4}.get(etype, 0.2)
            scale = 90 if material else 25
            d1 = rng.gauss(0, scale)
            direction = ("up" if d1 > 15 else "down" if d1 < -15 else "unclear") if material else "unclear"
            f = {"n": n, "ticker": rng.choice(DEMO_TICKERS)[0], "observed": t.isoformat(),
                 "latency_s": round(rng.lognormvariate(2.8, 0.5), 1),
                 "observe_delay_s": round(rng.choice([0.3, 0.5, 0.8, 1.1, 1.6, 7.0]), 1),
                 "items": _ITEMS_BY_TYPE[etype],
                 "moves_bps": {"mark_1h": round(d1 * 0.6, 1), "mark_1d": round(d1, 1),
                               "mark_5d": round(d1 + rng.gauss(0, scale * 0.7), 1)},
                 "form_type": "8-K/A" if rng.random() < 0.05 else "8-K"}
            if rng.random() < 0.75:
                f["label"] = {"material": material, "direction": direction, "event_type": etype}
            filings.append(f)
    first = sessions[0].open
    last = sessions[-1].close
    return {
        "url_base": "https://demo.invalid/edgar",  # synthetic: never a real SEC link
        "window": {"start": sessions[0].open.astimezone(NEW_YORK).date().isoformat(),
                   "end": sessions[-1].open.astimezone(NEW_YORK).date().isoformat()},
        "instruments": [{"ticker": t, "figi": f, "cik": c, "name": nm} for t, f, c, nm in DEMO_TICKERS],
        "filings": filings,
        "runs": [
            {"process": "poll_edgar", "started_at": first.isoformat(),
             "stopped_at": (first + timedelta(days=9)).isoformat(), "reason": "signal SIGTERM"},
            {"process": "poll_edgar", "started_at": (first + timedelta(days=9, minutes=2)).isoformat()},
            {"process": "poll_edgar", "started_at": (first + timedelta(days=20)).isoformat(),
             "stopped_at": last.isoformat(), "reason": "signal SIGTERM"},
            {"process": "quotes", "started_at": first.isoformat(),
             "stopped_at": last.isoformat(), "reason": "signal SIGTERM"},
        ],
    }


DEMO_DATABASE = "nta_demo"


def recreate_demo_database(base_url: str, seed: int = 1) -> str:
    """Drop and rebuild the demo database next to the real one, fill it with synthetic data,
    and return its URL. Refuses to touch any database not named nta_demo."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from nta.db import make_session_factory

    if make_url(base_url).database == DEMO_DATABASE:
        # DATABASE_URL itself points at nta_demo: it may be someone's real data. Refuse.
        raise RuntimeError("DATABASE_URL already points at nta_demo; refusing to rebuild it")
    url = make_url(base_url).set(database=DEMO_DATABASE)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DEMO_DATABASE}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{DEMO_DATABASE}"'))
    admin.dispose()
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    load_spec(make_session_factory(engine), synthetic_spec(seed))
    engine.dispose()
    return url.render_as_string(hide_password=False)
