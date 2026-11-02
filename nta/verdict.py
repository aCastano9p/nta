"""Phase 0 verdict.

load_records (DB -> records) is the only I/O; compute (records -> Report) and
format_report (Report -> text) are pure.

Disproof condition, fixed before any data:
  STOP if the median absolute 1-day move after a human-labeled material 8-K is below
  30 bps, or fewer than 60 labelable material filings occur per month in the allowlist.
"""

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nta.edgar import NEW_YORK, SOURCE_ID
from nta.marketcal import is_open, nyse_sessions
from nta.schema import MARK_KINDS, Instrument, Label, Quote, QuoteKind, QuoteSchedule, SourceEvent

MOVE_THRESHOLD_BPS = 30.0
MIN_MATERIAL_PER_MONTH = 60.0
DAYS_PER_MONTH = 30.4375
QUOTE_TOLERANCE = timedelta(minutes=10)  # a quote recorded later than this after due is not used
OBSERVE_LATE_S = 5.0
MAX_MID_SPREAD_BPS = 20.0  # wider IEX quotes are priced from the last trade instead
HORIZONS = {QuoteKind.mark_1h: "1h", QuoteKind.mark_1d: "1d", QuoteKind.mark_5d: "5d"}


@dataclass(frozen=True)
class QuotePoint:
    price: Decimal
    observed_at: datetime
    due_at: datetime | None
    source: str = "mid"  # "mid" or "last"

    @property
    def on_time(self) -> bool:
        return self.due_at is None or self.observed_at - self.due_at <= QUOTE_TOLERANCE


@dataclass(frozen=True)
class Record:
    source_event_id: str
    accession: str
    ticker: str | None
    form_type: str
    items: tuple[str, ...]
    published_at: datetime
    first_observed_at: datetime
    in_session: bool
    observe: QuotePoint | None
    marks: dict[QuoteKind, QuotePoint]
    pre_filing_price: Decimal | None = None
    material: bool | None = None       # None = not labeled
    direction: str | None = None
    event_type: str | None = None

    @property
    def poll_latency_s(self) -> float:
        return (self.first_observed_at - self.published_at).total_seconds()

    @property
    def observe_delay_s(self) -> float | None:
        return None if self.observe is None else \
            (self.observe.observed_at - self.first_observed_at).total_seconds()

    def move_bps(self, kind: QuoteKind) -> float | None:
        """Signed move of the mark vs the observe price, in bps; None if either quote is
        missing or was recorded too late to be trusted."""
        mark = self.marks.get(kind)
        if self.observe is None or mark is None or not self.observe.on_time or not mark.on_time:
            return None
        return float((mark.price / self.observe.price - 1) * 10000)


@dataclass(frozen=True)
class Stats:
    n: int
    median: float
    q1: float
    q3: float


@dataclass
class Report:
    start: date
    end: date
    days: int
    n_filings: int
    n_by_form: dict[str, int]
    n_labeled: int
    n_material: int
    share: float | None
    share_ci: tuple[float, float] | None
    per_month: float | None
    per_month_ci: tuple[float, float] | None
    groups: list[tuple[str, dict[str, Stats | None]]]
    excluded_late: dict[str, int]
    missing: dict[str, int]
    latency_p50: float | None
    latency_p95: float | None
    n_in_session: int
    n_observe_late: int
    n_observe_missing: int
    n_after_hours: int
    material_median_1d: float | None
    verdict: str
    reasons: list[str] = field(default_factory=list)
    price_sources: dict[str, int] = field(default_factory=dict)


# ---------- pure math ----------

def price_and_source(bid: Decimal | None, ask: Decimal | None,
                     last: Decimal | None) -> tuple[Decimal | None, str | None]:
    """The price of a quote and where it came from.

    Mid when the quote is two-sided and tight (spread <= 20 bps of the mid); otherwise
    the last trade. The free plan's IEX quotes only show IEX's own book, which is often
    far wider than the national market, so their mid can sit hundreds of bps away from
    where the stock trades. A wide quote with no trade has no price."""
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = (bid + ask) / 2
        if (ask - bid) / mid * 10000 <= Decimal(str(MAX_MID_SPREAD_BPS)):
            return mid, "mid"
    if last is not None and last > 0:
        return last, "last"
    return None, None


def quote_price(bid: Decimal | None, ask: Decimal | None, last: Decimal | None) -> Decimal | None:
    return price_and_source(bid, ask, last)[0]


def pct(values: Iterable[float], p: float) -> float | None:
    """Percentile with linear interpolation between closest ranks (numpy's default)."""
    xs = sorted(values)
    if not xs:
        return None
    rank = p * (len(xs) - 1)
    lo = math.floor(rank)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


def stats(values: list[float]) -> Stats | None:
    if not values:
        return None
    return Stats(len(values), pct(values, 0.5), pct(values, 0.25), pct(values, 0.75))  # type: ignore[arg-type]


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _abs_moves(records: list[Record], kind: QuoteKind) -> list[float]:
    return [abs(m) for r in records if (m := r.move_bps(kind)) is not None]


def _group(records: list[Record]) -> dict[str, Stats | None]:
    return {label: stats(_abs_moves(records, kind)) for kind, label in HORIZONS.items()}


def compute(records: list[Record], start: date, end: date) -> Report:
    days = (end - start).days + 1
    n = len(records)
    labeled = [r for r in records if r.material is not None]
    material = [r for r in labeled if r.material]
    share = len(material) / len(labeled) if labeled else None
    share_ci = wilson(len(material), len(labeled)) if labeled else None
    scale = n * DAYS_PER_MONTH / days
    per_month = share * scale if share is not None else None
    per_month_ci = (share_ci[0] * scale, share_ci[1] * scale) if share_ci else None

    groups: list[tuple[str, dict[str, Stats | None]]] = [
        ("all filings", _group(records)),
        ("labeled", _group(labeled)),
        ("material", _group(material)),
    ]
    for et in sorted({r.event_type for r in material if r.event_type}):
        groups.append((f"material, {et}", _group([r for r in material if r.event_type == et])))
    for d in ("up", "down", "unclear"):
        sub = [r for r in material if r.direction == d]
        if sub:
            groups.append((f"material, {d}", _group(sub)))

    excluded = {label: 0 for label in HORIZONS.values()}
    missing = {label: 0 for label in HORIZONS.values()}
    for r in records:
        for kind, label in HORIZONS.items():
            mark = r.marks.get(kind)
            if mark is None or r.observe is None:
                missing[label] += 1
            elif not (mark.on_time and r.observe.on_time):
                excluded[label] += 1

    latencies = [r.poll_latency_s for r in records]
    in_session = [r for r in records if r.in_session]
    late = [r for r in in_session if r.observe_delay_s is not None and r.observe_delay_s > OBSERVE_LATE_S]
    no_obs = [r for r in in_session if r.observe is None]

    med_1d = pct(_abs_moves(material, QuoteKind.mark_1d), 0.5)
    reasons = []
    if med_1d is None:
        reasons.append("no material filing has a usable 1-day move")
    elif med_1d < MOVE_THRESHOLD_BPS:
        reasons.append(f"median |1d move| of material filings {med_1d:.1f} bps < {MOVE_THRESHOLD_BPS:g} bps")
    if per_month is None:
        reasons.append("no labeled filings")
    elif per_month < MIN_MATERIAL_PER_MONTH:
        reasons.append(f"material filings per month {per_month:.1f} < {MIN_MATERIAL_PER_MONTH:g}")

    by_form: dict[str, int] = {}
    sources: dict[str, int] = {}
    for r in records:
        by_form[r.form_type] = by_form.get(r.form_type, 0) + 1
        for q in ([r.observe] if r.observe else []) + list(r.marks.values()):
            sources[q.source] = sources.get(q.source, 0) + 1

    return Report(
        start=start, end=end, days=days, n_filings=n, n_by_form=by_form,
        n_labeled=len(labeled), n_material=len(material), share=share, share_ci=share_ci,
        per_month=per_month, per_month_ci=per_month_ci, groups=groups,
        excluded_late=excluded, missing=missing,
        latency_p50=pct(latencies, 0.5), latency_p95=pct(latencies, 0.95),
        n_in_session=len(in_session), n_observe_late=len(late), n_observe_missing=len(no_obs),
        n_after_hours=n - len(in_session), material_median_1d=med_1d,
        verdict="STOP" if reasons else "CONTINUE", reasons=reasons, price_sources=sources,
    )


def _fmt_stats(s: Stats | None) -> str:
    return "-" if s is None else f"{s.median:.1f} [{s.q1:.1f}-{s.q3:.1f}] ({s.n})"


def _f(v: float | None, spec: str = ".1f", suffix: str = "") -> str:
    return "-" if v is None else f"{v:{spec}}{suffix}"


def format_report(r: Report) -> str:
    forms = ", ".join(f"{k} {v}" for k, v in sorted(r.n_by_form.items())) or "none"
    lines = [
        "NTA phase 0 verdict",
        f"window {r.start} .. {r.end} ({r.days} days)",
        "",
        f"8-K filings observed: {r.n_filings} ({forms})",
    ]
    if r.n_labeled:
        lo, hi = r.share_ci  # type: ignore[misc]
        plo, phi = r.per_month_ci  # type: ignore[misc]
        lines += [
            f"labeled sample: {r.n_labeled} filings, {r.n_material} material "
            f"({r.share:.1%}, 95% Wilson CI {lo:.1%}-{hi:.1%})",
            f"material filings per month: {r.per_month:.1f} (95% CI {plo:.1f}-{phi:.1f})"
            f"  [= observed x material share x {DAYS_PER_MONTH} / {r.days} days]",
        ]
    else:
        lines.append("labeled sample: none yet")
    lines += ["", "absolute moves from the observe quote, bps: median [IQR] (n)",
              f"{'group':<28}{'1h':<24}{'1d':<24}{'5d':<24}"]
    for name, g in r.groups:
        lines.append(f"{name:<28}{_fmt_stats(g['1h']):<24}{_fmt_stats(g['1d']):<24}{_fmt_stats(g['5d']):<24}".rstrip())
    lines += [
        "marks excluded (recorded > 10 min after due): " + ", ".join(f"{k} {v}" for k, v in r.excluded_late.items()),
        "marks missing: " + ", ".join(f"{k} {v}" for k, v in r.missing.items()),
        f"quote prices: {r.price_sources.get('mid', 0)} from the mid, {r.price_sources.get('last', 0)} from the "
        f"last trade (spread > {MAX_MID_SPREAD_BPS:g} bps or one-sided)",
        "",
        f"poll latency (first_observed_at - published_at): p50 {_f(r.latency_p50, '.1f', ' s')}, "
        f"p95 {_f(r.latency_p95, '.1f', ' s')} (n={r.n_filings})",
    ]
    share_late = f"{r.n_observe_late / r.n_in_session:.1%}" if r.n_in_session else "-"
    lines += [
        f"observe quote > {OBSERVE_LATE_S:g} s after first_observed_at: {r.n_observe_late} of {r.n_in_session} "
        f"filings observed during market hours ({share_late}); {r.n_observe_missing} with no observe quote",
        f"filings observed outside market hours (observe quote at next open, not counted above): {r.n_after_hours}",
        "",
        f"disproof condition: median |1d move| of material filings < {MOVE_THRESHOLD_BPS:g} bps, "
        f"or < {MIN_MATERIAL_PER_MONTH:g} material filings per month",
        f"  median |1d move|, material: {_f(r.material_median_1d, '.1f', ' bps')}",
        f"  material filings per month: {_f(r.per_month)}",
    ]
    lines += [f"  -> {reason}" for reason in r.reasons]
    lines.append(f"VERDICT: {r.verdict}")
    return "\n".join(lines)


CSV_COLUMNS = ["accession", "ticker", "form_type", "items", "published_at", "first_observed_at",
               "poll_latency_s", "in_session", "observe_price", "observe_price_source", "observe_observed_at",
               "observe_delay_s", "pre_filing_price", "price_1h", "price_1h_source", "price_1d",
               "price_1d_source", "price_5d", "price_5d_source",
               "move_1h_bps", "move_1d_bps", "move_5d_bps", "material", "direction", "event_type"]


def _px(p: Decimal | None) -> str:
    return "" if p is None else f"{p:.4f}"


def csv_rows(records: list[Record]) -> list[dict[str, object]]:
    rows = []
    for r in sorted(records, key=lambda r: r.first_observed_at):
        row: dict[str, object] = {
            "accession": r.accession, "ticker": r.ticker, "form_type": r.form_type,
            "items": ";".join(r.items), "published_at": r.published_at.isoformat(),
            "first_observed_at": r.first_observed_at.isoformat(),
            "poll_latency_s": round(r.poll_latency_s, 3), "in_session": r.in_session,
            "observe_price": _px(r.observe.price if r.observe else None),
            "observe_price_source": r.observe.source if r.observe else "",
            "observe_observed_at": r.observe.observed_at.isoformat() if r.observe else "",
            "observe_delay_s": round(r.observe_delay_s, 3) if r.observe_delay_s is not None else "",
            "pre_filing_price": _px(r.pre_filing_price),
            "material": "" if r.material is None else r.material,
            "direction": r.direction or "", "event_type": r.event_type or "",
        }
        for kind, label in HORIZONS.items():
            mark = r.marks.get(kind)
            move = r.move_bps(kind)
            row[f"price_{label}"] = _px(mark.price if mark else None)
            row[f"price_{label}_source"] = mark.source if mark else ""
            row[f"move_{label}_bps"] = round(move, 2) if move is not None else ""
        rows.append(row)
    return rows


def write_csv(records: list[Record], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(csv_rows(records))


# ---------- I/O ----------

def default_window(s: Session) -> tuple[date, date] | None:
    times = list(s.scalars(select(SourceEvent.first_observed_at).where(SourceEvent.source_id == SOURCE_ID)))
    if not times:
        return None
    days = [t.astimezone(NEW_YORK).date() for t in times]
    return min(days), max(days)


def load_records(s: Session, start: date, end: date) -> list[Record]:
    events = [
        (ev, ticker) for ev, ticker in s.execute(
            select(SourceEvent, Instrument.ticker)
            .outerjoin(Instrument, Instrument.id == SourceEvent.instrument_id)
            .where(SourceEvent.source_id == SOURCE_ID))
        if start <= ev.first_observed_at.astimezone(NEW_YORK).date() <= end
    ]
    if not events:
        return []
    ids = [ev.id for ev, _ in events]
    due = {(sid, kind): at for sid, kind, at in s.execute(
        select(QuoteSchedule.source_event_id, QuoteSchedule.kind, QuoteSchedule.due_at)
        .where(QuoteSchedule.source_event_id.in_(ids)))}
    quotes: dict[tuple[str, QuoteKind], Quote] = {}
    for q in s.scalars(select(Quote).where(Quote.source_event_id.in_(ids)).order_by(Quote.first_observed_at)):
        quotes.setdefault((q.source_event_id, q.kind), q)  # earliest wins
    labels: dict[str, Label] = {}
    for lab in s.scalars(select(Label).where(Label.source_event_id.in_(ids)).order_by(Label.id)):
        labels[lab.source_event_id] = lab  # latest wins
    sessions = nyse_sessions(start - timedelta(days=7), end + timedelta(days=7))

    def point(sid: str, kind: QuoteKind) -> QuotePoint | None:
        q = quotes.get((sid, kind))
        if q is None:
            return None
        price, source = price_and_source(q.bid, q.ask, q.last)
        if price is None or source is None:
            return None
        return QuotePoint(price, q.first_observed_at, due.get((sid, kind)), source)

    records = []
    for ev, ticker in events:
        pre = None
        if ev.instrument_id:
            pq = s.scalars(select(Quote).where(Quote.instrument_id == ev.instrument_id,
                                               Quote.kind == QuoteKind.periodic,
                                               Quote.first_observed_at < ev.first_observed_at)
                           .order_by(Quote.first_observed_at.desc()).limit(1)).first()
            pre = quote_price(pq.bid, pq.ask, pq.last) if pq else None
        lab = labels.get(ev.id)
        records.append(Record(
            source_event_id=ev.id, accession=ev.external_id, ticker=ticker, form_type=ev.form_type,
            items=tuple(ev.items or []), published_at=ev.published_at,
            first_observed_at=ev.first_observed_at, in_session=is_open(sessions, ev.first_observed_at),
            observe=point(ev.id, QuoteKind.observe),
            marks={k: p for k in MARK_KINDS if (p := point(ev.id, k)) is not None},
            pre_filing_price=pre,
            material=lab.material if lab else None,
            direction=lab.direction.value if lab else None,
            event_type=lab.event_type.value if lab else None,
        ))
    return records


def run(s: Session, start: date | None, end: date | None, csv_path: Path | None) -> tuple[str, Report | None]:
    """Load, compute, format, and optionally write the CSV. Returns (text, report)."""
    if start is None or end is None:
        window = default_window(s)
        if window is None:
            return "no filings stored yet\nVERDICT: STOP", None
        start, end = start or window[0], end or window[1]
    records = load_records(s, start, end)
    report = compute(records, start, end)
    text = format_report(report)
    if csv_path is not None:
        write_csv(records, csv_path)
        text += f"\n(per-filing table: {csv_path})"
    return text, report
