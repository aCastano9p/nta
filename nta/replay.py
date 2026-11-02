"""nta replay: re-run the decision loop over a window of recorded data.

- Output goes to its own schema, replay_<run_id> (tables signals and decisions, created
  LIKE the live ones, append-only trigger included). The session's search_path is
  `replay_<run_id>, public`: the pipeline's unqualified reads of signals/decisions see the
  replay's own rows, everything else (source events, quotes, llm_calls, fills, snapshots,
  kill switch) is the recorded live data, read-only.
- Never calls the LLM: the gateway is cache-only and a miss raises CacheMiss.
- Source events are fed in first_observed_at order; the 72 h before the window are replayed
  as signals only, so clusters and repeats match live.
- Quotes: the recorded decision quote; else the first periodic quote within 5 minutes after
  the event; else (optional) a 1-minute bar, flagged uncertain.
- Recorded mode (same policy): exposure from recorded fills and snapshots; position closes
  re-derived from the recorded manage quotes; every decision's content hash is compared
  with the live one.
- Counterfactual mode (--policy): exposure from a simulated book (fills at the reference
  price, exits simulated on recorded prices); the two portfolio paths are printed side by side.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import Connection, select, text
from sqlalchemy.orm import Session, sessionmaker

from nta.agent import Pipeline, PositionManager, SourceMeta
from nta.decide import (
    LIVE_DECISIONS,
    CENT,
    DecisionContent,
    Exposure,
    Position,
    QuoteView,
    load_equity,
    load_positions,
    price_of,
    quote_view,
)
from nta.edgar import NEW_YORK
from nta.extract import LlmGateway, ModelConfig
from nta.ids import new_ulid
from nta.logs import log
from nta.policy import LoadedPolicy, Policy
from nta.schema import Decision, DecisionKind, Outcome, Quote, QuoteKind, SourceEvent
from nta.simulate import net_bps, simulate_long

REPLAY_TABLES = ("signals", "decisions")
WARMUP = timedelta(hours=72)
PERIODIC_FALLBACK = timedelta(minutes=5)
DEFAULT_START_EQUITY = Decimal("100000")


class ReplayClock:
    """Time is whatever the replay says it is."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.t.timestamp()

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=max(seconds, 0))

    def set(self, t: datetime) -> None:
        self.t = t


def schema_name(run_id: str) -> str:
    return f"replay_{run_id.lower()}"


def create_replay_schema(conn: Connection, schema: str) -> None:
    conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    for t in REPLAY_TABLES:
        conn.execute(text(f'CREATE TABLE "{schema}".{t} (LIKE public.{t} INCLUDING ALL)'))
        conn.execute(text(f'CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON "{schema}".{t} '
                          f"FOR EACH ROW EXECUTE FUNCTION public.nta_forbid_mutation()"))
        conn.execute(text(f'CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON "{schema}".{t} '
                          f"FOR EACH STATEMENT EXECUTE FUNCTION public.nta_forbid_mutation()"))


def set_search_path(conn: Connection, schema: str | None) -> None:
    conn.execute(text(f'SET search_path TO "{schema}", public' if schema else "SET search_path TO public"))


# ---------- quotes ----------

class BarSource(Protocol):
    def first_bar_close(self, symbol: str, after: datetime) -> tuple[datetime, Decimal] | None: ...


class AlpacaBars:
    def __init__(self, alpaca) -> None:  # noqa: ANN001 (AlpacaData)
        self._alpaca = alpaca

    def first_bar_close(self, symbol: str, after: datetime) -> tuple[datetime, Decimal] | None:
        from nta.alpaca import alpaca_symbol

        bars = [b for b in self._alpaca.bars(alpaca_symbol(symbol), after, after + PERIODIC_FALLBACK) if b.t >= after]
        return (bars[0].t + timedelta(minutes=1), bars[0].close) if bars else None


class RecordedQuotes:
    def __init__(self, sf: sessionmaker[Session], bars: BarSource | None = None) -> None:
        self.sf, self.bars = sf, bars
        self.used = {"decision": 0, "periodic": 0, "bar": 0, "none": 0}

    def decision_quote(self, ev: SourceEvent, symbol: str, instrument_id: str) -> QuoteView | None:
        with self.sf() as s:
            q = s.scalars(select(Quote).where(Quote.source_event_id == ev.id, Quote.kind == QuoteKind.decision)
                          .order_by(Quote.first_observed_at, Quote.id).limit(1)).first()
            if q is not None:
                self.used["decision"] += 1
                return quote_view(q)
            q = s.scalars(select(Quote).where(Quote.instrument_id == instrument_id, Quote.kind == QuoteKind.periodic,
                                              Quote.first_observed_at >= ev.first_observed_at,
                                              Quote.first_observed_at <= ev.first_observed_at + PERIODIC_FALLBACK)
                          .order_by(Quote.first_observed_at, Quote.id).limit(1)).first()
        if q is not None:
            self.used["periodic"] += 1
            return quote_view(q)
        if self.bars is not None:
            bar = self.bars.first_bar_close(symbol, ev.first_observed_at)
            if bar is not None:
                self.used["bar"] += 1
                t, close = bar
                return QuoteView(None, None, close, t, t, "alpaca_bars", None, uncertain=True)
        self.used["none"] += 1
        return None

    def manage_quote(self, source_event_id: str, symbol: str, instrument_id: str) -> QuoteView | None:
        raise RuntimeError("replay feeds recorded manage quotes directly")


class FixedQuote:
    """Hands the position manager one recorded manage quote."""

    def __init__(self, q: QuoteView) -> None:
        self.q = q

    def manage_quote(self, source_event_id: str, symbol: str, instrument_id: str) -> QuoteView | None:
        return self.q


# ---------- simulated book (counterfactual mode) ----------

@dataclass
class SimTrade:
    symbol: str
    sector: str
    cluster_id: str | None
    qty: int
    entry: Decimal
    entry_t: datetime
    exit: Decimal | None
    exit_t: datetime | None
    exit_reason: str | None
    pnl: Decimal


@dataclass
class SimBook:
    start_equity: Decimal
    trades: list[SimTrade] = field(default_factory=list)

    def exposure(self, _s: Session, as_of: datetime, policy: Policy) -> Exposure:
        open_ = tuple(Position(t.symbol, Decimal(t.qty), (t.qty * t.entry).quantize(CENT), t.sector, t.cluster_id)
                      for t in self.trades if t.entry_t <= as_of and (t.exit_t is None or t.exit_t > as_of))
        return Exposure(self.equity(as_of), open_)

    def equity(self, as_of: datetime) -> Decimal:
        return self.start_equity + sum((t.pnl for t in self.trades if t.exit_t is not None and t.exit_t <= as_of),
                                       Decimal(0))

    def add(self, s: Session, c: DecisionContent, policy: Policy) -> None:
        plan = c.plan or {}
        entry = Decimal(plan["ref_price"])
        expiry = datetime.fromisoformat(plan["expiry"])
        prices = [(q.first_observed_at, p) for q in s.scalars(
            select(Quote).where(Quote.instrument_id == c.instrument_id, Quote.first_observed_at > c.decided_at,
                                Quote.first_observed_at <= expiry).order_by(Quote.first_observed_at, Quote.id))
            if (p := price_of(q)) is not None]
        sim = simulate_long(entry=entry, stop=Decimal(plan["stop"]), take_profit=Decimal(plan["take_profit"]),
                            start=c.decided_at, expiry=expiry, prices=prices)
        nb = net_bps(entry, sim.exit_price, Decimal(c.expected_cost_bps or 0)) if sim.exit_price else Decimal(0)
        qty = int(c.qty or 0)
        self.trades.append(SimTrade(c.symbol or "", policy.sector(c.symbol), c.cluster_id, qty, entry, c.decided_at,
                                    sim.exit_price, sim.exit_time or expiry, sim.exit_reason,
                                    (qty * entry * nb / 10000).quantize(CENT)))


# ---------- the run ----------

@dataclass
class Comparison:
    matched: int = 0
    mismatched: list[dict[str, Any]] = field(default_factory=list)
    live_only: list[str] = field(default_factory=list)
    replay_only: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.mismatched or self.live_only or self.replay_only)


@dataclass
class ReplayResult:
    run_id: str
    schema: str
    policy_version: str
    mode: str
    events: int
    decisions: list[DecisionContent]
    quotes_used: dict[str, int]
    comparison: Comparison | None = None
    book: SimBook | None = None


def window_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(start, datetime.min.time(), NEW_YORK)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), NEW_YORK)
    return lo, hi


def run_replay(sf: sessionmaker[Session], *, run_id: str, schema: str, start: date, end: date, policy: LoadedPolicy,
               models: ModelConfig, simulate: bool = False, bars: BarSource | None = None,
               source_meta: Callable[[SourceEvent], SourceMeta] | None = None,
               start_equity: Decimal | None = None) -> ReplayResult:
    """`sf` must already point at the replay schema (search_path). Raises CacheMiss if a
    model answer the replay needs was never recorded."""
    lo, hi = window_bounds(start, end)
    clock = ReplayClock(lo)
    gateway = LlmGateway(sf, clock, backend=None, replay=True)
    quotes = RecordedQuotes(sf, bars)
    book = None
    exposure_fn = None
    if simulate:
        with sf() as s:
            eq = start_equity or load_equity(s, lo) or DEFAULT_START_EQUITY
        book = SimBook(eq)
        exposure_fn = book.exposure
    pipe = Pipeline(sf=sf, clock=clock, gateway=gateway, models=models, policy=policy, quotes=quotes,
                    code_sha="replay", source_meta=source_meta, **({"exposure_fn": exposure_fn} if exposure_fn else {}))
    with sf() as s:
        events = list(s.scalars(select(SourceEvent).where(SourceEvent.first_observed_at >= lo - WARMUP,
                                                          SourceEvent.first_observed_at < hi)
                                .order_by(SourceEvent.first_observed_at, SourceEvent.id)))
    out: list[DecisionContent] = []
    n = 0
    for ev in events:
        clock.set(ev.first_observed_at)
        sig = pipe.make_signal(ev)
        if ev.first_observed_at < lo:
            continue  # warm-up: signals only, for clusters
        n += 1
        c = pipe.decide_signal(sig)
        out.append(c)
        if book is not None and c.outcome == Outcome.trade:
            with sf() as s:
                book.add(s, c, policy.policy)
    if not simulate:
        out += replay_closes(sf, pipe, lo, hi)
    result = ReplayResult(run_id, schema, policy.hash, "simulated" if simulate else "recorded", n, out,
                          dict(quotes.used), book=book)
    if not simulate:
        result.comparison = compare(sf, lo, hi)
    log("replay_done", run_id=run_id, schema=schema, mode=result.mode, events=n, decisions=len(out),
        identical=result.comparison.identical if result.comparison else None, quotes=quotes.used)
    return result


def replay_closes(sf: sessionmaker[Session], pipe: Pipeline, lo: datetime, hi: datetime) -> list[DecisionContent]:
    """Re-run the position manager at every recorded manage quote in the window, on the
    recorded positions, against the replay's own entry plans."""
    mgr = PositionManager(pipe)
    out = []
    with sf() as s:
        manage = list(s.scalars(select(Quote).where(Quote.kind == QuoteKind.manage, Quote.first_observed_at >= lo,
                                                    Quote.first_observed_at < hi)
                                .order_by(Quote.first_observed_at, Quote.id)))
    d = LIVE_DECISIONS.c
    for q in manage:
        pipe.clock.set(q.first_observed_at)  # type: ignore[attr-defined]
        with sf() as s:
            live_entry_event = {r[0]: r[1] for r in s.execute(select(d.id, d.source_event_id))}
            positions = [p for p in load_positions(s, q.first_observed_at, pipe.policy.policy)
                         if p.qty > 0 and live_entry_event.get(p.entry_decision_id) == q.source_event_id]
            entry = s.scalars(select(Decision).where(Decision.kind == DecisionKind.entry,
                                                     Decision.source_event_id == q.source_event_id)).first()
        if not positions or entry is None or not entry.plan:
            continue
        saved = pipe.quotes
        pipe.quotes = FixedQuote(quote_view(q))  # type: ignore[assignment]
        try:
            c = mgr.check(positions[0], entry, q.first_observed_at)
        finally:
            pipe.quotes = saved
        if c is not None:
            out.append(c)
    return out


def _close_key(rows: list[tuple[str, str, datetime]]) -> dict[str, str]:
    """First close per entry event -> content hash."""
    out: dict[str, str] = {}
    for event_id, h, _ in sorted(rows, key=lambda r: r[2]):
        out.setdefault(event_id, h)
    return out


def compare(sf: sessionmaker[Session], lo: datetime, hi: datetime) -> Comparison:
    """Replay vs live, by source event: entry decisions, and the first close per position."""
    d = LIVE_DECISIONS.c
    with sf() as s:
        in_window = select(SourceEvent.id).where(SourceEvent.first_observed_at >= lo, SourceEvent.first_observed_at < hi)
        live_entries = dict(s.execute(select(d.source_event_id, d.content_hash).where(
            d.kind == "entry", d.source_event_id.in_(in_window))).all())
        replay_entries = dict(s.execute(select(Decision.source_event_id, Decision.content_hash).where(
            Decision.kind == DecisionKind.entry)).all())
        live_closes = _close_key(list(s.execute(select(d.source_event_id, d.content_hash, d.decided_at).where(
            d.kind == "close", d.decided_at >= lo, d.decided_at < hi, d.reasons.op("?")("flatten").is_(False)))))
        replay_closes_ = _close_key(list(s.execute(select(Decision.source_event_id, Decision.content_hash,
                                                          Decision.decided_at).where(Decision.kind == DecisionKind.close))))
    cmp = Comparison()
    for kind, live, rep in (("entry", live_entries, replay_entries), ("close", live_closes, replay_closes_)):
        for ev in sorted(set(live) | set(rep)):
            if ev not in rep:
                cmp.live_only.append(f"{kind}:{ev}")
            elif ev not in live:
                cmp.replay_only.append(f"{kind}:{ev}")
            elif live[ev] == rep[ev]:
                cmp.matched += 1
            else:
                cmp.mismatched.append({"kind": kind, "source_event_id": ev, "live": live[ev], "replay": rep[ev]})
    return cmp


# ---------- reporting ----------

def daily_path(book: SimBook, start: date, end: date) -> list[tuple[date, Decimal, int]]:
    out = []
    d = start
    while d <= end:
        close = datetime.combine(d + timedelta(days=1), datetime.min.time(), NEW_YORK)
        trades = sum(1 for t in book.trades if t.entry_t < close)
        out.append((d, book.equity(close), trades))
        d += timedelta(days=1)
    return out


def format_paths(a: ReplayResult, b: ReplayResult, start: date, end: date) -> str:
    assert a.book and b.book
    pa, pb = daily_path(a.book, start, end), daily_path(b.book, start, end)
    lines = [f"portfolio paths (simulated fills, realized P&L), start equity {a.book.start_equity}",
             f"  A = policy {a.policy_version[:12]} (schema {a.schema})",
             f"  B = policy {b.policy_version[:12]} (schema {b.schema})",
             f"{'date':<12}{'equity A':>14}{'trades A':>10}{'equity B':>14}{'trades B':>10}"]
    for (d, ea, ta), (_, eb, tb) in zip(pa, pb):
        lines.append(f"{d.isoformat():<12}{ea:>14,.2f}{ta:>10}{eb:>14,.2f}{tb:>10}")
    return "\n".join(lines)


def format_result(r: ReplayResult) -> str:
    outcomes: dict[str, int] = {}
    for c in r.decisions:
        outcomes[f"{c.kind}:{c.outcome.value}"] = outcomes.get(f"{c.kind}:{c.outcome.value}", 0) + 1
    lines = [f"replay {r.run_id} ({r.mode}) -> schema {r.schema}",
             f"events in window: {r.events}; decisions: {len(r.decisions)} {dict(sorted(outcomes.items()))}",
             f"quotes used: {r.quotes_used} (bar quotes are flagged uncertain)"]
    if r.comparison is not None:
        c = r.comparison
        lines.append(f"vs live: matched {c.matched}, mismatched {len(c.mismatched)}, live only {len(c.live_only)}, "
                     f"replay only {len(c.replay_only)} -> {'IDENTICAL' if c.identical else 'DIFFERENT'}")
        for m in c.mismatched[:20]:
            lines.append(f"  mismatch {m['kind']} {m['source_event_id']}: live {m['live'][:12]} replay {m['replay'][:12]}")
        for x in (c.live_only + c.replay_only)[:20]:
            lines.append(f"  unmatched {x}")
    return "\n".join(lines)


def new_run_id() -> str:
    return new_ulid()
