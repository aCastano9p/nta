"""Terminal labeling loop.

Sample: every filing in the window gets a rank sha256("<seed>:<accession>"); the sample is
the `target` filings with the smallest ranks, labeled in rank order. The rank of a filing
never depends on which other filings exist, so a session resumes in the same order and a
filing's place in the queue is stable.

Filing text is untrusted: it is converted to plain text and stripped of control and
bidi-override characters before it is printed, so it cannot drive the terminal.
"""

import hashlib
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nta.clock import Clock
from nta.edgar import NEW_YORK, SOURCE_ID
from nta.schema import Direction, EventType, Instrument, Label, SourceEvent

DEFAULT_SEED = 7
DEFAULT_TARGET = 100
PREVIEW_LINES = 60
WRAP = 110

ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers; Compensation",
    "5.03": "Amendments to Articles or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendments to the Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

EVENT_TYPES = list(EventType)  # numbered 1..7 in the prompt


# ---------- pure: text ----------

_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"          # C0/C1 controls (incl. ESC) except \t and \n
    "​-‏‪-‮⁦-⁩"  # zero-width and bidi overrides
    "﻿]")


def sanitize(text: str) -> str:
    return _CONTROL_RE.sub("", text)


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title", "ix:header", "noscript"}
    _BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "hr", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            # Source line breaks are whitespace in HTML; only block tags start new lines.
            self.parts.append(data.replace("\r", " ").replace("\n", " "))


_ITEM_HEADING_RE = re.compile(r"^item\s+\d{1,2}\.\d{2}\b", re.I)


def preview_lines(raw: str | None, max_lines: int = PREVIEW_LINES) -> tuple[list[str], int]:
    """First max_lines lines of the document body: starts at the first "Item X.XX" heading
    (skipping the cover page) when there is one. Returns (lines, cover_lines_skipped)."""
    lines = html_to_lines(raw, max_lines=2000)
    start = next((i for i, line in enumerate(lines) if _ITEM_HEADING_RE.match(line)), 0)
    return lines[start:start + max_lines], start


def html_to_lines(raw: str | None, max_lines: int = PREVIEW_LINES, width: int = WRAP) -> list[str]:
    """Plain-text preview of a stored document: tags dropped, whitespace collapsed,
    empty lines removed, long lines wrapped, control characters stripped."""
    if not raw:
        return []
    if re.search(r"<\s*(html|body|div|p|table)\b", raw[:5000], re.I):
        ex = _TextExtractor()
        ex.feed(raw)
        ex.close()
        text = "".join(ex.parts)
    else:
        text = raw
    lines: list[str] = []
    for line in sanitize(text.replace("\xa0", " ")).splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        lines.extend(textwrap.wrap(line, width) or [line])
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


# ---------- pure: sampling and answers ----------

def sample_rank(seed: int, accession: str) -> str:
    return hashlib.sha256(f"{seed}:{accession}".encode()).hexdigest()


def choose_sample(accessions: list[str], seed: int, target: int) -> list[str]:
    return sorted(accessions, key=lambda a: sample_rank(seed, a))[:target]


def parse_material(ans: str) -> bool | str | None:
    a = ans.strip().lower()
    return {"y": True, "yes": True, "n": False, "no": False, "s": "skip", "q": "quit"}.get(a)


def parse_direction(ans: str) -> Direction | None:
    a = ans.strip().lower()
    return {"u": Direction.up, "up": Direction.up, "d": Direction.down, "down": Direction.down,
            "c": Direction.unclear, "unclear": Direction.unclear, "?": Direction.unclear}.get(a)


def parse_event_type(ans: str) -> EventType | None:
    a = ans.strip().lower()
    if a.isdigit() and 1 <= int(a) <= len(EVENT_TYPES):
        return EVENT_TYPES[int(a) - 1]
    try:
        return EventType(a)
    except ValueError:
        return None


# ---------- I/O ----------

@dataclass(frozen=True)
class Candidate:
    id: str
    accession: str
    ticker: str | None
    form_type: str
    published_at: datetime
    first_observed_at: datetime
    items: list
    url: str
    raw: str | None


def load_candidates(s: Session, start: date | None, end: date | None) -> list[Candidate]:
    rows = s.execute(
        select(SourceEvent, Instrument.ticker)
        .outerjoin(Instrument, Instrument.id == SourceEvent.instrument_id)
        .where(SourceEvent.source_id == SOURCE_ID)
    ).all()
    out = []
    for ev, ticker in rows:
        d = ev.first_observed_at.astimezone(NEW_YORK).date()
        if (start and d < start) or (end and d > end):
            continue
        out.append(Candidate(ev.id, ev.external_id, ticker, ev.form_type, ev.published_at,
                             ev.first_observed_at, list(ev.items or []), ev.url, ev.raw))
    return out


def render(c: Candidate, position: int, total: int) -> str:
    out = [
        "=" * WRAP,
        f"[{position}/{total}]  {c.ticker or '?'}  {c.form_type}  accession {c.accession}",
        f"published {c.published_at.astimezone(NEW_YORK):%Y-%m-%d %H:%M %Z}   "
        f"first observed {c.first_observed_at.astimezone(NEW_YORK):%Y-%m-%d %H:%M:%S %Z}",
        f"index: {sanitize(c.url)}",
        "items:",
    ]
    for it in c.items or ["(none listed)"]:
        out.append(f"  {sanitize(str(it))}  {ITEM_TITLES.get(str(it), '')}".rstrip())
    out.append("-" * WRAP)
    lines, skipped = preview_lines(c.raw)
    if skipped:
        out.append(f"(cover page skipped: {skipped} lines)")
    out.extend(lines if lines else ["(primary document not available)"])
    out.append("-" * WRAP)
    return "\n".join(out)


def _ask(prompt: str, parse: Callable[[str], object], input_fn: Callable[[str], str],
         print_fn: Callable[[str], None]) -> object:
    while True:
        value = parse(input_fn(prompt))
        if value is not None:
            return value
        print_fn("  ? not understood, try again")


def run_session(sf: sessionmaker[Session], clock: Clock, *, seed: int = DEFAULT_SEED,
                target: int = DEFAULT_TARGET, start: date | None = None, end: date | None = None,
                input_fn: Callable[[str], str] = input,
                print_fn: Callable[[str], None] = print) -> int:
    """Label until the sample is done, the user quits, or input ends. Returns labels written."""
    with sf() as s:
        candidates = load_candidates(s, start, end)
        labeled = set(s.scalars(select(Label.source_event_id)))
    by_acc = {c.accession: c for c in candidates}
    sample = [by_acc[a] for a in choose_sample(list(by_acc), seed, target)]
    todo = [c for c in sample if c.id not in labeled]
    done_before = len(sample) - len(todo)
    print_fn(f"sample: {len(sample)} of {len(candidates)} filings (seed {seed}); "
             f"{done_before} already labeled, {len(todo)} to go. Answer q to stop.")
    types = "  ".join(f"{i}={t.value}" for i, t in enumerate(EVENT_TYPES, 1))
    written = 0
    try:
        for n, c in enumerate(todo, start=done_before + 1):
            print_fn(render(c, n, len(sample)))
            material = _ask("material? [y/n, s=skip, q=quit] ", parse_material, input_fn, print_fn)
            if material == "quit":
                break
            if material == "skip":
                continue
            direction = (_ask("direction? [u=up, d=down, c=unclear] ", parse_direction, input_fn, print_fn)
                         if material else Direction.unclear)
            event_type = _ask(f"event_type? [{types}] ", parse_event_type, input_fn, print_fn)
            note = input_fn("note? (enter to skip) ").strip() or None
            with sf() as s, s.begin():
                s.add(Label(source_event_id=c.id, labeler="human", material=bool(material),
                            direction=direction, event_type=event_type, labeled_at=clock.now(),
                            note=note, sample_seed=seed))
            written += 1
            print_fn(f"  saved ({done_before + written}/{len(sample)})")
    except EOFError:
        print_fn("")
    print_fn(f"labeled {written} this session; {done_before + written}/{len(sample)} done.")
    return written
