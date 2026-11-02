"""Read-only localhost dashboard (stdlib http.server, server-rendered HTML, no JavaScript).

Every value that comes from the database is HTML-escaped; fetched filing text is only ever
shown as escaped plain text inside <pre>. Pages are served with a CSP that allows no
scripts. The server binds to 127.0.0.1 and answers GET only.
"""

import html
import re
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nta import allowlist
from nta.edgar import NEW_YORK, SOURCE_ID
from nta.label import DEFAULT_TARGET, ITEM_TITLES, preview_lines
from nta.marketcal import is_open, nyse_sessions, roll_forward, sessions_around
from nta.schema import MARK_KINDS, Instrument, Label, Quote, QuoteKind, QuoteSchedule, Run, RunStop, SourceEvent
from nta.verdict import (
    HORIZONS,
    MIN_MATERIAL_PER_MONTH,
    MOVE_THRESHOLD_BPS,
    OBSERVE_LATE_S,
    Record,
    Report,
    compute,
    csv_rows,
    CSV_COLUMNS,
    default_window,
    format_report,
    load_records,
    price_and_source,
    quote_price,
)

TARGET_TRADING_DAYS = 30
_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def esc(v: object) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def ny(t: datetime | None, fmt: str = "%a %m-%d %H:%M") -> str:
    return "" if t is None else t.astimezone(NEW_YORK).strftime(fmt)


def ago(now: datetime, t: datetime | None) -> str:
    if t is None:
        return "never"
    s = (now - t).total_seconds()
    if s < 0:
        return "in " + ago(t, now)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return f"{int(s)}s ago"


def px(v: Decimal | None) -> str:
    """Price with 2-4 decimals (no trailing zeros beyond cents)."""
    if v is None:
        return ""
    text = f"{v:.4f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def sec_link(url: str | None, text: str) -> str:
    """Only link to sec.gov over https; anything else is shown as text."""
    if url and urlparse(url).scheme == "https" and urlparse(url).hostname == "www.sec.gov":
        return f'<a href="{esc(url)}" rel="noreferrer noopener" target="_blank">{esc(text)}</a>'
    return esc(text)


# ---------- data ----------

@dataclass
class ProcessHealth:
    name: str
    url: str
    state: str          # running | stale | down
    detail: str


def probe(name: str, url: str, timeout: float = 0.4) -> ProcessHealth:
    try:
        r = httpx.get(url, timeout=timeout)
        body = r.json()
        since = body.get("seconds_since_last_success")
        state = "running" if r.status_code == 200 else "stale"
        detail = f"last success {since:.0f}s ago" if since is not None else "no successful cycle yet"
        return ProcessHealth(name, url, state, detail)
    except (httpx.HTTPError, ValueError):
        return ProcessHealth(name, url, "down", "not running")


@dataclass
class FilingRow:
    record: Record
    schedule: dict[QuoteKind, datetime]


def gather(s: Session, now: datetime) -> dict[str, Any]:
    window = default_window(s)
    records: list[Record] = []
    report: Report | None = None
    if window:
        records = load_records(s, *window)
        report = compute(records, *window)
    ids = [r.source_event_id for r in records]
    schedule: dict[str, dict[QuoteKind, datetime]] = {}
    if ids:
        for sid, kind, at in s.execute(select(QuoteSchedule.source_event_id, QuoteSchedule.kind,
                                              QuoteSchedule.due_at).where(QuoteSchedule.source_event_id.in_(ids))):
            schedule.setdefault(sid, {})[kind] = at
    rows = [FilingRow(r, schedule.get(r.source_event_id, {}))
            for r in sorted(records, key=lambda r: r.first_observed_at, reverse=True)]

    pending = s.execute(
        select(QuoteSchedule.due_at, QuoteSchedule.kind, Instrument.ticker)
        .join(Instrument, Instrument.id == QuoteSchedule.instrument_id)
        .where(~select(Quote.id).where(Quote.source_event_id == QuoteSchedule.source_event_id,
                                       Quote.kind == QuoteSchedule.kind).exists())
        .order_by(QuoteSchedule.due_at)).all()

    runs = s.execute(select(Run, RunStop).outerjoin(RunStop, RunStop.run_id == Run.id)
                     .order_by(Run.id.desc()).limit(12)).all()
    latest_run_by_process: dict[str, str] = {}
    for run, _ in runs:
        latest_run_by_process.setdefault(run.process, run.id)

    today = now.astimezone(NEW_YORK).date()
    insts = allowlist.current(s, today)
    per_ticker = Counter(r.record.ticker for r in rows)
    sessions = sessions_around(now, days_before=7, days_after=10)
    trading_days = 0
    if window and window[0] <= today:
        trading_days = len(nyse_sessions(window[0], min(window[1], today)))
    market_open = is_open(sessions, now)
    next_open = None if market_open else roll_forward(sessions, now)[0]
    periodic_count = s.scalar(select(func.count()).select_from(Quote).where(Quote.kind == QuoteKind.periodic))
    labels_total = s.scalar(select(func.count(func.distinct(Label.source_event_id))))
    return {
        "now": now, "window": window, "records": records, "rows": rows, "report": report,
        "pending": pending, "runs": runs, "latest_run_by_process": latest_run_by_process,
        "instruments": insts, "per_ticker": per_ticker, "trading_days": trading_days,
        "market_open": market_open, "next_open": next_open, "periodic_count": periodic_count,
        "labels_total": labels_total,
    }


# ---------- rendering ----------

STYLE = """
:root { --bg:#f7f7f5; --panel:#ffffff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e4e3de;
  --accent:#2a5bd7; --good:#1f7a4d; --good-bg:#e3f3ea; --bad:#b3261e; --bad-bg:#fbe7e5;
  --warn:#8a5a00; --warn-bg:#fdf1d8; --bar:#2a5bd7; --chip:#efeee9; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --panel:#1d1d1b; --ink:#ecebe6;
  --muted:#9a9993; --line:#33322f; --accent:#7ea2ff; --good:#6fd39f; --good-bg:#16301f;
  --bad:#ff8a80; --bad-bg:#3a1a17; --warn:#f2c46b; --warn-bg:#3a2d10; --bar:#7ea2ff; --chip:#2a2926; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.45 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
header { padding:18px 24px 10px; display:flex; flex-wrap:wrap; gap:12px 24px; align-items:baseline; }
header h1 { font-size:18px; margin:0; letter-spacing:-0.01em; }
header nav a { margin-right:14px; }
.muted { color:var(--muted); }
main { padding:0 24px 40px; max-width:1400px; }
.banner { margin:0 24px 12px; padding:8px 12px; border-radius:8px; background:var(--warn-bg); color:var(--warn); }
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); margin-bottom:14px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); margin:0 0 8px; font-weight:600; }
.big { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-0.02em; }
.cols { display:grid; gap:14px; grid-template-columns:2fr 1fr; margin-bottom:14px; }
@media (max-width: 900px) { .cols { grid-template-columns:1fr; } }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { font-size:12px; color:var(--muted); font-weight:600; }
td.num, th.num { text-align:right; }
.scroll { overflow-x:auto; }
.pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; font-weight:600; }
.ok { background:var(--good-bg); color:var(--good); } .bad { background:var(--bad-bg); color:var(--bad); }
.warn { background:var(--warn-bg); color:var(--warn); } .idle { background:var(--chip); color:var(--muted); }
.chip { display:inline-block; padding:0 6px; border-radius:4px; background:var(--chip); font-size:12px; margin-right:3px; }
.up { color:var(--good); } .down { color:var(--bad); }
.bars { display:flex; align-items:flex-end; gap:3px; height:90px; padding-top:6px; }
.bars div { flex:1; background:var(--bar); border-radius:2px 2px 0 0; min-height:1px; opacity:0.85; }
.axis { display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-top:4px; }
.meter { height:8px; background:var(--chip); border-radius:999px; position:relative; margin:6px 0 2px; }
.meter > span { position:absolute; left:0; top:0; bottom:0; border-radius:999px; background:var(--bar); }
.meter > i { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--ink); }
.verdict { font-size:20px; font-weight:700; }
pre { white-space:pre-wrap; word-break:break-word; background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:14px; font:12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:0; } dt { color:var(--muted); }
"""


def page(title: str, body: str, *, demo: bool, db_name: str, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    banner = ('<div class="banner"><b>Demo data.</b> Synthetic filings, quotes and labels in the '
              f'<code>{esc(db_name)}</code> database. Nothing here came from SEC or Alpaca.</div>') if demo else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{esc(title)}</title><style>{STYLE}</style></head><body>
<header><h1>NTA phase 0</h1><nav><a href="/">Overview</a><a href="/verdict">Verdict</a>
<a href="/verdict.csv">CSV</a></nav><span class="muted">database {esc(db_name)}</span></header>
{banner}<main>{body}</main></body></html>"""


def _pill(state: str, text: str) -> str:
    cls = {"running": "ok", "ok": "ok", "stale": "warn", "warn": "warn", "down": "bad", "bad": "bad"}.get(state, "idle")
    return f'<span class="pill {cls}">{esc(text)}</span>'


def _fmt(v: float | None, spec: str = ".1f", suffix: str = "") -> str:
    return "–" if v is None else f"{v:{spec}}{suffix}"


def _move_cell(bps: float | None) -> str:
    if bps is None:
        return '<td class="num muted">–</td>'
    cls = "up" if bps > 0 else "down" if bps < 0 else ""
    return f'<td class="num {cls}">{bps:+.1f}</td>'


def _mark_cell(row: FilingRow, kind: QuoteKind, now: datetime) -> str:
    r = row.record
    mark = r.marks.get(kind)
    due = row.schedule.get(kind)
    if mark is not None:
        move = r.move_bps(kind)
        if move is None:
            return '<td class="num"><span class="pill warn" title="recorded more than 10 min after due">late</span></td>'
        return _move_cell(move)
    if due is None:
        return '<td class="num muted" title="not scheduled yet">–</td>'
    if due > now:
        return f'<td class="num muted" title="due {esc(ny(due))}">{esc(ny(due, "%a %H:%M"))}</td>'
    return '<td class="num"><span class="pill bad">overdue</span></td>'


def _observe_cell(row: FilingRow, now: datetime) -> str:
    r = row.record
    if r.observe is None:
        due = row.schedule.get(QuoteKind.observe)
        if due is None:
            return '<td class="muted">–</td>'
        if due > now:
            return f'<td class="muted">at {esc(ny(due, "%a %H:%M"))}</td>'
        return '<td><span class="pill bad">overdue</span></td>'
    if not r.in_session:
        return f'<td class="muted" title="filed outside market hours">open +{_fmt(_delay_vs_due(r))}s</td>'
    d = r.observe_delay_s or 0.0
    cls = "bad" if d > OBSERVE_LATE_S else "ok"
    return f'<td><span class="pill {cls}">{d:.1f}s</span></td>'


def _delay_vs_due(r: Record) -> float | None:
    if r.observe is None or r.observe.due_at is None:
        return None
    return (r.observe.observed_at - r.observe.due_at).total_seconds()


def _items(items: tuple[str, ...]) -> str:
    return "".join(f'<span class="chip" title="{esc(ITEM_TITLES.get(str(i), ""))}">{esc(i)}</span>' for i in items)


def _label_cell(r: Record) -> str:
    if r.material is None:
        return '<td class="muted">–</td>'
    if not r.material:
        return f'<td class="muted">not material · {esc(r.event_type)}</td>'
    arrow = {"up": "▲", "down": "▼"}.get(r.direction or "", "◆")
    return f'<td><b>material</b> {esc(arrow)} {esc(r.event_type)}</td>'


def render_overview(d: dict[str, Any], health: list[ProcessHealth], *, demo: bool, db_name: str) -> str:
    now: datetime = d["now"]
    rep: Report | None = d["report"]
    rows: list[FilingRow] = d["rows"]
    parts: list[str] = []

    # status cards
    last_obs = rows[0].record.first_observed_at if rows else None
    market = (_pill("ok", "market open") if d["market_open"]
              else _pill("idle", "market closed") + f' <span class="muted">opens {esc(ny(d["next_open"]))}</span>')
    cards = [f'<div class="panel"><h2>{esc(h.name)}</h2>{_pill(h.state, h.state)}'
             f'<div class="muted">{esc(h.detail)}</div></div>' for h in health]
    cards.append(f'<div class="panel"><h2>Market (NYSE)</h2>{market}<div class="muted">{esc(ny(now, "%a %Y-%m-%d %H:%M %Z"))}</div></div>')
    cards.append(f'<div class="panel"><h2>Filings stored</h2><div class="big">{len(rows)}</div>'
                 f'<div class="muted">last observed {esc(ago(now, last_obs))}</div></div>')
    days = d["trading_days"]
    cards.append(f'<div class="panel"><h2>Window</h2><div class="big">{days}<span class="muted" style="font-size:14px"> / {TARGET_TRADING_DAYS} trading days</span></div>'
                 f'<div class="muted">{esc(d["window"][0]) + " → " + esc(d["window"][1]) if d["window"] else "no filings yet"}</div></div>')
    cards.append(f'<div class="panel"><h2>Labels</h2><div class="big">{d["labels_total"]}<span class="muted" style="font-size:14px"> / {DEFAULT_TARGET}</span></div>'
                 f'<div class="muted">human-labeled sample</div></div>')
    cards.append(f'<div class="panel"><h2>Allowlist</h2><div class="big">{len(d["instruments"])}</div>'
                 f'<div class="muted">{d["periodic_count"]} periodic quotes</div></div>')
    parts.append(f'<div class="grid">{"".join(cards)}</div>')

    # verdict + charts
    parts.append('<div class="cols">')
    parts.append(_verdict_panel(rep, d))
    parts.append('<div style="display:grid;gap:14px">' + _per_day_panel(rows) + _latency_panel(rep, rows) + "</div>")
    parts.append("</div>")

    # moves table
    if rep:
        trs = []
        for name, g in rep.groups:
            cells = "".join(
                f'<td class="num">{"–" if g[h] is None else f"{g[h].median:.1f} <span class=muted>[{g[h].q1:.0f}–{g[h].q3:.0f}] n={g[h].n}</span>"}</td>'
                for h in HORIZONS.values())
            trs.append(f"<tr><td>{esc(name)}</td>{cells}</tr>")
        parts.append('<div class="panel" style="margin-bottom:14px"><h2>Absolute moves from the observe quote, bps · median [IQR]</h2>'
                     '<div class="scroll"><table><tr><th>group</th><th class="num">1h</th><th class="num">1d</th><th class="num">5d</th></tr>'
                     + "".join(trs) + "</table></div></div>")

    # recent filings
    trs = []
    for row in rows[:60]:
        r = row.record
        trs.append(
            f'<tr><td>{esc(ny(r.first_observed_at))}</td><td><a href="/filing/{esc(r.source_event_id)}"><b>{esc(r.ticker or "?")}</b></a></td>'
            f'<td>{esc(r.form_type)}</td><td>{_items(r.items)}</td><td class="num">{r.poll_latency_s:.1f}s</td>'
            f'{_observe_cell(row, now)}' + "".join(_mark_cell(row, k, now) for k in MARK_KINDS) + _label_cell(r) + "</tr>")
    parts.append('<div class="panel" style="margin-bottom:14px"><h2>Recent filings</h2><div class="scroll"><table>'
                 '<tr><th>first observed (NY)</th><th>ticker</th><th>form</th><th>items</th><th class="num">poll latency</th>'
                 '<th>observe</th><th class="num">1h bps</th><th class="num">1d bps</th><th class="num">5d bps</th><th>label</th></tr>'
                 + ("".join(trs) or '<tr><td colspan="10" class="muted">No filings yet. Start <code>nta poll edgar</code>.</td></tr>')
                 + "</table></div></div>")

    # schedule + runs + allowlist
    parts.append('<div class="cols"><div style="display:grid;gap:14px">' + _runs_panel(d, health, now) + "</div>"
                 + '<div style="display:grid;gap:14px">' + _schedule_panel(d, now) + _allowlist_panel(d) + "</div></div>")
    return page("NTA dashboard", "".join(parts), demo=demo, db_name=db_name, refresh=15)


def _meter(value: float | None, threshold: float, top: float, unit: str, label: str) -> str:
    if value is None:
        return f'<div style="margin-top:10px"><div class="muted">{esc(label)}</div><div>–</div></div>'
    ok = value >= threshold
    width = max(0.0, min(100.0, value / top * 100))
    mark = threshold / top * 100
    return (f'<div style="margin-top:10px"><div style="display:flex;justify-content:space-between">'
            f'<span class="muted">{esc(label)}</span><span>{_pill("ok" if ok else "bad", f"{value:.1f}{unit}")}</span></div>'
            f'<div class="meter"><span style="width:{width:.1f}%"></span><i style="left:{mark:.1f}%" title="threshold {threshold:g}{unit}"></i></div>'
            f'<div class="muted" style="font-size:12px">needs ≥ {threshold:g}{unit}</div></div>')


def _verdict_panel(rep: Report | None, d: dict[str, Any]) -> str:
    if rep is None:
        return '<div class="panel"><h2>Verdict</h2><div class="muted">No filings yet.</div></div>'
    provisional = d["trading_days"] < TARGET_TRADING_DAYS or rep.n_labeled < DEFAULT_TARGET
    tag = '<span class="pill warn">provisional</span>' if provisional else ""
    pill = "ok" if rep.verdict == "CONTINUE" else "bad"
    share = (f"{rep.n_material} of {rep.n_labeled} labeled are material ({rep.share:.0%}, "
             f"95% CI {rep.share_ci[0]:.0%}–{rep.share_ci[1]:.0%})") if rep.n_labeled else "no labels yet"
    per_month_ci = (f'<div class="muted" style="font-size:12px">95% CI {rep.per_month_ci[0]:.1f}–{rep.per_month_ci[1]:.1f}</div>'
                    if rep.per_month_ci else "")
    top_m = max(MIN_MATERIAL_PER_MONTH * 2, (rep.per_month or 0) * 1.1)
    top_b = max(MOVE_THRESHOLD_BPS * 3, (rep.material_median_1d or 0) * 1.1)
    reasons = "".join(f"<li>{esc(x)}</li>" for x in rep.reasons)
    return (f'<div class="panel"><h2>Verdict {tag}</h2>'
            f'<div class="verdict">VERDICT: {_pill(pill, rep.verdict)}</div>'
            f'<div class="muted">{esc(share)}</div>'
            + _meter(rep.material_median_1d, MOVE_THRESHOLD_BPS, top_b, " bps", "median |1-day move|, material filings")
            + _meter(rep.per_month, MIN_MATERIAL_PER_MONTH, top_m, "", "material filings per month")
            + per_month_ci
            + (f'<ul class="muted" style="margin:10px 0 0 18px;padding:0">{reasons}</ul>' if reasons else "")
            + '<div style="margin-top:10px"><a href="/verdict">full report →</a></div></div>')


def _per_day_panel(rows: list[FilingRow]) -> str:
    counts = Counter(r.record.first_observed_at.astimezone(NEW_YORK).date() for r in rows)
    if not counts:
        return '<div class="panel"><h2>Filings per day</h2><div class="muted">No data.</div></div>'
    start, end = min(counts), max(counts)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    # Weekdays, plus any weekend day that has filings (e.g. the poller ran on a Sunday).
    days = [x for x in days if x.weekday() < 5 or counts.get(x)][-45:]
    top = max(counts.values())
    bars = "".join(f'<div style="height:{counts.get(x, 0) / top * 100:.0f}%" title="{esc(x)}: {counts.get(x, 0)}"></div>'
                   for x in days)
    return (f'<div class="panel"><h2>Filings per day</h2><div class="bars">{bars}</div>'
            f'<div class="axis"><span>{esc(days[0])}</span><span>max {top}/day</span><span>{esc(days[-1])}</span></div></div>')


def _latency_panel(rep: Report | None, rows: list[FilingRow]) -> str:
    if rep is None or not rows:
        return '<div class="panel"><h2>Poll latency</h2><div class="muted">No data.</div></div>'
    edges = [(0, 10, "<10s"), (10, 30, "10–30s"), (30, 60, "30–60s"), (60, 300, "1–5m"), (300, float("inf"), ">5m")]
    lat = [r.record.poll_latency_s for r in rows]
    counts = [sum(1 for x in lat if lo <= x < hi) for lo, hi, _ in edges]
    top = max(counts) or 1
    trs = "".join(f'<tr><td>{esc(lbl)}</td><td style="width:70%"><div class="meter" style="margin:0">'
                  f'<span style="width:{c / top * 100:.0f}%"></span></div></td><td class="num">{c}</td></tr>'
                  for (_, _, lbl), c in zip(edges, counts))
    late = (f"{rep.n_observe_late} of {rep.n_in_session} in-hours observe quotes > {OBSERVE_LATE_S:g}s"
            if rep.n_in_session else "no in-hours filings yet")
    return (f'<div class="panel"><h2>Poll latency</h2><div><span class="big">{_fmt(rep.latency_p50)}s</span> '
            f'<span class="muted">p50 · p95 {_fmt(rep.latency_p95)}s</span></div>'
            f'<table style="margin-top:6px">{trs}</table><div class="muted" style="margin-top:6px">{esc(late)}</div></div>')


def _runs_panel(d: dict[str, Any], health: list[ProcessHealth], now: datetime) -> str:
    live = {h.name: h.state for h in health}
    names = {"poll_edgar": "EDGAR poller", "quotes": "Quote recorder"}
    trs = []
    for run, stop in d["runs"]:
        if stop is not None:
            status = _pill("idle", "stopped") + f' <span class="muted">{esc(stop.reason)}</span>'
            ended = ny(stop.stopped_at)
        elif d["latest_run_by_process"].get(run.process) == run.id and live.get(names.get(run.process, "")) == "running":
            status, ended = _pill("running", "running"), ""
        else:
            status, ended = _pill("bad", "no clean stop"), '<span class="muted">crashed or killed</span>'
        trs.append(f"<tr><td>{esc(run.process)}</td><td>{esc(ny(run.started_at))}</td><td>{ended}</td>"
                   f"<td>{status}</td><td class='muted'>{esc(run.code_sha[:10])}</td></tr>")
    return ('<div class="panel"><h2>Process runs</h2><div class="scroll"><table><tr><th>process</th><th>started</th>'
            '<th>stopped</th><th>status</th><th>code</th></tr>'
            + ("".join(trs) or '<tr><td colspan="5" class="muted">No runs yet.</td></tr>') + "</table></div></div>")


def _schedule_panel(d: dict[str, Any], now: datetime) -> str:
    pending = d["pending"]
    overdue = [p for p in pending if p.due_at <= now]
    upcoming = [p for p in pending if p.due_at > now][:8]
    trs = "".join(f"<tr><td>{esc(ny(p.due_at))}</td><td>{esc(p.ticker)}</td><td>{esc(p.kind)}</td></tr>" for p in upcoming)
    return (f'<div class="panel"><h2>Quote schedule</h2><div><span class="big">{len(pending)}</span> '
            f'<span class="muted">pending</span> {_pill("bad", f"{len(overdue)} overdue") if overdue else ""}</div>'
            f'<table style="margin-top:6px"><tr><th>next due (NY)</th><th>ticker</th><th>kind</th></tr>'
            + (trs or '<tr><td colspan="3" class="muted">Nothing scheduled.</td></tr>') + "</table></div>")


def _allowlist_panel(d: dict[str, Any]) -> str:
    trs = "".join(f'<tr><td><b>{esc(i.ticker)}</b></td><td class="muted">{esc(i.name)}</td>'
                  f'<td class="num">{d["per_ticker"].get(i.ticker, 0)}</td></tr>' for i in d["instruments"])
    return (f'<div class="panel"><details><summary><b>Allowlist</b> <span class="muted">{len(d["instruments"])} tickers</span></summary>'
            f'<table style="margin-top:6px"><tr><th>ticker</th><th>name</th><th class="num">filings</th></tr>{trs}</table></details></div>')


def render_filing(s: Session, event_id: str, now: datetime, *, demo: bool, db_name: str) -> str | None:
    ev = s.get(SourceEvent, event_id)
    if ev is None:
        return None
    inst = s.get(Instrument, ev.instrument_id) if ev.instrument_id else None
    sched = {k: at for k, at in s.execute(select(QuoteSchedule.kind, QuoteSchedule.due_at)
                                          .where(QuoteSchedule.source_event_id == ev.id))}
    quotes = list(s.scalars(select(Quote).where(Quote.source_event_id == ev.id).order_by(Quote.first_observed_at)))
    labels = list(s.scalars(select(Label).where(Label.source_event_id == ev.id).order_by(Label.id)))
    obs_price = next((quote_price(q.bid, q.ask, q.last) for q in quotes if q.kind == QuoteKind.observe), None)
    qrows = []
    for kind in [QuoteKind.observe, *MARK_KINDS]:
        q = next((x for x in quotes if x.kind == kind), None)
        due = sched.get(kind)
        if q is None:
            qrows.append(f"<tr><td>{esc(kind)}</td><td>{esc(ny(due, '%a %m-%d %H:%M:%S'))}</td>"
                         f"<td class='muted' colspan='7'>{'pending' if due and due > now else 'not recorded'}</td></tr>")
            continue
        price, source = price_and_source(q.bid, q.ask, q.last)
        delay = (q.first_observed_at - due).total_seconds() if due else None
        move = (float((price / obs_price - 1) * 10000) if price and obs_price and kind != QuoteKind.observe else None)
        qrows.append(f"<tr><td>{esc(kind)}</td><td>{esc(ny(due, '%a %m-%d %H:%M:%S'))}</td>"
                     f"<td>{esc(ny(q.first_observed_at, '%a %m-%d %H:%M:%S'))}</td><td class='num'>{_fmt(delay)}s</td>"
                     f"<td class='num'>{esc(px(q.bid))}</td><td class='num'>{esc(px(q.ask))}</td><td class='num'>{esc(px(q.last))}</td>"
                     f"<td class='num'>{esc(px(price))} <span class='muted'>{esc(source or 'none')}</span></td>"
                     + (_move_cell(move) if kind != QuoteKind.observe else "<td></td>") + "</tr>")
    lab = labels[-1] if labels else None
    label_html = ("<span class='muted'>not labeled</span>" if lab is None else
                  f"{'material' if lab.material else 'not material'} · {esc(lab.direction)} · {esc(lab.event_type)}"
                  + (f" · <span class='muted'>{esc(lab.note)}</span>" if lab.note else ""))
    lines, skipped = preview_lines(ev.raw)
    preview = "\n".join(lines) if lines else "(primary document not available)"
    items = "".join(f"<div>{_items((str(i),))} {esc(ITEM_TITLES.get(str(i), ''))}</div>" for i in (ev.items or []))
    body = f"""
<div class="cols"><div class="panel"><h2>Filing</h2><dl>
<dt>ticker</dt><dd><b>{esc(inst.ticker if inst else '?')}</b> <span class="muted">{esc(inst.name if inst else '')}</span></dd>
<dt>form</dt><dd>{esc(ev.form_type)}</dd>
<dt>accession</dt><dd>{sec_link(ev.url, ev.external_id)}</dd>
<dt>published</dt><dd>{esc(ny(ev.published_at, '%a %Y-%m-%d %H:%M:%S %Z'))}</dd>
<dt>first observed</dt><dd>{esc(ny(ev.first_observed_at, '%a %Y-%m-%d %H:%M:%S %Z'))}
 <span class="muted">(poll latency {(ev.first_observed_at - ev.published_at).total_seconds():.1f}s)</span></dd>
<dt>items</dt><dd>{items or '<span class="muted">none</span>'}</dd>
<dt>label</dt><dd>{label_html}</dd>
<dt>document</dt><dd>{esc(len(ev.raw.encode()) if ev.raw else 0)} bytes{' (truncated)' if ev.raw_truncated else ''}
 <span class="muted">sha256 {esc((ev.raw_hash or '')[:16])}</span></dd>
</dl></div>
<div class="panel"><h2>Quotes</h2><div class="scroll"><table><tr><th>kind</th><th>due (NY)</th><th>recorded</th>
<th class="num">delay</th><th class="num">bid</th><th class="num">ask</th><th class="num">last</th><th class="num">price used</th><th class="num">move bps</th></tr>
{''.join(qrows)}</table></div></div></div>
<div class="panel"><h2>Document preview{f' · cover page skipped ({skipped} line{"s" if skipped != 1 else ""})' if skipped else ''} · plain text, untrusted</h2>
<pre>{esc(preview)}</pre></div>"""
    return page(f"{inst.ticker if inst else ''} {ev.form_type} · NTA", body, demo=demo, db_name=db_name)


def render_verdict(s: Session, *, demo: bool, db_name: str) -> str:
    window = default_window(s)
    if window is None:
        text = "no filings stored yet\nVERDICT: STOP"
    else:
        text = format_report(compute(load_records(s, *window), *window))
    return page("Verdict · NTA", f'<pre>{esc(text)}</pre><p><a href="/verdict.csv">Download per-filing CSV</a></p>',
                demo=demo, db_name=db_name)


def render_csv(s: Session) -> str:
    import csv
    import io
    window = default_window(s)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    if window:
        w.writerows(csv_rows(load_records(s, *window)))
    return buf.getvalue()


# ---------- server ----------

class Dashboard:
    def __init__(self, session_factory: sessionmaker[Session], *, db_name: str, demo: bool = False,
                 host: str = "127.0.0.1", port: int = 8780,
                 health_urls: dict[str, str] | None = None, clock=None) -> None:
        from nta.clock import SystemClock
        self._sf = session_factory
        self._clock = clock or SystemClock()
        self.db_name, self.demo = db_name, demo
        self.health_urls = {"EDGAR poller": "http://127.0.0.1:8765/healthz",
                            "Quote recorder": "http://127.0.0.1:8766/healthz"} if health_urls is None else health_urls
        self._lock = threading.Lock()  # the session factory may share one connection (tests)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                try:
                    status, ctype, body = outer.respond(self.path)
                except Exception as exc:  # show the error, do not crash the server
                    status, ctype = 500, "text/html; charset=utf-8"
                    body = page("Error", f"<pre>{esc(type(exc).__name__)}: {esc(exc)}</pre>",
                                demo=outer.demo, db_name=outer.db_name)
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                if ctype.startswith("text/csv"):
                    self.send_header("Content-Disposition", 'attachment; filename="nta_verdict.csv"')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def respond(self, raw_path: str) -> tuple[int, str, str]:
        path = urlparse(raw_path).path
        html_type = "text/html; charset=utf-8"
        now = self._clock.now()
        with self._lock, self._sf() as s:
            if path == "/":
                health = [probe(n, u) for n, u in self.health_urls.items()]
                return 200, html_type, render_overview(gather(s, now), health, demo=self.demo, db_name=self.db_name)
            if path == "/verdict":
                return 200, html_type, render_verdict(s, demo=self.demo, db_name=self.db_name)
            if path == "/verdict.csv":
                return 200, "text/csv; charset=utf-8", render_csv(s)
            if path.startswith("/filing/") and _ID_RE.match(path.removeprefix("/filing/")):
                out = render_filing(s, path.removeprefix("/filing/"), now, demo=self.demo, db_name=self.db_name)
                if out is not None:
                    return 200, html_type, out
        return 404, html_type, page("Not found", "<p>Not found. <a href='/'>Overview</a></p>",
                                    demo=self.demo, db_name=self.db_name)

    def start(self) -> "Dashboard":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="dashboard")
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
