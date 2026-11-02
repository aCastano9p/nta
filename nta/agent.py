"""nta agent: source event -> signal (extract + cluster) -> entry decision; position manager.

Two crash-safe passes per tick, each leaving one append-only row per item:
1. every recent source_event without a signal gets one (LLM triage/extraction + clustering);
2. every signal without an entry decision gets exactly one: if the signal is tradeable in
   substance, a fresh quote is fetched (account D's data feed) and stored as quote kind
   'decision', and the checks run as of that quote's first_observed_at.

The same Pipeline runs inside replay with a recorded quote source, a cache-only LLM gateway
and a replay clock.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from nta.alpaca import AlpacaData, alpaca_symbol
from nta.clock import Clock
from nta.cluster import (
    Candidate,
    assign,
    cited_accessions,
    edgar_headline,
    injection_suspected,
    load_members,
    minhash,
    normalize_headline,
)
from nta.decide import (
    DecisionContent,
    MarketInputs,
    Position,
    QuoteView,
    SignalView,
    Versions,
    close_trigger,
    data_snapshot,
    decide_entry,
    exit_price,
    has_pending_close,
    last_close_at,
    load_exposure,
    load_kill_state,
    load_positions,
    load_recent_prices,
    load_ref_at_observe,
    needs_quote,
    quote_view,
    to_row,
)
from nta.extract import ExtractResult, Item, LlmGateway, ModelConfig, document_text, extract_item
from nta.label import ITEM_TITLES
from nta.logs import log
from nta.marketcal import Session as MarketSession
from nta.marketcal import is_open, sessions_around
from nta.policy import LoadedPolicy, SourceKind
from nta.schema import (
    Decision,
    DecisionKind,
    Instrument,
    KillState,
    Outcome,
    Quote,
    QuoteKind,
    Side,
    Signal,
    SourceEvent,
)

DEFAULT_LOOKBACK = timedelta(hours=24)


@dataclass(frozen=True)
class SourceMeta:
    origin: str
    source_class: str
    weight: Decimal
    kind: SourceKind


def edgar_meta(_ev: SourceEvent) -> SourceMeta:
    return SourceMeta("edgar", "primary", Decimal(1), SourceKind.filing)


class QuoteSource(Protocol):
    def decision_quote(self, ev: SourceEvent, symbol: str, instrument_id: str) -> QuoteView | None: ...
    def manage_quote(self, source_event_id: str, symbol: str, instrument_id: str) -> QuoteView | None: ...


class LiveQuotes:
    """Fetches a snapshot from account D's data feed and stores it as quote kind 'decision'."""

    def __init__(self, sf: sessionmaker[Session], alpaca: AlpacaData, clock: Clock) -> None:
        self._sf, self._alpaca, self._clock = sf, alpaca, clock

    def decision_quote(self, ev: SourceEvent, symbol: str, instrument_id: str) -> QuoteView | None:
        return self._fetch(ev.id, symbol, instrument_id, QuoteKind.decision)

    def manage_quote(self, source_event_id: str, symbol: str, instrument_id: str) -> QuoteView | None:
        return self._fetch(source_event_id, symbol, instrument_id, QuoteKind.manage)

    def _fetch(self, source_event_id: str, symbol: str, instrument_id: str, kind: QuoteKind) -> QuoteView | None:
        snaps = self._alpaca.snapshots([alpaca_symbol(symbol)])
        observed = self._clock.now()
        snap = snaps.get(alpaca_symbol(symbol))
        if snap is None:
            log("quote_missing", ticker=symbol, kind=kind.value, source_event_id=source_event_id)
            return None
        q = Quote(instrument_id=instrument_id, source_event_id=source_event_id, ts=snap.ts,
                  first_observed_at=observed, bid=snap.bid, ask=snap.ask, last=snap.last,
                  source=self._alpaca.source, kind=kind)
        with self._sf() as s, s.begin():
            s.add(q)
        return quote_view(q)


def post_text(raw: str | None) -> str:
    try:
        return str(json.loads(raw or "{}").get("text", ""))
    except (ValueError, AttributeError):
        return ""


class Pipeline:
    def __init__(self, *, sf: sessionmaker[Session], clock: Clock, gateway: LlmGateway, models: ModelConfig,
                 policy: LoadedPolicy, quotes: QuoteSource, code_sha: str,
                 source_meta: Callable[[SourceEvent], SourceMeta] = edgar_meta,
                 sessions_fn: Callable[[datetime], list[MarketSession]] = sessions_around) -> None:
        self.sf, self.clock, self.gateway, self.models = sf, clock, gateway, models
        self.policy, self.quotes, self.code_sha = policy, quotes, code_sha
        self.source_meta, self.sessions_fn = source_meta, sessions_fn

    # ---- pass 1: signals ----
    def item_for(self, ev: SourceEvent, inst: Instrument | None) -> tuple[Item, str]:
        if ev.source_id == "x":
            text = post_text(ev.raw)[:4000]
            return (Item("x", ev.published_at, inst.name if inst else None, inst.ticker if inst else None,
                         None, (), text), text)
        text, truncated = document_text(ev.raw)
        items = tuple(ev.items or [])
        headline = edgar_headline(inst.name if inst else ev.cik, ev.form_type, items, ITEM_TITLES)
        return (Item("edgar", ev.published_at, inst.name if inst else None, inst.ticker if inst else None,
                     ev.form_type, items, text, truncated), headline)

    def make_signal(self, ev: SourceEvent) -> Signal:
        with self.sf() as s:
            inst = s.get(Instrument, ev.instrument_id) if ev.instrument_id else None
        item, headline = self.item_for(ev, inst)
        result: ExtractResult = extract_item(self.gateway, self.models, item)
        meta = self.source_meta(ev)
        event_type = (result.extraction.event_type.value if result.extraction
                      else result.triage.event_type.value if result.triage else "unknown")
        cites = set(cited_accessions(item.text))
        if result.extraction and result.extraction.cited_origin:
            cites |= cited_accessions(result.extraction.cited_origin)
        cites.discard(ev.external_id)
        norm = normalize_headline(headline)
        mh = tuple(minhash(norm))
        ready_at = self.clock.now()
        cl = self.policy.policy.clustering
        window = timedelta(hours=cl.repeat_window_h)
        with self.sf() as s:
            members = load_members(s, ev.instrument_id, cites, ready_at - window, ready_at)
        cand = Candidate(ev.id, event_type, ev.instrument_id, mh, meta.origin, meta.source_class, meta.weight,
                         frozenset(cites), ready_at)
        co = self.policy.policy.corroboration
        a = assign(cand, members, window=window, threshold=cl.minhash_threshold,
                   min_origins=co.min_origins, min_weight=co.min_weight)
        sig = Signal(source_event_id=ev.id, instrument_id=ev.instrument_id, origin=meta.origin,
                     source_class=meta.source_class, source_weight=meta.weight, headline=norm, minhash=list(mh),
                     event_type=event_type, cluster_id=a.cluster_id, is_repeat=a.is_repeat,
                     corroboration=a.corroboration, corroborated=a.corroborated, status=result.status,
                     triage_call_id=result.triage_call_id, extract_call_id=result.extract_call_id,
                     extraction=result.extraction.model_dump(mode="json") if result.extraction else None,
                     flags=["injection_suspected"] if injection_suspected(item.text) else [],
                     ready_at=ready_at)
        with self.sf() as s, s.begin():
            s.add(sig)
        log("signal_written", source_event_id=ev.id, status=result.status.value, event_type=event_type,
            cluster_id=a.cluster_id, is_repeat=a.is_repeat, corroborated=a.corroborated)
        return sig

    def pending_events(self, since: datetime | None) -> list[SourceEvent]:
        with self.sf() as s:
            q = (select(SourceEvent).where(~exists().where(Signal.source_event_id == SourceEvent.id))
                 .order_by(SourceEvent.first_observed_at, SourceEvent.id))
            if since is not None:
                q = q.where(SourceEvent.first_observed_at >= since)
            return list(s.scalars(q))

    # ---- pass 2: entry decisions ----
    def pending_signals(self) -> list[Signal]:
        with self.sf() as s:
            return list(s.scalars(
                select(Signal).where(~exists().where(Decision.signal_id == Signal.id, Decision.kind == DecisionKind.entry))
                .order_by(Signal.ready_at, Signal.id)))

    def signal_view(self, sig: Signal, ev: SourceEvent, symbol: str | None) -> SignalView:
        ex = sig.extraction or {}
        return SignalView(source_event_id=ev.id, instrument_id=sig.instrument_id, symbol=symbol,
                          cluster_id=sig.cluster_id, source_kind=self.source_meta(ev).kind,
                          first_observed_at=ev.first_observed_at, status=sig.status, direction=ex.get("direction"),
                          p_up=ex.get("p_up_at_horizon"), is_repeat=sig.is_repeat, corroborated=sig.corroborated,
                          flags=tuple(sig.flags or ()))

    def market_inputs(self, s: Session, sv: SignalView, quote: QuoteView | None, as_of: datetime) -> MarketInputs:
        assert sv.instrument_id is not None
        return MarketInputs(
            as_of=as_of, quote=quote,
            ref_at_observe=load_ref_at_observe(s, sv.instrument_id, sv.source_event_id, sv.first_observed_at, as_of),
            recent_prices=load_recent_prices(s, sv.instrument_id, as_of),
            exposure=load_exposure(s, as_of, self.policy.policy), kill_state=load_kill_state(s, as_of),
            sessions=self.sessions_fn(as_of))

    def decide_signal(self, sig: Signal) -> DecisionContent:
        with self.sf() as s:
            ev = s.get(SourceEvent, sig.source_event_id)
            inst = s.get(Instrument, sig.instrument_id) if sig.instrument_id else None
        assert ev is not None
        sv = self.signal_view(sig, ev, inst.ticker if inst else None)
        m = None
        decided_at = sig.ready_at
        if needs_quote(sv):
            assert sv.symbol and sv.instrument_id
            q = self.quotes.decision_quote(ev, sv.symbol, sv.instrument_id)
            as_of = q.observed_at if q else self.clock.now()
            with self.sf() as s:
                m = self.market_inputs(s, sv, q, as_of)
                snap = data_snapshot(s, as_of)
            decided_at = as_of
        else:
            with self.sf() as s:
                snap = data_snapshot(s, decided_at)
        versions = Versions(self.code_sha, self.policy.hash, self.models.prompt_version, self.models.model_id, snap)
        content = decide_entry(sv, m, self.policy.policy, versions, decided_at)
        with self.sf() as s, s.begin():
            s.add(to_row(content, signal_id=sig.id))
        log("decision_written", kind="entry", source_event_id=ev.id, symbol=sv.symbol, outcome=content.outcome.value,
            reasons=list(content.reasons), qty=content.qty, content_hash=content.content_hash,
            decide_latency_s=round((decided_at - ev.first_observed_at).total_seconds(), 3))
        return content

    def step(self, since: datetime | None) -> tuple[int, int]:
        """Each new event is decided right after its signal is written; the second loop picks
        up signals left without a decision by a crash."""
        signals = decisions = 0
        for ev in self.pending_events(since):
            self.decide_signal(self.make_signal(ev))
            signals += 1
            decisions += 1
        for sig in self.pending_signals():
            self.decide_signal(sig)
            decisions += 1
        return signals, decisions


class PositionManager:
    """Watches every open position's plan (stop, take-profit, expiry) and writes a close
    decision when one triggers. The close goes through nta risk like any order.

    Runs during market hours only (a close cannot fill otherwise). Keeps running under PAUSE
    and FLATTEN; stands down under HALT (nta risk would refuse anyway). After a close
    decision, the same position is not re-closed for `retry_after` unless that close is
    still pending, so a rejected close is retried without spamming.
    """

    def __init__(self, pipeline: Pipeline, *, every: timedelta = timedelta(seconds=5),
                 retry_after: timedelta = timedelta(seconds=60)) -> None:
        self.p = pipeline
        self.every, self.retry_after = every, retry_after
        self._last_run: datetime | None = None

    def open_positions(self, now: datetime) -> list[tuple[Position, Decision]]:
        with self.p.sf() as s:
            positions = [x for x in load_positions(s, now, self.p.policy.policy) if x.qty > 0 and x.entry_decision_id]
            entries = {d.id: d for d in s.scalars(select(Decision).where(
                Decision.id.in_([x.entry_decision_id for x in positions])))} if positions else {}
        return [(x, entries[x.entry_decision_id]) for x in positions if x.entry_decision_id in entries]

    def check(self, pos: Position, entry: Decision, now: datetime) -> DecisionContent | None:
        policy = self.p.policy
        with self.p.sf() as s:
            if has_pending_close(s, entry.id):
                return None
            last = last_close_at(s, entry.id)
        if last is not None and now - last < self.retry_after:
            return None
        assert entry.symbol and entry.instrument_id and entry.plan
        q = self.p.quotes.manage_quote(entry.source_event_id, entry.symbol, entry.instrument_id)
        if q is None:
            return None
        price = exit_price(q, policy.policy)
        trigger = close_trigger(entry.plan, price, q.observed_at)
        if trigger is None:
            return None
        with self.p.sf() as s:
            snap = data_snapshot(s, q.observed_at)
        plan = {"trigger": trigger, "exit_ref": str(price) if price is not None else None,
                "stop": entry.plan["stop"], "take_profit": entry.plan["take_profit"], "expiry": entry.plan["expiry"]}
        c = DecisionContent(kind="close", source_event_id=entry.source_event_id, instrument_id=entry.instrument_id,
                            symbol=entry.symbol, cluster_id=entry.cluster_id, outcome=Outcome.trade, reasons=(trigger,),
                            side=Side.sell, qty=int(pos.qty), quote=q.as_json(), exposure={}, plan=plan,
                            expected_cost_bps=None, spread_unverified=policy.policy.spread_unverified,
                            hypothetical=None, decided_at=q.observed_at,
                            versions=Versions(self.p.code_sha, policy.hash, self.p.models.prompt_version,
                                              self.p.models.model_id, snap),
                            parent_source_event_id=entry.source_event_id)
        with self.p.sf() as s, s.begin():
            s.add(to_row(c, parent_decision_id=entry.id))
        log("decision_written", kind="close", trigger=trigger, symbol=entry.symbol, qty=int(pos.qty),
            price=str(price), entry_decision_id=entry.id, content_hash=c.content_hash)
        return c

    def step(self, force: bool = False) -> int:
        now = self.p.clock.now()
        if not force and self._last_run is not None and now - self._last_run < self.every:
            return 0
        self._last_run = now
        if not is_open(self.p.sessions_fn(now), now):
            return 0
        with self.p.sf() as s:
            if load_kill_state(s, now) == KillState.HALT:
                return 0
        return sum(1 for pos, entry in self.open_positions(now) if self.check(pos, entry, now) is not None)


class AgentProcess:
    """Long-running loop around the pipeline (and, once built, the position manager)."""

    def __init__(self, pipeline: Pipeline, clock: Clock, *, run_id: str | None = None,
                 lookback: timedelta = DEFAULT_LOOKBACK, manager: "Callable[[], int] | None" = None) -> None:
        import threading

        self.pipeline, self.clock, self.run_id, self.lookback = pipeline, clock, run_id, lookback
        self.manager = manager
        self._lock = threading.Lock()
        self.ticks_ok = self.ticks_failed = 0
        self.signals = self.decisions = self.closes = 0
        self.last_success_monotonic: float | None = None

    def tick(self) -> None:
        s, d = self.pipeline.step(self.clock.now() - self.lookback)
        c = self.manager() if self.manager else 0
        with self._lock:
            self.signals += s
            self.decisions += d
            self.closes += c
            self.ticks_ok += 1
            self.last_success_monotonic = self.clock.monotonic()

    def run(self, stop, tick_s: float = 1.0) -> None:  # noqa: ANN001 (StopFlag)
        while not stop.is_set():
            started = self.clock.monotonic()
            try:
                self.tick()
            except Exception as exc:
                with self._lock:
                    self.ticks_failed += 1
                log("agent_tick_failed", error=f"{type(exc).__name__}: {exc}")
            stop.wait(max(0.0, tick_s - (self.clock.monotonic() - started)))

    def health(self) -> dict:
        with self._lock:
            since = (None if self.last_success_monotonic is None
                     else round(self.clock.monotonic() - self.last_success_monotonic, 3))
            return {"seconds_since_last_success": since, "ticks_ok": self.ticks_ok, "ticks_failed": self.ticks_failed,
                    "signals": self.signals, "decisions": self.decisions, "closes": self.closes,
                    "llm_calls": self.pipeline.gateway.calls_made, "policy_version": self.pipeline.policy.hash,
                    "run_id": self.run_id}
