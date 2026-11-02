"""nta risk: independent risk check, signing, execution and order sync on paper account T.

This module never imports the LLM client and never reads free text: it selects explicit
columns from decisions (which hold no free text), orders, order_events, fills, risk_reviews,
position_snapshots, the `state` column of kill_switch_events and incidents it wrote itself.

Per tick: reconcile (on startup and every 30 s, before any order) -> sync open orders and
record fills -> FLATTEN/HALT handling -> review new trade decisions -> execute signed orders
-> account D watchdog.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from nta.broker import (
    Broker,
    BrokerOrder,
    BrokerRejected,
    BrokerTimeout,
    client_order_id,
    sign_order,
    verify_order,
)
from nta.clock import Clock
from nta.decide import (
    CENT,
    FINAL_ORDER_EVENTS,
    DecisionContent,
    QuoteView,
    Versions,
    has_pending_close,
    last_close_at,
    load_kill_state,
    load_positions,
    reference_price,
    to_row,
)
from nta.logs import log
from nta.marketcal import Session as MarketSession
from nta.marketcal import is_open, sessions_around
from nta.policy import LoadedPolicy
from nta.schema import (
    Decision,
    DecisionKind,
    Fill,
    Incident,
    KillState,
    KillSwitchEvent,
    Order,
    OrderEvent,
    Outcome,
    PositionSnapshot,
    RiskReview,
    RiskVerdict,
    Side,
)

PROCESS = "risk"
MAX_DECISION_AGE = timedelta(seconds=30)
MAX_APPROVAL_AGE = timedelta(seconds=30)
CLOSE_RETRY = timedelta(seconds=60)
RECONCILE_EVERY_S = 30.0
# An order is "sent" (the executor is done with it) after one of these events.
SEND_FINAL = ("submitted", "found_on_lookup", "broker_rejected", "hmac_invalid", "blocked", "no_trade_decision")
SENT_TO_BROKER = ("submitted", "found_on_lookup")


@dataclass(frozen=True)
class DecisionRow:
    id: str
    kind: str
    parent_decision_id: str | None
    source_event_id: str
    instrument_id: str | None
    symbol: str
    cluster_id: str | None
    side: str
    qty: int
    plan: dict[str, Any]
    decided_at: datetime
    policy_version: str


DECISION_COLUMNS = (Decision.id, Decision.kind, Decision.parent_decision_id, Decision.source_event_id,
                    Decision.instrument_id, Decision.symbol, Decision.cluster_id, Decision.side, Decision.qty,
                    Decision.plan, Decision.decided_at, Decision.policy_version)


def qty_str(v: Decimal | int) -> str:
    """49.000000 -> '49', 0.5 -> '0.5'."""
    return format(Decimal(v).normalize(), "f")


def _d(row) -> DecisionRow:  # noqa: ANN001
    return DecisionRow(*row)


# ---------- shared helpers (also used by the kill CLI and the dashboard) ----------

def last_human_resume(s: Session) -> datetime | None:
    return s.scalar(select(func.max(KillSwitchEvent.at)).where(KillSwitchEvent.state == KillState.RUN,
                                                                KillSwitchEvent.actor == "human"))


def open_incidents(s: Session) -> list[Incident]:
    since = last_human_resume(s)
    q = select(Incident).order_by(Incident.at)
    if since is not None:
        q = q.where(Incident.at > since)
    return list(s.scalars(q))


def write_kill(s: Session, state: KillState, actor: str, reason: str, at: datetime, incident_id: str | None = None) -> None:
    s.add(KillSwitchEvent(state=state, actor=actor, reason=reason, at=at, incident_id=incident_id))


def working_remainders(s: Session, side: Side) -> dict[str, Decimal]:
    """Unfilled quantity per symbol of signed orders on `side` that are not finished: approved
    entries count against caps before they fill, working sells against what may still be sold."""
    done = exists().where(and_(OrderEvent.order_id == Order.id, OrderEvent.event.in_(FINAL_ORDER_EVENTS)))
    out: dict[str, Decimal] = {}
    for oid, sym, qty in s.execute(select(Order.id, Order.symbol, Order.qty).where(Order.side == side, ~done)):
        filled = s.scalar(select(func.coalesce(func.max(Fill.cum_qty), 0)).where(Fill.order_id == oid)) or 0
        rem = Decimal(qty) - Decimal(filled)
        if rem > 0:
            out[sym] = out.get(sym, Decimal(0)) + rem
    return out


class RiskService:
    def __init__(self, *, sf: sessionmaker[Session], clock: Clock, broker: Broker,
                 quotes: Callable[[str], QuoteView | None], policy: LoadedPolicy, signing_key: str,
                 run_id: str | None = None, watchdog: Broker | None = None, code_sha: str = "unknown",
                 sessions_fn: Callable[[datetime], list[MarketSession]] = sessions_around,
                 fault: Callable[[str], None] | None = None) -> None:
        self.sf, self.clock, self.broker, self.quotes = sf, clock, broker, quotes
        self.loaded, self.policy = policy, policy.policy
        self._key = signing_key
        self.run_id, self.watchdog, self.code_sha = run_id, watchdog, code_sha
        self.sessions_fn = sessions_fn
        self._fault = fault or (lambda point: None)
        self._lock = threading.Lock()
        self._last_reconcile: float | None = None
        self._last_watchdog: float | None = None
        self.reconciled_once = False
        self.ticks_ok = self.ticks_failed = 0
        self.last_success_monotonic: float | None = None
        self.counts = {"approved": 0, "rejected": 0, "submitted": 0, "fills": 0}

    # ---------- incidents and kill switch ----------
    def raise_incident(self, s: Session, kind: str, severity: int, detail: dict[str, Any], *,
                       pause: bool = True, actor: str = PROCESS) -> str | None:
        """One incident per kind until the next human resume. Optionally sets PAUSE."""
        if any(i.kind == kind for i in open_incidents(s)):
            return None
        now = self.clock.now()
        inc = Incident(severity=severity, kind=kind, process=PROCESS, detail=detail, at=now)
        s.add(inc)
        s.flush()
        if pause and load_kill_state(s, now) == KillState.RUN:
            write_kill(s, KillState.PAUSE, actor, f"incident {kind}", now, inc.id)
        log("incident", kind=kind, severity=severity, incident_id=inc.id, **{"detail": detail})
        return inc.id

    # ---------- review ----------
    def pending_decisions(self, s: Session) -> list[DecisionRow]:
        return [_d(r) for r in s.execute(
            select(*DECISION_COLUMNS).where(Decision.outcome == Outcome.trade,
                                            ~exists().where(RiskReview.decision_id == Decision.id))
            .order_by(Decision.decided_at, Decision.id))]

    def _market_open(self, now: datetime) -> bool:
        return is_open(self.sessions_fn(now), now)

    def _quote_reasons(self, q: QuoteView | None, now: datetime) -> list[str]:
        if q is None or reference_price(q, self.policy) is None:
            return ["quote_missing"]
        out = []
        if (now - q.ts).total_seconds() > self.policy.quote_max_age_s:
            out.append("quote_stale")
        if not self.policy.spread_unverified:
            spread = q.spread_bps()
            if spread is None or spread > self.policy.spread_limit_bps:
                out.append("spread_wide")
        return out

    def _peak_equity(self, s: Session, equity: Decimal) -> Decimal:
        peak = s.scalar(select(func.max(PositionSnapshot.equity)).where(PositionSnapshot.account == "T"))
        return max(Decimal(peak), equity) if peak is not None else equity

    def review_entry(self, s: Session, d: DecisionRow, now: datetime) -> tuple[list[str], dict, QuoteView | None, int, Decimal]:
        p = self.policy
        reasons: list[str] = []
        checks: dict[str, Any] = {}
        if load_kill_state(s, now) != KillState.RUN:
            reasons.append("kill_switch")
        if now - d.decided_at > MAX_DECISION_AGE:
            reasons.append("decision_stale")
        if not self._market_open(now):
            reasons.append("market_closed")
        if d.side != Side.buy:
            reasons.append("long_only")
        q = self.quotes(d.symbol)
        reasons += self._quote_reasons(q, now)
        limit = Decimal(d.plan["entry"])
        stop = Decimal(d.plan["stop"])
        ref = reference_price(q, p) if q else None
        if ref is not None:
            checks["ref_price"] = str(ref)
            if ref > limit:
                reasons.append("price_above_limit")
            if ref <= stop:
                reasons.append("price_below_stop")
        acct = self.broker.account()
        positions = {k: v for k, v in self.broker.positions().items() if v != 0}
        for sym, rem in working_remainders(s, Side.buy).items():  # approved, not yet filled
            positions[sym] = positions.get(sym, Decimal(0)) + rem
        checks.update(equity=str(acct.equity), last_equity=str(acct.last_equity))
        if acct.equity <= acct.last_equity * (1 - p.daily_loss_stop_pct / 100):
            reasons.append("daily_loss_stop")
        peak = self._peak_equity(s, acct.equity)
        checks["peak_equity"] = str(peak)
        if acct.equity <= peak * (1 - p.drawdown_pause_pct / 100):
            reasons.append("drawdown_pause")
            self.raise_incident(s, "drawdown_pause", 2, {"equity": str(acct.equity), "peak": str(peak)})
        # caps against what account T actually holds, valued at the risk quote (or the limit)
        px = ref or limit
        qty = d.qty
        pct = Decimal(100)
        if d.symbol not in positions and len(positions) >= p.max_open_positions:
            qty = 0
            reasons.append("max_open_positions")
        log_pos = load_positions(s, now, p)
        cluster_of = {pos.symbol: pos.cluster_id for pos in log_pos}

        def room(limit_pct: Decimal, held: Decimal) -> int:
            return int(max(acct.equity * limit_pct / pct - held, Decimal(0)) / limit) if limit > 0 else 0

        held_sym = abs(positions.get(d.symbol, Decimal(0))) * px
        held_sector = sum(abs(q_) * px for sym, q_ in positions.items() if p.sector(sym) == p.sector(d.symbol))
        held_cluster = sum(abs(q_) * px for sym, q_ in positions.items()
                           if d.cluster_id and cluster_of.get(sym) == d.cluster_id)
        allowed = min(room(p.position_cap_pct, held_sym), room(p.sector_cap_pct, held_sector),
                      room(p.cluster_cap_pct, held_cluster) if d.cluster_id else qty)
        checks["allowed_qty"] = allowed
        qty = min(qty, allowed)
        if qty < 1 and "max_open_positions" not in reasons:
            reasons.append("cap_hit")
        return reasons, checks, q, qty, limit

    def review_close(self, s: Session, d: DecisionRow, now: datetime) -> tuple[list[str], dict, QuoteView | None, int, Decimal]:
        reasons: list[str] = []
        checks: dict[str, Any] = {}
        if load_kill_state(s, now) == KillState.HALT:
            reasons.append("kill_switch")
        if not self._market_open(now):
            reasons.append("market_closed")
        held_log = next((pos.qty for pos in load_positions(s, now, self.policy)
                         if pos.entry_decision_id == d.parent_decision_id), Decimal(0))
        held_broker = self.broker.positions().get(d.symbol, Decimal(0))
        selling = working_remainders(s, Side.sell).get(d.symbol, Decimal(0))  # e.g. a flatten and a stop at once
        qty = int(min(Decimal(d.qty), held_log - selling, max(held_broker - selling, Decimal(0))))
        checks.update(held_log=str(held_log), held_broker=str(held_broker), working_sells=str(selling))
        if qty < 1:
            reasons.append("no_position")
        q = self.quotes(d.symbol)
        ref = (q.last or q.bid) if q else None
        if ref is None:
            reasons.append("quote_missing")
            limit = Decimal(0)
        else:
            limit = (ref * (1 - self.policy.exit_limit_bps / 10000)).quantize(CENT, ROUND_HALF_UP)
            checks["ref_price"] = str(ref)
        return reasons, checks, q, qty, limit

    def review(self, d: DecisionRow) -> RiskVerdict:
        now = self.clock.now()
        with self.sf() as s, s.begin():
            if d.policy_version != self.loaded.hash:
                reasons, checks, q, qty, limit = ["policy_mismatch"], {"risk_policy": self.loaded.hash}, None, 0, Decimal(0)
            elif d.kind == DecisionKind.entry:
                reasons, checks, q, qty, limit = self.review_entry(s, d, now)
            else:
                reasons, checks, q, qty, limit = self.review_close(s, d, now)
            verdict = RiskVerdict.reject if reasons else RiskVerdict.approve
            s.add(RiskReview(decision_id=d.id, verdict=verdict, reasons=reasons, quote=q.as_json() if q else None,
                             checks=checks, policy_version=self.loaded.hash, reviewed_at=now))
            if verdict == RiskVerdict.approve:
                side = Side.buy if d.kind == DecisionKind.entry else Side.sell
                s.add(Order(decision_id=d.id, leg=1, client_order_id=client_order_id(d.id, 1), symbol=d.symbol,
                            side=side, qty=qty, limit_price=limit, time_in_force="day",
                            hmac=sign_order(self._key, d.id, side.value, qty, limit), policy_version=self.loaded.hash))
        key = "approved" if verdict == RiskVerdict.approve else "rejected"
        self.counts[key] += 1
        log("risk_review", decision_id=d.id, kind=d.kind, symbol=d.symbol, verdict=verdict.value, reasons=reasons,
            qty=qty, limit=str(limit))
        if verdict == RiskVerdict.approve:
            self._fault("after_order_row")
        return verdict

    def review_pending(self) -> int:
        with self.sf() as s:
            pending = self.pending_decisions(s)
        for d in pending:
            self.review(d)
        return len(pending)

    # ---------- execution ----------
    def unsent_orders(self, s: Session) -> list[Order]:
        return list(s.scalars(select(Order).where(~exists().where(
            and_(OrderEvent.order_id == Order.id, OrderEvent.event.in_(SEND_FINAL)))).order_by(Order.id)))

    def _event(self, order: Order, event: str, b: BrokerOrder | None = None, **detail: Any) -> None:
        with self.sf() as s, s.begin():
            s.add(OrderEvent(order_id=order.id, event=event, broker_order_id=b.id if b else None,
                             broker_status=b.status if b else None, filled_qty=b.filled_qty if b else None,
                             filled_avg_price=b.filled_avg_price if b else None,
                             detail={k: str(v) for k, v in detail.items()}, first_observed_at=self.clock.now()))
        log("order_event", order_id=order.id, client_order_id=order.client_order_id, step=event,
            broker_status=b.status if b else None, **{k: str(v) for k, v in detail.items()})

    def execute(self, order: Order) -> str:
        with self.sf() as s:
            dec = s.execute(select(Decision.outcome, Decision.kind).where(Decision.id == order.decision_id)).first()
            kill = load_kill_state(s, self.clock.now())
            approved_at = s.scalar(select(RiskReview.reviewed_at).where(RiskReview.decision_id == order.decision_id))
        if not verify_order(self._key, order.decision_id, order.side.value, order.qty, order.limit_price, order.hmac):
            self._event(order, "hmac_invalid")
            with self.sf() as s, s.begin():
                self.raise_incident(s, "hmac_invalid", 1, {"order_id": order.id, "client_order_id": order.client_order_id})
            return "hmac_invalid"
        if dec is None or dec[0] != Outcome.trade:
            self._event(order, "no_trade_decision")
            with self.sf() as s, s.begin():
                self.raise_incident(s, "order_without_trade_decision", 1, {"order_id": order.id})
            return "no_trade_decision"
        is_entry = dec[1] == DecisionKind.entry
        if kill == KillState.HALT or (is_entry and kill != KillState.RUN):
            self._event(order, "blocked", kill_state=kill.value)
            return "blocked"
        found = self.broker.get_by_client_order_id(order.client_order_id)  # never re-send blindly
        if found is not None:
            self._event(order, "found_on_lookup", found)
            return "found_on_lookup"
        now = self.clock.now()
        if not self._market_open(now) or approved_at is None or now - approved_at > MAX_APPROVAL_AGE:
            # approved, then risk was down: the approval no longer describes the market
            self._event(order, "blocked", reason="stale_approval" if self._market_open(now) else "market_closed")
            return "blocked"
        self._event(order, "submit_attempt")
        try:
            b = self.broker.submit_order(client_order_id=order.client_order_id, symbol=order.symbol,
                                         side=order.side.value, qty=order.qty, limit_price=order.limit_price,
                                         time_in_force=order.time_in_force)
        except BrokerTimeout as exc:
            found = self.broker.get_by_client_order_id(order.client_order_id)
            if found is not None:
                self._event(order, "found_on_lookup", found, after="timeout")
                return "found_on_lookup"
            self._event(order, "submit_timeout", error=exc)  # not final: next tick looks up first again
            return "submit_timeout"
        except BrokerRejected as exc:
            if exc.status == 422 and "client_order_id" in exc.body:  # duplicate id: it is already there
                found = self.broker.get_by_client_order_id(order.client_order_id)
                if found is not None:
                    self._event(order, "found_on_lookup", found, after="duplicate")
                    return "found_on_lookup"
            self._event(order, "broker_rejected", status=exc.status, body=exc.body[:300])
            return "broker_rejected"
        self._fault("after_submit")
        self._event(order, "submitted", b)
        self.counts["submitted"] += 1
        return "submitted"

    def execute_pending(self) -> int:
        with self.sf() as s:
            orders = self.unsent_orders(s)
        for o in orders:
            self.execute(o)
        return len(orders)

    # ---------- order sync and fills ----------
    def working_orders(self, s: Session) -> list[Order]:
        sent = exists().where(and_(OrderEvent.order_id == Order.id, OrderEvent.event.in_(SENT_TO_BROKER)))
        done = exists().where(and_(OrderEvent.order_id == Order.id, OrderEvent.event == "terminal"))
        return list(s.scalars(select(Order).where(sent, ~done).order_by(Order.id)))

    def _last_state(self, s: Session, order_id: str) -> tuple[str | None, Decimal, Decimal | None, datetime | None, bool]:
        evs = list(s.execute(select(OrderEvent.event, OrderEvent.broker_status, OrderEvent.filled_qty,
                                    OrderEvent.filled_avg_price, OrderEvent.first_observed_at)
                             .where(OrderEvent.order_id == order_id).order_by(OrderEvent.first_observed_at, OrderEvent.id)))
        status, qty, avg, sent_at, cancel = None, Decimal(0), None, None, False
        filled = s.scalar(select(func.max(Fill.cum_qty)).where(Fill.order_id == order_id))
        for ev, st, fq, fa, at in evs:
            if ev in SENT_TO_BROKER and sent_at is None:
                sent_at = at
            if ev == "cancel_requested":
                cancel = True
            if st is not None:
                status = st
            if fa is not None:
                avg = fa
        qty = Decimal(filled) if filled is not None else Decimal(0)
        return status, qty, avg, sent_at, cancel

    def sync_order(self, order: Order) -> None:
        b = self.broker.get_by_client_order_id(order.client_order_id)
        now = self.clock.now()
        with self.sf() as s:
            status, recorded_qty, _, sent_at, cancel = self._last_state(s, order.id)
            prev_cost = s.scalar(select(func.coalesce(func.sum(Fill.qty * Fill.price), 0)).where(Fill.order_id == order.id))
        if b is None:
            with self.sf() as s, s.begin():
                self.raise_incident(s, "order_missing_at_broker", 2, {"client_order_id": order.client_order_id})
            return
        if b.filled_qty > recorded_qty:
            delta = b.filled_qty - recorded_qty
            avg = b.filled_avg_price or order.limit_price
            price = ((avg * b.filled_qty - Decimal(prev_cost)) / delta).quantize(Decimal("0.000001"))
            with self.sf() as s, s.begin():
                s.add(Fill(order_id=order.id, decision_id=order.decision_id, symbol=order.symbol, side=order.side,
                           qty=delta, price=price, cum_qty=b.filled_qty, broker_ts=b.updated_at, first_observed_at=now))
            self.counts["fills"] += 1
            log("fill", order_id=order.id, symbol=order.symbol, side=order.side.value, qty=str(delta), price=str(price),
                cum_qty=str(b.filled_qty))
        if b.status != status or b.filled_qty != recorded_qty:
            self._event(order, "status", b)
        if b.terminal:
            self._event(order, "terminal", b)
            return
        if (order.side == Side.buy and not cancel and sent_at is not None
                and (now - sent_at).total_seconds() > self.policy.entry_ttl_s):
            self.broker.cancel(b.id)
            self._event(order, "cancel_requested", b, reason="entry_ttl")

    def sync_orders(self) -> int:
        with self.sf() as s:
            orders = self.working_orders(s)
        for o in orders:
            self.sync_order(o)
        return len(orders)

    # ---------- kill switch actions ----------
    def handle_kill_state(self) -> None:
        now = self.clock.now()
        with self.sf() as s:
            state = load_kill_state(s, now)
        if state == KillState.HALT:
            for b in self.broker.open_orders():
                self.broker.cancel(b.id)
                log("halt_cancel", broker_order_id=b.id, client_order_id=b.client_order_id)
        elif state == KillState.FLATTEN:
            self.flatten(now)

    def flatten(self, now: datetime) -> int:
        """Write a close decision for every open position without a pending close."""
        written = 0
        if not self._market_open(now):
            return 0  # a close cannot fill; flatten resumes at the open
        with self.sf() as s:
            positions = [p for p in load_positions(s, now, self.policy) if p.qty > 0]
            todo = []
            for p in positions:
                if not p.entry_decision_id or has_pending_close(s, p.entry_decision_id):
                    continue
                last = last_close_at(s, p.entry_decision_id)
                if last is not None and now - last < CLOSE_RETRY:
                    continue  # a close was just rejected or died: retry after a minute, not every tick
                todo.append(p)
            entries = {r.id: _d(r) for r in s.execute(select(*DECISION_COLUMNS).where(
                Decision.id.in_([p.entry_decision_id for p in todo])))} if todo else {}
        for p in todo:
            e = entries[p.entry_decision_id]
            c = DecisionContent(kind="close", source_event_id=e.source_event_id, instrument_id=e.instrument_id,
                                symbol=e.symbol, cluster_id=e.cluster_id, outcome=Outcome.trade, reasons=("flatten",),
                                side=Side.sell, qty=int(p.qty), quote=None, exposure={},
                                plan={"trigger": "flatten", "entry_decision_id": e.id}, expected_cost_bps=None,
                                spread_unverified=self.policy.spread_unverified, hypothetical=None, decided_at=now,
                                versions=Versions(self.code_sha, self.loaded.hash, "none", "none", None),
                                parent_source_event_id=e.source_event_id)
            with self.sf() as s, s.begin():
                s.add(to_row(c, parent_decision_id=e.id))
            log("flatten_close_written", entry_decision_id=e.id, symbol=e.symbol, qty=int(p.qty))
            written += 1
        return written

    # ---------- reconciliation (account T) ----------
    def log_positions_by_symbol(self, s: Session, now: datetime) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in load_positions(s, now, self.policy):
            out[p.symbol] = out.get(p.symbol, Decimal(0)) + p.qty
        return {k: v for k, v in out.items() if v != 0}

    def reconcile(self) -> bool:
        """Compare account T's positions and open orders with the log; write a snapshot.
        Any mismatch raises an incident, which sets PAUSE. Returns True if it matched."""
        self.resolve_in_flight()  # an order the broker took before we recorded it is ours
        problems, acct, broker_pos, broker_open, log_pos = self._compare()
        if problems:  # a fill can land between the order sync and the positions read: look again once
            problems, acct, broker_pos, broker_open, log_pos = self._compare()
        now = self.clock.now()  # stamped after the broker reads, just before the insert
        with self.sf() as s, s.begin():
            s.add(PositionSnapshot(account="T", taken_at=now, equity=acct.equity, last_equity=acct.last_equity,
                                   cash=acct.cash, positions={k: qty_str(v) for k, v in broker_pos.items()},
                                   open_orders=[o.client_order_id or o.id for o in broker_open],
                                   log_positions={k: qty_str(v) for k, v in log_pos.items()},
                                   mismatch=bool(problems), detail=problems, run_id=self.run_id))
            if problems:
                self.raise_incident(s, "reconcile_mismatch", 2, problems, actor="reconciler")
        log("reconciled", mismatch=bool(problems), equity=str(acct.equity), positions=len(broker_pos),
            open_orders=len(broker_open), **({"problems": problems} if problems else {}))
        return not problems

    def resolve_in_flight(self) -> None:
        """Unsent orders that already had a submit attempt may be at the broker: look them up."""
        with self.sf() as s:
            tried = [o for o in self.unsent_orders(s) if s.scalar(select(func.count()).select_from(OrderEvent).where(
                OrderEvent.order_id == o.id, OrderEvent.event == "submit_attempt"))]
        for o in tried:
            found = self.broker.get_by_client_order_id(o.client_order_id)
            if found is not None:
                self._event(o, "found_on_lookup", found, after="reconcile")

    def _compare(self):  # noqa: ANN202
        self.sync_orders()  # record fills that happened since the last tick first
        acct = self.broker.account()
        broker_pos = {k: v for k, v in self.broker.positions().items() if v != 0}
        broker_open = self.broker.open_orders()
        with self.sf() as s:
            log_pos = self.log_positions_by_symbol(s, self.clock.now())
            known = set(s.scalars(select(Order.client_order_id).where(
                Order.client_order_id.in_([o.client_order_id for o in broker_open])))) if broker_open else set()
        problems: dict[str, Any] = {}
        for sym in sorted(set(broker_pos) | set(log_pos)):
            if broker_pos.get(sym, Decimal(0)) != log_pos.get(sym, Decimal(0)):
                problems.setdefault("positions", {})[sym] = {"broker": qty_str(broker_pos.get(sym, 0)),
                                                             "log": qty_str(log_pos.get(sym, 0))}
        unknown = [o.client_order_id or o.id for o in broker_open if o.client_order_id not in known]
        if unknown:
            problems["unknown_open_orders"] = unknown
        return problems, acct, broker_pos, broker_open, log_pos

    def reconcile_if_due(self) -> bool:
        """On startup nothing is sent until one reconcile has completed; afterwards every 30 s.
        Returns False when orders must not go out this tick."""
        mono = self.clock.monotonic()
        if self.reconciled_once and self._last_reconcile is not None and mono - self._last_reconcile < RECONCILE_EVERY_S:
            return True
        try:
            self.reconcile()
        except Exception as exc:
            log("reconcile_failed", error=f"{type(exc).__name__}: {exc}")
            return self.reconciled_once  # before the first success: send nothing
        self._last_reconcile = mono
        self.reconciled_once = True
        return True

    # ---------- watchdog (account D) ----------
    def watchdog_check(self) -> bool:
        """Account D is data-only. Any order or position there is a severity-1 incident."""
        if self.watchdog is None:
            return True
        pos = {k: v for k, v in self.watchdog.positions().items() if v != 0}
        orders = self.watchdog.open_orders()
        now = self.clock.now()
        bad = bool(pos or orders)
        with self.sf() as s, s.begin():
            s.add(PositionSnapshot(account="D", taken_at=now, positions={k: qty_str(v) for k, v in pos.items()},
                                   open_orders=[o.client_order_id or o.id for o in orders], mismatch=bad,
                                   detail={}, run_id=self.run_id))
            if bad:
                self.raise_incident(s, "account_d_activity", 1,
                                    {"positions": {k: qty_str(v) for k, v in pos.items()},
                                     "open_orders": [o.symbol for o in orders]}, actor="watchdog")
        return not bad

    def watchdog_if_due(self) -> None:
        mono = self.clock.monotonic()
        if self._last_watchdog is not None and mono - self._last_watchdog < RECONCILE_EVERY_S:
            return
        try:
            self.watchdog_check()
        except Exception as exc:
            log("watchdog_failed", error=f"{type(exc).__name__}: {exc}")
        self._last_watchdog = mono

    # ---------- loop ----------
    def tick(self) -> None:
        if not self.reconcile_if_due():
            return
        self.sync_orders()
        self.handle_kill_state()
        self.review_pending()
        self.execute_pending()
        self.watchdog_if_due()
        with self._lock:
            self.ticks_ok += 1
            self.last_success_monotonic = self.clock.monotonic()

    def run(self, stop, tick_s: float = 0.5) -> None:  # noqa: ANN001 (StopFlag)
        while not stop.is_set():
            started = self.clock.monotonic()
            try:
                self.tick()
            except Exception as exc:
                with self._lock:
                    self.ticks_failed += 1
                log("risk_tick_failed", error=f"{type(exc).__name__}: {exc}")
            stop.wait(max(0.0, tick_s - (self.clock.monotonic() - started)))

    def health(self) -> dict:
        with self.sf() as s:
            state = load_kill_state(s, self.clock.now())
            incidents = [{"id": i.id, "kind": i.kind, "severity": i.severity, "at": i.at.isoformat()}
                         for i in open_incidents(s)]
        with self._lock:
            since = (None if self.last_success_monotonic is None
                     else round(self.clock.monotonic() - self.last_success_monotonic, 3))
            return {"seconds_since_last_success": since, "kill_switch": state.value, "open_incidents": incidents,
                    "unhealthy": bool(incidents) or not self.reconciled_once,
                    "ticks_ok": self.ticks_ok, "ticks_failed": self.ticks_failed, **self.counts,
                    "policy_version": self.loaded.hash, "run_id": self.run_id}




def alpaca_quote_fn(alpaca, clock: Clock) -> Callable[[str], QuoteView | None]:  # noqa: ANN001 (AlpacaData)
    """Risk's own quote, from account T's data feed. Kept in risk_reviews.quote, not in quotes."""
    from nta.alpaca import alpaca_symbol

    def fetch(symbol: str) -> QuoteView | None:
        snap = alpaca.snapshots([alpaca_symbol(symbol)]).get(alpaca_symbol(symbol))
        if snap is None:
            return None
        return QuoteView(snap.bid, snap.ask, snap.last, snap.ts, clock.now(), alpaca.source + "_t")
    return fetch
