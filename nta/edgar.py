"""EDGAR 8-K poller.

Pure parts: parse_feed, group_filings, classify, parse_index, cap_raw.
I/O parts: EdgarPoller (HTTP via the shared SEC token bucket, DB inserts).

All fetched content is untrusted: it is parsed for a few fields and stored as text,
never executed, rendered or used as a template.
"""

import hashlib
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from nta import allowlist
from nta.clock import Clock
from nta.http import send_with_retry
from nta.ids import new_ulid
from nta.logs import log
from nta.ratelimit import TokenBucket
from nta.shutdown import StopFlag
from nta.schema import RAW_MAX_BYTES, SourceEvent

SOURCE_ID = "edgar"
SEC_BASE = "https://www.sec.gov"
FEED_URL = (f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcurrent&type=8-K&owner=include"
            "&count=100&output=atom")
FEED_PAGE_SIZE = 100
ACCEPTED_FORMS = frozenset({"8-K", "8-K/A"})
NEW_YORK = ZoneInfo("America/New_York")

_ATOM = "{http://www.w3.org/2005/Atom}"
_TITLE_RE = re.compile(r"^(?P<form>\S+) - (?P<company>.*) \((?P<cik>\d{10})\) \((?P<role>[^)]*)\)\s*$")
_ACCESSION_RE = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")
_ITEM_RE = re.compile(r"Item\s+(\d{1,2}\.\d{2})")


# ---------- pure ----------

@dataclass(frozen=True)
class FeedEntry:
    accession: str
    cik: str
    form_type: str
    company: str
    published_at: datetime
    index_url: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Filing:
    """One accession number. Co-registrant filings appear once per filer in the feed."""
    accession: str
    form_type: str
    published_at: datetime
    items: tuple[str, ...]
    index_url_by_cik: dict[str, str]  # insertion-ordered as in the feed


@dataclass(frozen=True)
class NewFiling:
    filing: Filing
    cik: str
    instrument_id: str
    ticker: str


@dataclass(frozen=True)
class Classified:
    new_allowlisted: list[NewFiling]
    new_other: list[Filing]       # accepted form, CIK not in the allowlist
    other_form: list[Filing]      # e.g. 8-K12B; ignored
    duplicates: list[Filing]      # accession already seen


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_feed(xml: bytes) -> tuple[list[FeedEntry], int]:
    """Parse the getcurrent Atom feed. Returns (entries, malformed_entry_count)."""
    root = ET.fromstring(xml)  # expat >= 2.4 guards entity expansion; no external entities
    entries: list[FeedEntry] = []
    bad = 0
    for e in root.iter(f"{_ATOM}entry"):
        try:
            title = _TITLE_RE.match(_text(e.find(f"{_ATOM}title")))
            acc = _ACCESSION_RE.search(_text(e.find(f"{_ATOM}id")))
            cat = e.find(f"{_ATOM}category")
            link = e.find(f"{_ATOM}link")
            updated = _text(e.find(f"{_ATOM}updated"))
            if not (title and acc and link is not None and updated):
                raise ValueError("missing field")
            form = (cat.get("term") if cat is not None else None) or title["form"]
            published = datetime.fromisoformat(updated)
            if published.tzinfo is None:
                raise ValueError("naive timestamp")
            entries.append(FeedEntry(
                accession=acc.group(1), cik=title["cik"], form_type=form.strip(),
                company=title["company"], published_at=published,
                index_url=urljoin(SEC_BASE, link.get("href", "")),
                items=tuple(dict.fromkeys(_ITEM_RE.findall(_text(e.find(f"{_ATOM}summary"))))),
            ))
        except (ValueError, TypeError):
            bad += 1
    return entries, bad


def group_filings(entries: list[FeedEntry]) -> list[Filing]:
    """Group entries by accession, keeping feed order and the first entry's fields."""
    grouped: dict[str, Filing] = {}
    for e in entries:
        f = grouped.get(e.accession)
        if f is None:
            grouped[e.accession] = Filing(e.accession, e.form_type, e.published_at, e.items,
                                          {e.cik: e.index_url})
        else:
            f.index_url_by_cik.setdefault(e.cik, e.index_url)
    return list(grouped.values())


def classify(filings: list[Filing], seen: set[str],
             allowlisted: dict[str, tuple[str, str]]) -> Classified:
    """allowlisted maps CIK -> (instrument_id, ticker)."""
    new_allow, new_other, other_form, dups = [], [], [], []
    for f in filings:
        if f.accession in seen:
            dups.append(f)
        elif f.form_type not in ACCEPTED_FORMS:
            other_form.append(f)
        else:
            cik = next((c for c in f.index_url_by_cik if c in allowlisted), None)
            if cik is None:
                new_other.append(f)
            else:
                inst_id, ticker = allowlisted[cik]
                new_allow.append(NewFiling(f, cik, inst_id, ticker))
    return Classified(new_allow, new_other, other_form, dups)


@dataclass(frozen=True)
class IndexInfo:
    primary_doc_url: str | None
    items: tuple[str, ...]


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items_text: list[str] = []
        self.rows: list[list[tuple[str, str | None]]] = []  # cells: (text, first href)
        self._div_class: str | None = None
        self._last_head = ""
        self._in_items = False
        self._in_doc_table = False
        self._row: list[tuple[str, str | None]] | None = None
        self._cell: list[str] | None = None
        self._cell_href: str | None = None
        self._head_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "div":
            cls = a.get("class") or ""
            if cls == "infoHead":
                self._div_class, self._head_buf = "infoHead", []
            elif cls == "info" and self._last_head == "Items":
                self._in_items = True
        elif tag == "br" and self._in_items:
            self.items_text.append("\n")
        elif tag == "table" and "tableFile" in (a.get("class") or "") \
                and a.get("summary") == "Document Format Files":
            self._in_doc_table = True
        elif self._in_doc_table and tag == "tr":
            self._row = []
        elif self._row is not None and tag in ("td", "th"):
            self._cell, self._cell_href = [], None
        elif self._cell is not None and tag == "a" and self._cell_href is None:
            self._cell_href = a.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._div_class == "infoHead":
                self._last_head = "".join(self._head_buf).strip()
                self._div_class = None
            elif self._in_items:
                self._in_items = False
                self._last_head = ""
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((" ".join("".join(self._cell).split()), self._cell_href))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._in_doc_table:
            self._in_doc_table = False

    def handle_data(self, data: str) -> None:
        if self._div_class == "infoHead":
            self._head_buf.append(data)
        if self._in_items:
            self.items_text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def _doc_href(href: str) -> str:
    # iXBRL documents are linked through the viewer: /ix?doc=/Archives/...
    if href.startswith("/ix?doc="):
        href = href[len("/ix?doc="):]
    return urljoin(SEC_BASE, href)


def parse_index(html: str, form_type: str) -> IndexInfo:
    p = _IndexParser()
    p.feed(html)
    p.close()
    items = tuple(dict.fromkeys(_ITEM_RE.findall("".join(p.items_text))))
    data_rows = [r for r in p.rows if len(r) >= 4 and r[2][1]]  # has a document link
    primary = next((r for r in data_rows if r[3][0] == form_type), None)
    if primary is None:
        primary = next((r for r in data_rows if r[3][0].startswith("8-K")), None)
    if primary is None:
        primary = next((r for r in data_rows if (r[2][1] or "").lower().endswith((".htm", ".html", ".txt"))), None)
    url = _doc_href(primary[2][1]) if primary and primary[2][1] else None
    return IndexInfo(url, items)


def cap_raw(text: str, max_bytes: int = RAW_MAX_BYTES) -> tuple[str, bool, str]:
    """Return (stored_text, truncated, sha256_hex_of_stored_text). Postgres text cannot
    hold NUL, so NULs are dropped. Truncation never splits a UTF-8 character."""
    data = text.replace("\x00", "").encode("utf-8")
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")
    return data.decode("utf-8"), truncated, hashlib.sha256(data).hexdigest()


def new_york_date(dt: datetime) -> date:
    return dt.astimezone(NEW_YORK).date()


# ---------- I/O ----------

@dataclass
class PollStats:
    pages: int = 0
    entries: int = 0
    malformed: int = 0
    filings: int = 0
    stored: int = 0
    not_allowlisted: int = 0
    other_form: int = 0
    duplicate_seen: int = 0
    fetch_failed: int = 0
    stored_ids: list[str] = field(default_factory=list)


class EdgarPoller:
    def __init__(self, *, http: httpx.Client, bucket: TokenBucket, clock: Clock,
                 session_factory: sessionmaker[Session], user_agent: str,
                 run_id: str | None = None, feed_url: str = FEED_URL,
                 max_backfill_pages: int = 5, allowlist_refresh_s: float = 60.0) -> None:
        self._http, self._bucket, self._clock, self._sf = http, bucket, clock, session_factory
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self.run_id = run_id
        self.feed_url = feed_url
        self.max_backfill_pages = max_backfill_pages
        self._allowlist_refresh_s = allowlist_refresh_s
        self._allowlist: dict[str, tuple[str, str]] = {}
        self._allowlist_loaded_at: float | None = None
        self._lock = threading.Lock()  # guards the health fields below
        self.duplicate_seen_total = 0
        self.polls_ok = 0
        self.polls_failed = 0
        self.last_success_monotonic: float | None = None
        self.last_success_at: datetime | None = None
        # Resume: everything already stored counts as seen; never re-stamped.
        with self._sf() as s:
            self.seen: set[str] = set(s.scalars(
                select(SourceEvent.external_id).where(SourceEvent.source_id == SOURCE_ID)))
            self.anchor: datetime | None = s.scalar(
                select(func.max(SourceEvent.published_at)).where(SourceEvent.source_id == SOURCE_ID))

    # -- helpers --

    def _get(self, url: str) -> httpx.Response:
        req = self._http.build_request("GET", url, headers=self._headers)
        resp = send_with_retry(self._http, req, bucket=self._bucket, clock=self._clock)
        resp.raise_for_status()
        return resp

    def _refresh_allowlist(self) -> None:
        now = self._clock.monotonic()
        if self._allowlist_loaded_at is not None and \
                now - self._allowlist_loaded_at < self._allowlist_refresh_s:
            return
        with self._sf() as s:
            current = allowlist.current(s, new_york_date(self._clock.now()))
        self._allowlist = {i.cik: (i.id, i.ticker) for i in current}
        self._allowlist_loaded_at = now

    def _fetch_feed(self) -> tuple[list[tuple[FeedEntry, datetime]], int, int]:
        """Fetch page 1, and more pages while there may be a gap since the last poll.
        Returns ([(entry, first_observed_at)], pages, malformed)."""
        out: list[tuple[FeedEntry, datetime]] = []
        malformed = 0
        page = 0
        while True:
            url = self.feed_url if page == 0 else f"{self.feed_url}&start={page * FEED_PAGE_SIZE}"
            resp = self._get(url)
            observed = self._clock.now()  # the moment we first saw these entries
            entries, bad = parse_feed(resp.content)
            malformed += bad
            out.extend((e, observed) for e in entries)
            page += 1
            overlap = any(e.accession in self.seen for e, _ in out)
            oldest = min((e.published_at for e, _ in out), default=None)
            maybe_gap = (not overlap and self.anchor is not None and oldest is not None
                         and oldest > self.anchor and len(entries) >= FEED_PAGE_SIZE)
            if not maybe_gap or page >= 1 + self.max_backfill_pages:
                if page > 1:
                    log("feed_backfill", pages=page, gap_closed=not maybe_gap)
                return out, page, malformed

    def _store(self, nf: NewFiling, first_observed_at: datetime) -> str | None:
        f = nf.filing
        index_url = f.index_url_by_cik[nf.cik]
        items, primary_url, raw, truncated, raw_hash = f.items, None, None, False, None
        fetch_started = self._clock.monotonic()
        try:
            info = parse_index(self._get(index_url).text, f.form_type)
            items = tuple(dict.fromkeys(f.items + info.items))
            primary_url = info.primary_doc_url
            if primary_url is None:
                raise ValueError("no primary document link in filing index")
            raw, truncated, raw_hash = cap_raw(self._get(primary_url).text)
        except (httpx.HTTPError, ValueError) as exc:
            log("filing_fetch_failed", accession=f.accession, ticker=nf.ticker, url=index_url,
                error=f"{type(exc).__name__}: {exc}")
        fetch_s = self._clock.monotonic() - fetch_started
        ingested_at = self._clock.now()
        stmt = insert(SourceEvent).values(
            id=new_ulid(), source_id=SOURCE_ID, external_id=f.accession, form_type=f.form_type,
            cik=nf.cik, instrument_id=nf.instrument_id, published_at=f.published_at,
            first_observed_at=first_observed_at, ingested_at=ingested_at, url=index_url,
            items=list(items), primary_doc_url=primary_url, raw=raw, raw_hash=raw_hash,
            raw_truncated=truncated, run_id=self.run_id,
        ).on_conflict_do_nothing(constraint="uq_source_events_source_external").returning(SourceEvent.id)
        with self._sf() as s, s.begin():
            new_id = s.scalar(stmt)
        if new_id is None:  # another process stored it first
            return None
        log("filing_stored", accession=f.accession, ticker=nf.ticker, cik=nf.cik,
            form_type=f.form_type, items=list(items),
            published_at=f.published_at, first_observed_at=first_observed_at,
            poll_latency_s=round((first_observed_at - f.published_at).total_seconds(), 3),
            fetch_s=round(fetch_s, 3), raw_bytes=len(raw.encode()) if raw else 0,
            raw_truncated=truncated, fetch_ok=raw is not None)
        return new_id

    # -- public --

    def poll_once(self) -> PollStats:
        stats = PollStats()
        self._refresh_allowlist()
        observed, stats.pages, stats.malformed = self._fetch_feed()
        stats.entries = len(observed)
        first_seen: dict[str, datetime] = {}
        for e, t in observed:
            first_seen.setdefault(e.accession, t)
        filings = group_filings([e for e, _ in observed])
        stats.filings = len(filings)
        c = classify(filings, self.seen, self._allowlist)
        for nf in c.new_allowlisted:
            new_id = self._store(nf, first_seen[nf.filing.accession])
            if new_id is None:
                stats.duplicate_seen += 1
            else:
                stats.stored += 1
                stats.stored_ids.append(new_id)
            self.seen.add(nf.filing.accession)
        for f in c.new_other + c.other_form:
            self.seen.add(f.accession)
        stats.not_allowlisted = len(c.new_other)
        stats.other_form = len(c.other_form)
        stats.duplicate_seen += len(c.duplicates)
        newest = max((e.published_at for e, _ in observed), default=None)
        with self._lock:
            if newest is not None and (self.anchor is None or newest > self.anchor):
                self.anchor = newest
            self.duplicate_seen_total += stats.duplicate_seen
            self.polls_ok += 1
            self.last_success_monotonic = self._clock.monotonic()
            self.last_success_at = self._clock.now()
        log("poll_ok", pages=stats.pages, entries=stats.entries, filings=stats.filings,
            stored=stats.stored, not_allowlisted=stats.not_allowlisted,
            other_form=stats.other_form, malformed=stats.malformed,
            duplicate_seen=stats.duplicate_seen, duplicate_seen_total=self.duplicate_seen_total,
            allowlist_size=len(self._allowlist))
        return stats

    def run(self, stop: "StopFlag", interval_s: float = 5.0) -> None:
        """Poll every interval_s (measured start to start) until stop is set."""
        while not stop.is_set():
            started = self._clock.monotonic()
            try:
                self.poll_once()
            except Exception as exc:  # keep polling; /healthz shows staleness
                with self._lock:
                    self.polls_failed += 1
                log("poll_failed", error=f"{type(exc).__name__}: {exc}")
            stop.wait(max(0.0, interval_s - (self._clock.monotonic() - started)))

    def health(self) -> dict:
        with self._lock:
            since = (None if self.last_success_monotonic is None
                     else round(self._clock.monotonic() - self.last_success_monotonic, 3))
            return {
                "seconds_since_last_success": since,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                "polls_ok": self.polls_ok,
                "polls_failed": self.polls_failed,
                "duplicate_seen_total": self.duplicate_seen_total,
                "run_id": self.run_id,
            }
