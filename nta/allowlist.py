"""Allowlist: CSV parsing, FIGI resolution via OpenFIGI, versioned membership.

Pure functions (parse_csv, choose_figi, check_cik, active_on) take data and return data.
I/O lives in OpenFigiClient, fetch_sec_tickers and load/remove.
"""

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nta.clock import Clock
from nta.http import send_with_retry
from nta.logs import log
from nta.ratelimit import TokenBucket
from nta.schema import Instrument, OpenFigiResponse

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_MAX_JOBS = 10  # per request, without an API key
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,9}$")
REQUIRED_COLUMNS = ("ticker", "name", "cik")


@dataclass(frozen=True)
class CsvRow:
    line: int
    ticker: str
    name: str
    cik: str  # zero-padded to 10


@dataclass(frozen=True)
class Rejection:
    line: int
    ticker: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class FigiChoice:
    figi: str | None
    name: str | None = None
    reason: str | None = None  # set when figi is None
    detail: str = ""


@dataclass
class LoadReport:
    added: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


# ---------- pure ----------

def parse_csv(text: str) -> tuple[list[CsvRow], list[Rejection]]:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
    reader.fieldnames = header
    rows: list[CsvRow] = []
    rejected: list[Rejection] = []
    seen: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        ticker = (raw.get("ticker") or "").strip().upper()
        name = (raw.get("name") or "").strip()
        cik_raw = (raw.get("cik") or "").strip()
        if not _TICKER_RE.match(ticker):
            rejected.append(Rejection(line, ticker, "invalid_ticker", repr(ticker)))
        elif not name:
            rejected.append(Rejection(line, ticker, "missing_name"))
        elif not (cik_raw.isdigit() and 1 <= len(cik_raw) <= 10):
            rejected.append(Rejection(line, ticker, "invalid_cik", repr(cik_raw)))
        elif ticker in seen:
            rejected.append(Rejection(line, ticker, "duplicate_in_csv"))
        else:
            seen.add(ticker)
            rows.append(CsvRow(line, ticker, name, cik_raw.zfill(10)))
    return rows, rejected


def openfigi_job(ticker: str) -> dict[str, str]:
    # OpenFIGI writes share classes with a slash: BRK.B / BRK-B -> BRK/B.
    return {"idType": "TICKER", "idValue": re.sub(r"[.\-]", "/", ticker), "exchCode": "US"}


def request_key(job: dict[str, str]) -> str:
    return json.dumps(job, sort_keys=True, separators=(",", ":"))


def choose_figi(item: dict) -> FigiChoice:
    """Pick the US composite FIGI of the common stock from one OpenFIGI result item."""
    if "error" in item:
        return FigiChoice(None, reason="openfigi_error", detail=str(item["error"]))
    data = item.get("data") or []
    if not data:
        return FigiChoice(None, reason="figi_not_found", detail=str(item.get("warning", "")))
    common = [d for d in data
              if d.get("marketSector") == "Equity" and d.get("securityType2") == "Common Stock"]
    if not common:
        types = sorted({str(d.get("securityType2")) for d in data})
        return FigiChoice(None, reason="figi_not_common_stock", detail=",".join(types))
    composites = sorted({d.get("compositeFIGI") or d["figi"] for d in common})
    if len(composites) > 1:
        return FigiChoice(None, reason="figi_ambiguous", detail=",".join(composites))
    figi = composites[0]
    match = next((d for d in common if d.get("figi") == figi), common[0])
    return FigiChoice(figi, name=match.get("name"))


def sec_ticker_key(ticker: str) -> str:
    return re.sub(r"[./]", "-", ticker.upper())


def check_cik(row: CsvRow, sec_ciks: dict[str, str]) -> Rejection | None:
    expected = sec_ciks.get(sec_ticker_key(row.ticker))
    if expected is None:
        return Rejection(row.line, row.ticker, "ticker_not_in_sec_list")
    if expected != row.cik:
        return Rejection(row.line, row.ticker, "cik_mismatch", f"csv={row.cik} sec={expected}")
    return None


class _MembershipRow(Protocol):
    id: str
    figi: str
    added_on: date
    removed_on: date | None


def active_on[R: _MembershipRow](rows: Iterable[R], day: date) -> list[R]:
    """Instruments in the allowlist on `day`.

    Rows per FIGI are replayed in insertion order (ULID): a row without removed_on opens
    an interval; a row with removed_on closes the newest open interval with the same
    added_on (or, if none is open, records a closed interval by itself). Active iff some
    interval has added_on <= day < end. Returns the opening row of the newest active
    interval, one per FIGI. Same-day remove + re-add therefore works.
    """
    by_figi: dict[str, list[R]] = {}
    for r in rows:
        by_figi.setdefault(r.figi, []).append(r)
    active: list[R] = []
    for group in by_figi.values():
        intervals: list[tuple[date, date | None, R]] = []
        for r in sorted(group, key=lambda r: r.id):
            if r.removed_on is None:
                intervals.append((r.added_on, None, r))
                continue
            for i in range(len(intervals) - 1, -1, -1):
                added_on, end, opener = intervals[i]
                if end is None and added_on == r.added_on:
                    intervals[i] = (added_on, r.removed_on, opener)
                    break
            else:
                intervals.append((r.added_on, r.removed_on, r))
        hits = [opener for added_on, end, opener in intervals
                if added_on <= day and (end is None or day < end)]
        if hits:
            active.append(hits[-1])
    return sorted(active, key=lambda r: getattr(r, "ticker", r.figi))


# ---------- I/O ----------

class OpenFigiClient:
    """OpenFIGI mapping with a DB-backed cache. Only definitive answers are cached
    (data or "not found"); errors are retried next time."""

    def __init__(self, http: httpx.Client, bucket: TokenBucket, clock: Clock,
                 session_factory: sessionmaker[Session]) -> None:
        self._http, self._bucket, self._clock, self._sf = http, bucket, clock, session_factory
        self.http_requests = 0

    def _cached(self, keys: Sequence[str]) -> dict[str, dict]:
        with self._sf() as s:
            rows = s.execute(
                select(OpenFigiResponse.request_key, OpenFigiResponse.response)
                .where(OpenFigiResponse.request_key.in_(keys))
                .order_by(OpenFigiResponse.id)
            ).all()
        return {k: v for k, v in rows}  # later (newer) rows win

    def map(self, jobs: Sequence[dict[str, str]]) -> list[dict]:
        keys = [request_key(j) for j in jobs]
        results = self._cached(keys)
        misses = [j for j, k in zip(jobs, keys) if k not in results]
        for i in range(0, len(misses), OPENFIGI_MAX_JOBS):
            batch = misses[i:i + OPENFIGI_MAX_JOBS]
            req = self._http.build_request("POST", OPENFIGI_URL, json=batch)
            self.http_requests += 1
            resp = send_with_retry(self._http, req, bucket=self._bucket, clock=self._clock)
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list) or len(items) != len(batch):
                raise ValueError("OpenFIGI returned a response that does not match the request")
            observed = self._clock.now()
            to_cache = []
            for job, item in zip(batch, items):
                results[request_key(job)] = item
                if "data" in item or "warning" in item:
                    to_cache.append(OpenFigiResponse(request_key=request_key(job), response=item,
                                                     first_observed_at=observed))
            with self._sf() as s, s.begin():
                s.add_all(to_cache)
        return [results[k] for k in keys]


def fetch_sec_tickers(http: httpx.Client, bucket: TokenBucket, clock: Clock,
                      user_agent: str) -> dict[str, str]:
    """SEC's ticker -> CIK list, keyed by sec_ticker_key, CIK zero-padded."""
    req = http.build_request("GET", SEC_TICKERS_URL, headers={"User-Agent": user_agent})
    resp = send_with_retry(http, req, bucket=bucket, clock=clock)
    resp.raise_for_status()
    out: dict[str, str] = {}
    for entry in resp.json().values():
        out.setdefault(sec_ticker_key(str(entry["ticker"])), str(entry["cik_str"]).zfill(10))
    return out


def load(session_factory: sessionmaker[Session], csv_text: str, figi: OpenFigiClient,
         today: date, sec_ciks: dict[str, str] | None = None) -> LoadReport:
    rows, rejected = parse_csv(csv_text)
    report = LoadReport(rejected=list(rejected))
    if sec_ciks is not None:
        checked = []
        for r in rows:
            bad = check_cik(r, sec_ciks)
            if bad:
                report.rejected.append(bad)
            else:
                checked.append(r)
        rows = checked

    responses = figi.map([openfigi_job(r.ticker) for r in rows]) if rows else []
    resolved: list[tuple[CsvRow, FigiChoice]] = []
    seen_figi: dict[str, str] = {}
    for r, item in zip(rows, responses):
        choice = choose_figi(item)
        if choice.figi is None:
            report.rejected.append(Rejection(r.line, r.ticker, choice.reason or "?", choice.detail))
        elif choice.figi in seen_figi:
            report.rejected.append(Rejection(r.line, r.ticker, "duplicate_figi",
                                             f"same FIGI as {seen_figi[choice.figi]}"))
        else:
            seen_figi[choice.figi] = r.ticker
            resolved.append((r, choice))

    with session_factory() as s, s.begin():
        current = {i.figi for i in active_on(s.scalars(select(Instrument)).all(), today)}
        for r, choice in resolved:
            assert choice.figi is not None
            if choice.figi in current:
                report.already_present.append(r.ticker)
                continue
            aliases = sorted({r.name} | ({choice.name} if choice.name else set()))
            s.add(Instrument(ticker=r.ticker, figi=choice.figi, cik=r.cik, name=r.name,
                             aliases=aliases, added_on=today))
            report.added.append(r.ticker)

    for rej in report.rejected:
        log("allowlist_rejected", line=rej.line, ticker=rej.ticker, reason=rej.reason,
            detail=rej.detail)
    log("allowlist_loaded", added=len(report.added), already_present=len(report.already_present),
        rejected=len(report.rejected))
    return report


def remove(session_factory: sessionmaker[Session], ticker: str, today: date) -> bool:
    """Close the active interval for `ticker` by appending a row with removed_on=today."""
    ticker = ticker.strip().upper()
    with session_factory() as s, s.begin():
        match = [i for i in active_on(s.scalars(select(Instrument)).all(), today)
                 if i.ticker == ticker]
        if not match:
            return False
        cur = match[0]
        s.add(Instrument(ticker=cur.ticker, figi=cur.figi, cik=cur.cik, name=cur.name,
                         aliases=cur.aliases, added_on=cur.added_on, removed_on=today))
    log("allowlist_removed", ticker=ticker, removed_on=today)
    return True


def current(session: Session, today: date) -> list[Instrument]:
    return active_on(session.scalars(select(Instrument)).all(), today)
