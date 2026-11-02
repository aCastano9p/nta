"""Decision engine.

Pure core: each check takes data and returns None (PASS) or a reason code; size_and_plan
turns a quote, recent prices and the exposure into a plan; decide() combines them into a
DecisionContent whose content_hash is what replay must reproduce bit for bit.

Point-in-time loaders at the bottom read the inputs "as of" a time from the database
(quotes, fills, equity snapshots, kill switch). Live and replay call the same loaders.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import MetaData, func, select
from sqlalchemy.orm import Session

from nta.marketcal import Session as MarketSession
from nta.marketcal import roll_forward, session_index_at
from nta.policy import Policy, SourceKind
from nta.schema import (
    Decision,
    DecisionKind,
    Fill,
    KillState,
    KillSwitchEvent,
    Order,
    OrderEvent,
    Outcome,
    PositionSnapshot,
    Quote,
    QuoteKind,
    RiskReview,
    RiskVerdict,
    Side,
    SignalStatus,
    SourceEvent,
)

TRADING_DAY_S = 6.5 * 3600
PERIODS_PER_DAY = 78           # 5-minute periods in a regular session
MIN_ELAPSED_S = 300            # already_priced: never scale vol below one 5-minute period
MAX_PERIOD_GAP = timedelta(minutes=10)
MIN_VOL_RETURNS = 20
EXPIRY_BEFORE_CLOSE = timedelta(minutes=5)
CENT = Decimal("0.01")


class Reason(StrEnum):
    # signal-level (no quote fetched)
    not_material = "not_material"
    llm_invalid = "llm_invalid"
    llm_budget = "llm_budget"
    llm_error = "llm_error"
    no_instrument = "no_instrument"
    injection_suspected = "injection_suspected"
    source_not_allowed = "source_not_allowed"
    # judgment / policy -> abstain
    unclear = "unclear"
    low_conviction = "low_conviction"
    long_only = "long_only"
    repeat = "repeat"
    uncorroborated = "uncorroborated"
    late = "late"
    already_priced = "already_priced"
    cap_hit = "cap_hit"
    # market state -> wait
    market_closed = "market_closed"
    scheduled_blackout = "scheduled_blackout"
    kill_switch = "kill_switch"
    quote_missing = "quote_missing"
    quote_stale = "quote_stale"
    spread_wide = "spread_wide"
    no_equity = "no_equity"


WAIT_REASONS = frozenset({Reason.market_closed, Reason.scheduled_blackout, Reason.kill_switch,
                          Reason.quote_missing, Reason.quote_stale, Reason.spread_wide, Reason.no_equity})
ESTIMATE_REASONS = frozenset({Reason.late, Reason.already_priced, Reason.cap_hit, Reason.uncorroborated})
_STATUS_REASON = {SignalStatus.triage_rejected: Reason.not_material, SignalStatus.llm_invalid: Reason.llm_invalid,
                  SignalStatus.llm_budget: Reason.llm_budget, SignalStatus.llm_error: Reason.llm_error}


# ---------- inputs ----------

@dataclass(frozen=True)
class SignalView:
    source_event_id: str
    instrument_id: str | None
    symbol: str | None
    cluster_id: str
    source_kind: SourceKind
    first_observed_at: datetime
    status: SignalStatus
    direction: str | None
    p_up: float | None
    is_repeat: bool
    corroborated: bool
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuoteView:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    ts: datetime              # the source's timestamp
    observed_at: datetime     # our first_observed_at
    source: str
    quote_id: str | None = None
    uncertain: bool = False   # replay fallback to bars

    def mid(self) -> Decimal | None:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return None

    def spread_bps(self) -> Decimal | None:
        m = self.mid()
        if m is None or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / m * 10000

    def as_json(self) -> dict[str, Any]:
        return {"bid": _s(self.bid), "ask": _s(self.ask), "last": _s(self.last), "ts": _t(self.ts),
                "observed_at": _t(self.observed_at), "source": self.source, "quote_id": self.quote_id,
                "uncertain": self.uncertain}


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    cost: Decimal           # cost basis (qty x average fill price); planned cost when pending
    sector: str
    cluster_id: str | None
    entry_decision_id: str | None = None
    pending: bool = False   # a trade entry not (fully) filled yet: counts against caps


@dataclass(frozen=True)
class Exposure:
    equity: Decimal | None
    positions: tuple[Position, ...] = ()

    def by(self, key: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.positions:
            k = getattr(p, key) or "none"
            out[k] = out.get(k, Decimal(0)) + p.cost
        return out

    def as_json(self) -> dict[str, Any]:
        return {"equity": _s(self.equity),
                "positions": [{"symbol": p.symbol, "qty": _s(p.qty), "cost": _s(p.cost), "sector": p.sector,
                               "cluster_id": p.cluster_id, "pending": p.pending}
                              for p in sorted(self.positions, key=lambda p: (p.symbol, p.pending))],
                "open_positions": len({p.symbol for p in self.positions})}


# ---------- small helpers ----------

def _s(v: Decimal | float | int | None) -> str | None:
    """One canonical string per number: Alpaca's Decimal("200") and the stored 200.000000
    must hash the same, so Decimals are written normalized and fixed-point ("200", "200.2")."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


num_str = _s


def _t(v: datetime | None) -> str | None:
    return None if v is None else v.astimezone(UTC).isoformat()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def reference_price(q: QuoteView, policy: Policy) -> Decimal | None:
    """Price a buy is planned at: the last trade on IEX (its book is too thin to trust),
    the ask on SIP (fall back to last when one side is missing)."""
    if policy.spread_unverified:
        return q.last
    return q.ask or q.last


def realized_daily_vol(prices: list[tuple[datetime, Decimal]], default_pct: Decimal) -> Decimal:
    """Std dev of 5-minute log returns, scaled to a trading day; returns as a fraction.
    Pairs more than 10 minutes apart (overnight, gaps) are skipped."""
    rets = [math.log(float(b / a)) for (ta, a), (tb, b) in zip(prices, prices[1:])
            if a > 0 and b > 0 and tb - ta <= MAX_PERIOD_GAP]
    if len(rets) < MIN_VOL_RETURNS:
        return default_pct / 100
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return Decimal(str(math.sqrt(var) * math.sqrt(PERIODS_PER_DAY)))


def atr_5m(prices: list[tuple[datetime, Decimal]], periods: int) -> Decimal | None:
    """Close-to-close ATR over the last `periods` 5-minute moves (no highs/lows are recorded)."""
    moves = [abs(b - a) for (ta, a), (tb, b) in zip(prices, prices[1:]) if tb - ta <= MAX_PERIOD_GAP]
    moves = moves[-periods:]
    if len(moves) < max(2, periods // 2):
        return None
    return (sum(moves) / len(moves)).quantize(Decimal("0.0001"))


def expiry_after(sessions: list[MarketSession], t: datetime, trading_days: int) -> datetime:
    """Same New York time of day `trading_days` sessions after t's session, at least 5 minutes
    before that session's close."""
    t, idx = roll_forward(sessions, t)
    if idx + trading_days >= len(sessions):
        raise ValueError("session list too short for the horizon; extend it")
    s = sessions[idx + trading_days]
    return min(s.open + (t - sessions[idx].open), s.close - EXPIRY_BEFORE_CLOSE)


# ---------- checks: None = PASS ----------

def check_direction(sig: SignalView, policy: Policy) -> Reason | None:
    if sig.direction == "down":
        return Reason.long_only
    if sig.direction != "up":
        return Reason.unclear
    if sig.p_up is None or Decimal(str(sig.p_up)) < policy.min_p_up:
        return Reason.low_conviction
    return None


def check_repeat(sig: SignalView) -> Reason | None:
    return Reason.repeat if sig.is_repeat else None


def check_corroborated(sig: SignalView) -> Reason | None:
    return None if sig.corroborated else Reason.uncorroborated


def check_late(sig: SignalView, as_of: datetime, policy: Policy) -> Reason | None:
    budget = timedelta(minutes=policy.latency_budget_min[sig.source_kind])
    return Reason.late if as_of - sig.first_observed_at > budget else None


def check_market_open(sessions: list[MarketSession], as_of: datetime) -> Reason | None:
    return None if session_index_at(sessions, as_of) is not None else Reason.market_closed


def check_blackout(sessions: list[MarketSession], as_of: datetime, policy: Policy) -> Reason | None:
    b = policy.blackouts
    i = session_index_at(sessions, as_of)
    if i is not None:
        s = sessions[i]
        if as_of < s.open + timedelta(minutes=b.minutes_after_open):
            return Reason.scheduled_blackout
        if as_of >= s.close - timedelta(minutes=b.minutes_before_close):
            return Reason.scheduled_blackout
    for w in b.windows:
        if w["start"] <= as_of < w["end"]:
            return Reason.scheduled_blackout
    return None


def check_kill_switch(state: KillState) -> Reason | None:
    return None if state == KillState.RUN else Reason.kill_switch


def check_quote(q: QuoteView | None, as_of: datetime, policy: Policy) -> Reason | None:
    if q is None or reference_price(q, policy) is None:
        return Reason.quote_missing
    if (as_of - q.ts).total_seconds() > policy.quote_max_age_s:
        return Reason.quote_stale
    return None


def check_spread(q: QuoteView | None, policy: Policy) -> Reason | None:
    """IEX: skipped (the decision is flagged spread_unverified). SIP: NBBO spread vs the limit."""
    if q is None or policy.spread_unverified:
        return None
    spread = q.spread_bps()
    return Reason.spread_wide if spread is None or spread > policy.spread_limit_bps else None


def check_already_priced(ref_at_observe: Decimal | None, price_now: Decimal | None, daily_vol: Decimal,
                         elapsed_s: float, policy: Policy) -> Reason | None:
    """|move since first observed| > k x daily vol x sqrt(elapsed / trading day).
    Elapsed is floored at one 5-minute period so quote noise seconds after a filing does not
    count as "priced". No reference price = PASS (nothing to compare)."""
    if ref_at_observe is None or price_now is None or ref_at_observe <= 0:
        return None
    move = abs(price_now / ref_at_observe - 1)
    limit = policy.already_priced_k * daily_vol * Decimal(str(math.sqrt(max(elapsed_s, MIN_ELAPSED_S) / TRADING_DAY_S)))
    return Reason.already_priced if move > limit else None


# ---------- sizing and plan ----------

@dataclass(frozen=True)
class Plan:
    side: Side
    qty: int
    ref_price: Decimal
    entry: Decimal          # limit price
    stop: Decimal
    take_profit: Decimal
    expiry: datetime
    stop_distance: Decimal
    atr: Decimal | None
    risk_usd: Decimal
    capped_by: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {"side": self.side.value, "qty": self.qty, "ref_price": _s(self.ref_price), "entry": _s(self.entry),
                "stop": _s(self.stop), "take_profit": _s(self.take_profit), "expiry": _t(self.expiry),
                "stop_distance": _s(self.stop_distance), "atr_5m": _s(self.atr), "risk_usd": _s(self.risk_usd),
                "capped_by": list(self.capped_by)}


def size_and_plan(*, side: Side, ref: Decimal, atr: Decimal | None, equity: Decimal, exposure: Exposure,
                  symbol: str, sector: str, cluster_id: str | None, expiry: datetime, policy: Policy) -> Plan:
    """shares = floor(risk unit x equity / stop distance), then capped by position, cluster and
    sector caps and the open-position count. qty 0 means a cap was hit."""
    pct = Decimal(100)
    dist = max(policy.stop.atr_multiple * atr if atr is not None else Decimal(0),
               policy.stop.min_pct / pct * ref).quantize(CENT, ROUND_HALF_UP)
    dist = max(dist, CENT)
    sign = 1 if side == Side.buy else -1
    entry_off = policy.entry_limit_bps / 10000
    entry = (ref * (1 + sign * entry_off)).quantize(CENT, ROUND_HALF_UP)
    stop = (ref - sign * dist).quantize(CENT, ROUND_HALF_UP)
    tp = (ref + sign * policy.take_profit_r * dist).quantize(CENT, ROUND_HALF_UP)
    risk_usd = (equity * policy.risk_unit_pct / pct).quantize(CENT, ROUND_DOWN)
    qty = int((risk_usd / dist).to_integral_value(ROUND_DOWN))
    capped: list[str] = []

    def cap(name: str, room: Decimal) -> None:
        nonlocal qty
        allowed = int(max(room, Decimal(0)) / entry) if entry > 0 else 0
        if allowed < qty:
            qty = allowed
            capped.append(name)

    open_symbols = {p.symbol for p in exposure.positions}  # pending entries count as open
    if symbol not in open_symbols and len(open_symbols) >= policy.max_open_positions:
        qty, capped = 0, capped + ["max_open_positions"]
    cap("position", equity * policy.position_cap_pct / pct - exposure.by("symbol").get(symbol, Decimal(0)))
    if cluster_id:
        cap("cluster", equity * policy.cluster_cap_pct / pct - exposure.by("cluster_id").get(cluster_id, Decimal(0)))
    cap("sector", equity * policy.sector_cap_pct / pct - exposure.by("sector").get(sector, Decimal(0)))
    return Plan(side, max(qty, 0), ref, entry, stop, tp, expiry, dist, atr, risk_usd, tuple(capped))


def expected_cost_bps(q: QuoteView, policy: Policy) -> Decimal:
    """Round trip: one full spread (half in, half out) + slippage both ways + fees."""
    spread = policy.assumed_spread_bps_unverified if policy.spread_unverified else (q.spread_bps() or Decimal(0))
    return (spread + 2 * policy.slippage_bps + policy.fee_bps).quantize(CENT, ROUND_HALF_UP)


# ---------- the decision ----------

@dataclass(frozen=True)
class Versions:
    code_sha: str
    policy_version: str
    prompt_version: str
    model_id: str
    data_snapshot: str | None


@dataclass(frozen=True)
class MarketInputs:
    """Everything the entry checks read besides the signal, all as of `as_of`."""
    as_of: datetime
    quote: QuoteView | None
    ref_at_observe: Decimal | None          # price when the event was first observed
    recent_prices: list[tuple[datetime, Decimal]] = field(default_factory=list)  # 5-minute quotes
    exposure: Exposure = Exposure(None)
    kill_state: KillState = KillState.RUN
    sessions: list[MarketSession] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionContent:
    kind: str
    source_event_id: str
    instrument_id: str | None
    symbol: str | None
    cluster_id: str | None
    outcome: Outcome
    reasons: tuple[str, ...]
    side: Side | None
    qty: int | None
    quote: dict[str, Any] | None
    exposure: dict[str, Any]
    plan: dict[str, Any] | None
    expected_cost_bps: Decimal | None
    spread_unverified: bool
    hypothetical: dict[str, Any] | None
    decided_at: datetime
    versions: Versions
    parent_source_event_id: str | None = None   # close decisions: the entry's event

    def hash_payload(self) -> dict[str, Any]:
        """What replay must reproduce: content plus policy/prompt/model versions. Excluded:
        ids generated at write time, decided_at (wall clock), code_sha, data_snapshot."""
        quote = {k: v for k, v in (self.quote or {}).items() if k != "quote_id"} if self.quote else None
        return {"kind": self.kind, "source_event_id": self.source_event_id, "instrument_id": self.instrument_id,
                "symbol": self.symbol, "cluster_id": self.cluster_id, "outcome": self.outcome.value,
                "reasons": list(self.reasons), "side": self.side.value if self.side else None, "qty": self.qty,
                "quote": quote, "exposure": self.exposure, "plan": self.plan,
                "expected_cost_bps": _s(self.expected_cost_bps), "spread_unverified": self.spread_unverified,
                "hypothetical": self.hypothetical, "parent_source_event_id": self.parent_source_event_id,
                "policy_version": self.versions.policy_version, "prompt_version": self.versions.prompt_version,
                "model_id": self.versions.model_id}

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical(self.hash_payload()).encode()).hexdigest()


def row_payload(d: Any) -> dict[str, Any]:
    """The hash payload rebuilt from a stored decisions row (a Decision or a Core row), so a
    replay mismatch can name the fields that differ. sha256(canonical(row_payload)) equals
    the stored content_hash."""
    def v(name: str) -> Any:
        return getattr(d, name)
    quote = {k: x for k, x in (v("quote") or {}).items() if k != "quote_id"} if v("quote") else None
    side, outcome = v("side"), v("outcome")
    ecb = v("expected_cost_bps")
    return {"kind": getattr(v("kind"), "value", v("kind")), "source_event_id": v("source_event_id"),
            "instrument_id": v("instrument_id"), "symbol": v("symbol"), "cluster_id": v("cluster_id"),
            "outcome": getattr(outcome, "value", outcome), "reasons": list(v("reasons") or []),
            "side": getattr(side, "value", side), "qty": v("qty"), "quote": quote, "exposure": v("exposure"),
            "plan": v("plan"), "expected_cost_bps": _s(ecb),
            "spread_unverified": v("spread_unverified"), "hypothetical": v("hypothetical"),
            "parent_source_event_id": v("source_event_id") if getattr(v("kind"), "value", v("kind")) == "close" else None,
            "policy_version": v("policy_version"), "prompt_version": v("prompt_version"), "model_id": v("model_id")}


def payload_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if canonical(a.get(k)) != canonical(b.get(k))}


def outcome_for(reasons: list[Reason]) -> Outcome:
    if not reasons:
        return Outcome.trade
    return Outcome.wait if all(r in WAIT_REASONS for r in reasons) else Outcome.abstain


def decide_entry(sig: SignalView, m: MarketInputs | None, policy: Policy, versions: Versions,
                 decided_at: datetime) -> DecisionContent:
    """One entry decision for one signal. `m` is None when no quote was fetched (signal-level
    abstentions), in which case decided_at is the time the signal was ready."""
    base = dict(kind="entry", source_event_id=sig.source_event_id, instrument_id=sig.instrument_id,
                symbol=sig.symbol, cluster_id=sig.cluster_id, versions=versions,
                spread_unverified=policy.spread_unverified)
    pre = signal_level_reason(sig)
    if pre is not None or m is None:
        reasons = [pre or Reason.quote_missing]
        return DecisionContent(**base, outcome=outcome_for(reasons), reasons=tuple(reasons), side=None, qty=None,
                               quote=None, exposure={}, plan=None, expected_cost_bps=None, hypothetical=None,
                               decided_at=decided_at)
    as_of = m.as_of
    reasons: list[Reason] = []
    for r in (check_direction(sig, policy), check_repeat(sig), check_corroborated(sig),
              check_late(sig, as_of, policy), check_market_open(m.sessions, as_of),
              check_blackout(m.sessions, as_of, policy), check_kill_switch(m.kill_state),
              check_quote(m.quote, as_of, policy), check_spread(m.quote, policy)):
        if r is not None:
            reasons.append(r)
    ref = reference_price(m.quote, policy) if m.quote else None
    vol = realized_daily_vol(m.recent_prices, policy.default_daily_vol_pct)
    r = check_already_priced(m.ref_at_observe, ref, vol, (as_of - sig.first_observed_at).total_seconds(), policy)
    if r is not None:
        reasons.append(r)
    if m.exposure.equity is None or m.exposure.equity <= 0:
        reasons.append(Reason.no_equity)

    plan = hypo = None
    cost = None
    if ref is not None and m.exposure.equity and m.exposure.equity > 0 and m.sessions:
        atr = atr_5m(m.recent_prices, policy.stop.atr_periods)
        expiry = expiry_after(m.sessions, as_of, policy.horizon_trading_days[sig.source_kind])
        args = dict(ref=ref, atr=atr, equity=m.exposure.equity, exposure=m.exposure, symbol=sig.symbol or "",
                    sector=policy.sector(sig.symbol), cluster_id=sig.cluster_id, expiry=expiry, policy=policy)
        if Reason.long_only in reasons:
            hypo = size_and_plan(side=Side.sell, **args).as_json()
        p = size_and_plan(side=Side.buy, **args)
        if p.qty < 1 and Reason.long_only not in reasons:
            reasons.append(Reason.cap_hit)
        plan = p.as_json()
        cost = expected_cost_bps(m.quote, policy) if m.quote else None
    if not reasons and plan is None:  # cannot trade without a plan (no sessions loaded)
        reasons.append(Reason.quote_missing)
    outcome = outcome_for(reasons)
    trade = outcome == Outcome.trade
    return DecisionContent(**base, outcome=outcome, reasons=tuple(r.value for r in reasons),
                           side=Side.buy if trade else None, qty=plan["qty"] if trade and plan else None,
                           quote=m.quote.as_json() if m.quote else None, exposure=m.exposure.as_json(),
                           plan=plan, expected_cost_bps=cost, hypothetical=hypo, decided_at=as_of)


def signal_level_reason(sig: SignalView) -> Reason | None:
    if sig.status != SignalStatus.extracted:
        return _STATUS_REASON[sig.status]
    if "injection_suspected" in sig.flags:
        return Reason.injection_suspected
    if "event_type_not_allowed" in sig.flags or "source_unregistered" in sig.flags:
        return Reason.source_not_allowed
    if sig.instrument_id is None or not sig.symbol:
        return Reason.no_instrument
    return None


def needs_quote(sig: SignalView) -> bool:
    return signal_level_reason(sig) is None


def to_row(c: DecisionContent, *, signal_id: str | None = None, parent_decision_id: str | None = None) -> Decision:
    return Decision(kind=c.kind, signal_id=signal_id, parent_decision_id=parent_decision_id,
                    source_event_id=c.source_event_id, instrument_id=c.instrument_id, symbol=c.symbol,
                    cluster_id=c.cluster_id, outcome=c.outcome, reasons=list(c.reasons), side=c.side, qty=c.qty,
                    quote=c.quote, exposure=c.exposure, plan=c.plan, expected_cost_bps=c.expected_cost_bps,
                    spread_unverified=c.spread_unverified, hypothetical=c.hypothetical, decided_at=c.decided_at,
                    code_sha=c.versions.code_sha, policy_version=c.versions.policy_version,
                    prompt_version=c.versions.prompt_version, model_id=c.versions.model_id,
                    data_snapshot=c.versions.data_snapshot, content_hash=c.content_hash)


# ---------- position management (pure) ----------

def exit_price(q: QuoteView, policy: Policy) -> Decimal | None:
    """What a long can sell at: the last trade on IEX, the bid on SIP."""
    if policy.spread_unverified:
        return q.last
    return q.bid or q.last


def close_trigger(plan: dict[str, Any], price: Decimal | None, now: datetime) -> str | None:
    """stop / take_profit / expiry for a long position, checked in that order."""
    if price is not None and price <= Decimal(plan["stop"]):
        return "stop"
    if price is not None and price >= Decimal(plan["take_profit"]):
        return "take_profit"
    if now >= datetime.fromisoformat(plan["expiry"]):
        return "expiry"
    return None


# ---------- point-in-time loaders (shared by live and replay) ----------

# Replay runs with search_path = replay_<id>, public, so the unqualified `decisions` is the
# replay's own output. Positions are recorded-world data: they must join the live decisions
# that the recorded fills belong to, hence an explicitly schema-qualified copy.
LIVE_DECISIONS = Decision.__table__.to_metadata(MetaData(), schema="public")

def quote_view(q: Quote, uncertain: bool = False) -> QuoteView:
    return QuoteView(q.bid, q.ask, q.last, q.ts, q.first_observed_at, q.source, q.id, uncertain)


def price_of(q: Quote) -> Decimal | None:
    """One price per stored quote for vol/ATR/reference: last trade, else mid."""
    if q.last:
        return q.last
    if q.bid and q.ask:
        return (q.bid + q.ask) / 2
    return None


def load_ref_at_observe(s: Session, instrument_id: str, source_event_id: str, first_observed_at: datetime,
                        as_of: datetime) -> Decimal | None:
    """The event's observe quote if recorded by as_of, else the last quote of any kind for the
    instrument observed at or before first_observed_at."""
    obs = s.scalars(select(Quote).where(Quote.source_event_id == source_event_id, Quote.kind == QuoteKind.observe,
                                        Quote.first_observed_at <= as_of)
                    .order_by(Quote.first_observed_at, Quote.id).limit(1)).first()
    if obs is None:
        obs = s.scalars(select(Quote).where(Quote.instrument_id == instrument_id,
                                            Quote.first_observed_at <= first_observed_at)
                        .order_by(Quote.first_observed_at.desc(), Quote.id.desc()).limit(1)).first()
    return price_of(obs) if obs else None


def load_recent_prices(s: Session, instrument_id: str, as_of: datetime, n: int = 80) -> list[tuple[datetime, Decimal]]:
    rows = s.scalars(select(Quote).where(Quote.instrument_id == instrument_id, Quote.kind == QuoteKind.periodic,
                                         Quote.first_observed_at <= as_of)
                     .order_by(Quote.first_observed_at.desc(), Quote.id.desc()).limit(n)).all()
    out = [(q.first_observed_at, p) for q in reversed(rows) if (p := price_of(q)) is not None]
    return out


def load_kill_state(s: Session, as_of: datetime) -> KillState:
    st = s.scalar(select(KillSwitchEvent.state).where(KillSwitchEvent.at <= as_of)
                  .order_by(KillSwitchEvent.at.desc(), KillSwitchEvent.id.desc()).limit(1))
    return KillState(st) if st else KillState.RUN


def load_equity(s: Session, as_of: datetime) -> Decimal | None:
    return s.scalar(select(PositionSnapshot.equity).where(PositionSnapshot.account == "T",
                                                          PositionSnapshot.taken_at <= as_of,
                                                          PositionSnapshot.equity.is_not(None))
                    .order_by(PositionSnapshot.taken_at.desc(), PositionSnapshot.id.desc()).limit(1))


def load_positions(s: Session, as_of: datetime, policy: Policy) -> tuple[Position, ...]:
    """Open positions from fills observed by as_of, attributed to their entry decision.
    A close decision's fills reduce its parent entry's position."""
    d = LIVE_DECISIONS.c
    rows = s.execute(select(Fill.symbol, Fill.side, Fill.qty, Fill.price, d.id, d.kind, d.parent_decision_id)
                     .join(LIVE_DECISIONS, d.id == Fill.decision_id)
                     .where(Fill.first_observed_at <= as_of).order_by(Fill.first_observed_at, Fill.id)).all()
    qty: dict[str, Decimal] = {}
    cost: dict[str, Decimal] = {}
    symbol: dict[str, str] = {}
    for sym, side, q, price, dec_id, kind, parent in rows:
        entry = dec_id if kind == "entry" else parent
        symbol[entry] = sym
        if side == Side.buy:
            qty[entry] = qty.get(entry, Decimal(0)) + q
            cost[entry] = cost.get(entry, Decimal(0)) + q * price
        else:
            held = qty.get(entry, Decimal(0))
            if held > 0:
                cost[entry] = cost.get(entry, Decimal(0)) * (held - q) / held
            qty[entry] = held - q
    open_ids = [e for e, q in qty.items() if q > 0]
    clusters = dict(s.execute(select(d.id, d.cluster_id).where(d.id.in_(open_ids))).all()) if open_ids else {}
    return tuple(Position(symbol[e], qty[e], cost[e].quantize(CENT, ROUND_HALF_UP), policy.sector(symbol[e]),
                          clusters.get(e), e) for e in sorted(open_ids))


FINAL_ORDER_EVENTS = ("terminal", "broker_rejected", "blocked", "hmac_invalid", "no_trade_decision")
PENDING_ENTRY_WINDOW = timedelta(minutes=5)


def load_pending_entries(s: Session, as_of: datetime, policy: Policy) -> tuple[Position, ...]:
    """Trade entries decided in the last 5 minutes whose quantity is not filled yet and that
    were not rejected or finished by as_of. Without them, two decisions seconds apart would
    each see the cap as free. Recorded data only, so replay sees the same."""
    d = LIVE_DECISIONS.c
    out = []
    for dec_id, symbol, cluster_id, qty, plan in s.execute(
            select(d.id, d.symbol, d.cluster_id, d.qty, d.plan).where(
                # strictly earlier: a decision is never its own pending entry (in replay, the live
                # twin of the decision being replayed has decided_at == as_of)
                d.kind == "entry", d.outcome == "trade", d.decided_at < as_of,
                d.decided_at > as_of - PENDING_ENTRY_WINDOW).order_by(d.decided_at, d.id)):
        verdict = s.scalar(select(RiskReview.verdict).where(RiskReview.decision_id == dec_id,
                                                            RiskReview.reviewed_at <= as_of))
        if verdict == RiskVerdict.reject:
            continue
        order = s.execute(select(Order.id, Order.qty).where(Order.decision_id == dec_id)).first()
        if order is not None and s.scalar(select(func.count()).select_from(OrderEvent).where(
                OrderEvent.order_id == order[0], OrderEvent.event.in_(FINAL_ORDER_EVENTS),
                OrderEvent.first_observed_at <= as_of)):
            continue
        planned = Decimal(order[1] if order is not None else qty or 0)
        filled = Decimal(s.scalar(select(func.coalesce(func.sum(Fill.qty), 0)).where(
            Fill.decision_id == dec_id, Fill.first_observed_at <= as_of)) or 0)
        rem = planned - filled
        if rem > 0 and symbol and plan:
            out.append(Position(symbol, rem, (rem * Decimal(plan["entry"])).quantize(CENT, ROUND_HALF_UP),
                                policy.sector(symbol), cluster_id, dec_id, pending=True))
    return tuple(out)


def load_exposure(s: Session, as_of: datetime, policy: Policy) -> Exposure:
    return Exposure(load_equity(s, as_of), load_positions(s, as_of, policy) + load_pending_entries(s, as_of, policy))


def data_snapshot(s: Session, as_of: datetime) -> str | None:
    return s.scalar(select(func.max(SourceEvent.id)).where(SourceEvent.first_observed_at <= as_of))


def has_pending_close(s: Session, parent_id: str) -> bool:
    """A close for this entry is unreviewed, or approved with an order still working."""
    closes = list(s.scalars(select(Decision.id).where(Decision.parent_decision_id == parent_id,
                                                      Decision.outcome == Outcome.trade)))
    for cid in closes:
        review = s.scalar(select(RiskReview.verdict).where(RiskReview.decision_id == cid))
        if review is None:
            return True
        if review != RiskVerdict.approve:
            continue
        oid = s.scalar(select(Order.id).where(Order.decision_id == cid))
        if oid is None:
            return True
        done = s.scalar(select(func.count()).select_from(OrderEvent).where(
            OrderEvent.order_id == oid, OrderEvent.event.in_(("terminal", "broker_rejected", "blocked", "hmac_invalid"))))
        if not done:
            return True
    return False


def last_close_at(s: Session, parent_id: str) -> datetime | None:
    return s.scalar(select(func.max(Decision.decided_at)).where(Decision.parent_decision_id == parent_id,
                                                                Decision.kind == DecisionKind.close))
