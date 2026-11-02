"""nta poll x: optional X ingestion (off unless X_ENABLED=true).

Polls each approved account's timeline by numeric author id (GET /2/users/:id/tweets) every
5-10 s, stores new posts as source_events (source_id 'x'), and keeps spend under X_DAILY_USD:
USD 0.005 per post returned, a post id returned again within 24 h is not billed twice.
When the remaining budget cannot cover the minimum page (5 posts), polling stops for the
rest of the New York day.
"""

import hashlib
import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from nta import allowlist
from nta.clock import Clock
from nta.cluster import cited_accessions
from nta.edgar import new_york_date
from nta.extract import ny_day_start
from nta.http import send_with_retry
from nta.ids import new_ulid
from nta.logs import log
from nta.ratelimit import TokenBucket
from nta.schema import Instrument, SourceEvent, XPoll
from nta.xsource import Registry, XSource, map_instrument

X_API = "https://api.x.com"
SOURCE_ID = "x"
PRICE_PER_POST = Decimal("0.005")
MIN_RESULTS, MAX_RESULTS = 5, 100
DEDUP_WINDOW = timedelta(hours=24)
FIRST_POLL_LOOKBACK = timedelta(minutes=1)
NO_CIK = "0000000000"


def x_bucket(clock: Clock) -> TokenBucket:
    """1 request/s (burst 2): under X's per-app user-timeline limit."""
    return TokenBucket(rate=1.0, capacity=2.0, clock=clock)


def _ts(v: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


class XPoller:
    def __init__(self, *, sf: sessionmaker[Session], http: httpx.Client, bucket: TokenBucket, clock: Clock,
                 registry: Registry, bearer: str, daily_usd: Decimal, run_id: str | None = None,
                 base_url: str = X_API) -> None:
        self.sf, self.http, self.bucket, self.clock = sf, http, bucket, clock
        self.registry, self.daily_usd, self.run_id = registry, daily_usd, run_id
        self._headers = {"Authorization": f"Bearer {bearer}"}
        self._base = base_url.rstrip("/")
        self._budget_day = None
        self._lock = threading.Lock()
        self.polls_ok = self.polls_failed = 0
        self.last_success_monotonic: float | None = None

    def spent_today(self, s: Session, now: datetime) -> Decimal:
        return Decimal(s.scalar(select(func.coalesce(func.sum(XPoll.cost_usd), 0))
                                .where(XPoll.first_observed_at >= ny_day_start(now))) or 0)

    def since_id(self, s: Session, author_id: str) -> str | None:
        return s.scalar(select(XPoll.newest_id).where(XPoll.author_id == author_id, XPoll.newest_id.is_not(None))
                        .order_by(XPoll.first_observed_at.desc(), XPoll.id.desc()).limit(1))

    def _record(self, **fields) -> None:  # noqa: ANN003
        with self.sf() as s, s.begin():
            s.add(XPoll(first_observed_at=self.clock.now(), **fields))

    def poll_source(self, src: XSource) -> int:
        now = self.clock.now()
        with self.sf() as s:
            remaining = self.daily_usd - self.spent_today(s, now)
            since = self.since_id(s, src.author_id)
            tickers = {i.ticker.upper(): i.id for i in allowlist.current(s, new_york_date(now))}
        if remaining < MIN_RESULTS * PRICE_PER_POST:
            day = new_york_date(now)
            if self._budget_day != day:
                self._budget_day = day
                self._record(author_id=src.author_id, status="budget_skipped", detail={"remaining_usd": str(remaining)})
                log("x_budget_hit", daily_usd=str(self.daily_usd), remaining_usd=str(remaining))
            return 0
        params: dict[str, object] = {
            "max_results": max(MIN_RESULTS, min(MAX_RESULTS, int(remaining / PRICE_PER_POST))),
            "tweet.fields": "created_at,author_id,entities", "expansions": "author_id", "user.fields": "username,name"}
        if since:
            params["since_id"] = since
        else:  # first poll of an account: only what is new, not its history
            params["start_time"] = (now - FIRST_POLL_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
        req = self.http.build_request("GET", f"{self._base}/2/users/{src.author_id}/tweets", params=params,
                                      headers=self._headers)
        resp = send_with_retry(self.http, req, bucket=self.bucket, clock=self.clock, max_attempts=3,
                               base_delay=1.0, max_delay=15.0)
        observed = self.clock.now()
        if resp.status_code != 200:
            self._record(author_id=src.author_id, status="error", detail={"status": resp.status_code,
                                                                           "body": resp.text[:300]})
            log("x_poll_failed", author_id=src.author_id, status=resp.status_code)
            return 0
        body = resp.json()
        posts = body.get("data") or []
        users = {u.get("id"): u for u in (body.get("includes") or {}).get("users") or []}
        ids = [str(p.get("id")) for p in posts if p.get("id")]
        with self.sf() as s:
            seen = set(s.scalars(select(SourceEvent.external_id).where(
                SourceEvent.source_id == SOURCE_ID, SourceEvent.external_id.in_(ids),
                SourceEvent.first_observed_at >= observed - DEDUP_WINDOW))) if ids else set()
        billable = len(set(ids) - seen)
        stored = self._store(posts, users, tickers, observed)
        newest = (body.get("meta") or {}).get("newest_id") or since
        self._record(author_id=src.author_id, status="ok", posts_returned=len(posts), posts_billable=billable,
                     cost_usd=PRICE_PER_POST * billable, newest_id=newest)
        log("x_poll_ok", author_id=src.author_id, returned=len(posts), billable=billable, stored=stored)
        return stored

    def _store(self, posts: list[dict], users: dict, tickers: dict[str, str], observed: datetime) -> int:
        rows = []
        with self.sf() as s:
            for p in posts:
                text = str(p.get("text") or "")
                author = str(p.get("author_id") or "")
                u = users.get(author) or {}
                inst = map_instrument(text, tickers)
                cites = sorted(cited_accessions(text))
                if inst is None and cites:
                    inst = s.scalar(select(SourceEvent.instrument_id).where(
                        SourceEvent.source_id == "edgar", SourceEvent.external_id.in_(cites)).limit(1))
                cik = s.scalar(select(Instrument.cik).where(Instrument.id == inst)) if inst else None
                raw = json.dumps({"id": str(p.get("id")), "text": text, "author_id": author,
                                  "username": u.get("username"), "name": u.get("name"),
                                  "created_at": p.get("created_at")}, ensure_ascii=False)
                rows.append({"id": new_ulid(), "source_id": SOURCE_ID, "external_id": str(p.get("id")),
                             "form_type": "post", "cik": cik or NO_CIK, "instrument_id": inst,
                             "published_at": _ts(p.get("created_at")) or observed, "first_observed_at": observed,
                             "ingested_at": self.clock.now(),
                             "url": f"https://x.com/{u.get('username') or 'i'}/status/{p.get('id')}",
                             "items": [], "raw": raw, "raw_hash": hashlib.sha256(raw.encode()).hexdigest(),
                             "run_id": self.run_id})
        if not rows:
            return 0
        with self.sf() as s, s.begin():
            res = s.execute(insert(SourceEvent).values(rows).on_conflict_do_nothing(
                constraint="uq_source_events_source_external").returning(SourceEvent.id))
            return len(res.all())

    def poll_once(self) -> int:
        stored = 0
        for src in self.registry.sources:
            stored += self.poll_source(src)
        with self._lock:
            self.polls_ok += 1
            self.last_success_monotonic = self.clock.monotonic()
        return stored

    def run(self, stop, interval_s: float = 7.0) -> None:  # noqa: ANN001 (StopFlag)
        while not stop.is_set():
            started = self.clock.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                with self._lock:
                    self.polls_failed += 1
                log("x_poll_failed", error=f"{type(exc).__name__}: {exc}")
            stop.wait(max(0.0, interval_s - (self.clock.monotonic() - started)))

    def health(self) -> dict:
        with self._lock:
            since = (None if self.last_success_monotonic is None
                     else round(self.clock.monotonic() - self.last_success_monotonic, 3))
        with self.sf() as s:
            spent = self.spent_today(s, self.clock.now())
        return {"seconds_since_last_success": since, "polls_ok": self.polls_ok, "polls_failed": self.polls_failed,
                "spent_today_usd": str(spent), "daily_usd": str(self.daily_usd), "sources": len(self.registry.sources),
                "run_id": self.run_id}
