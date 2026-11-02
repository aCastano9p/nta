"""Dashboard pages for the phase 1 decision loop (read-only, server-rendered, no JavaScript).

Shown: kill switch, incidents, decisions with reason codes, orders and fills, positions,
quadrant counts and LLM-vs-human agreement, the estimate ledger, LLM and X spend, and the
share of decisions flagged spread_unverified. Free text (extractions, theses, filing text)
is not shown here; every value is HTML-escaped.
"""

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from nta.decide import load_kill_state, load_positions
from nta.extract import ny_day_start
from nta.policy import DEFAULT_POLICY_PATH, load_policy
from nta.schema import (
    Decision,
    Estimate,
    Fill,
    Incident,
    KillSwitchEvent,
    LlmCall,
    Order,
    OrderEvent,
    PositionSnapshot,
    Postmortem,
    RiskReview,
    XPoll,
)

LIMIT = 200


def _esc(v: object) -> str:
    from nta.dashboard import esc
    return esc(v)


def _ny(t: datetime | None) -> str:
    from nta.dashboard import ny
    return ny(t)


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{_esc(empty)}</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="panel" style="overflow-x:auto"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _usd(v: Decimal | None) -> str:
    return "–" if v is None else f"${v:,.4f}"


def _pct(n: int, d: int) -> str:
    return "–" if d == 0 else f"{100 * n / d:.0f}%"


def _kappa(v: float | None) -> str:
    return "–" if v is None else f"{v:.2f}"


def _open_incidents(s: Session) -> list[Incident]:
    from nta.risk import open_incidents
    return open_incidents(s)


# ---------- data ----------

def spread_share(s: Session) -> tuple[int, int]:
    total = s.scalar(select(func.count()).select_from(Decision).where(Decision.quote.is_not(None))) or 0
    flagged = s.scalar(select(func.count()).select_from(Decision).where(Decision.quote.is_not(None),
                                                                        Decision.spread_unverified.is_(True))) or 0
    return flagged, total


def llm_spend(s: Session, now: datetime) -> dict[str, Any]:
    today = Decimal(s.scalar(select(func.coalesce(func.sum(LlmCall.cost_usd), 0))
                             .where(LlmCall.first_observed_at >= ny_day_start(now))) or 0)
    by = s.execute(select(LlmCall.purpose, LlmCall.model_id, func.count(), func.sum(LlmCall.cost_usd),
                          func.count().filter(LlmCall.valid.is_(False)))
                   .where(LlmCall.first_observed_at >= now - timedelta(days=7))
                   .group_by(LlmCall.purpose, LlmCall.model_id).order_by(LlmCall.purpose)).all()
    return {"today": today, "by": by}


def x_spend(s: Session, now: datetime) -> dict[str, Any]:
    today = Decimal(s.scalar(select(func.coalesce(func.sum(XPoll.cost_usd), 0))
                             .where(XPoll.first_observed_at >= ny_day_start(now))) or 0)
    day = func.date_trunc(literal_column("'day'"), XPoll.first_observed_at).label("day")
    week = s.execute(select(day, func.count(), func.sum(XPoll.posts_returned), func.sum(XPoll.cost_usd))
                     .where(XPoll.first_observed_at >= now - timedelta(days=7))
                     .group_by(day).order_by(day.desc())).all()
    return {"today": today, "days": week}


def quadrants(s: Session) -> Counter:
    return Counter(q for q in s.scalars(select(Postmortem.quadrant)) if q)


# ---------- pages ----------

def render_loop(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    from nta.dashboard import _pill
    from nta.evaluate import agreement

    state = load_kill_state(s, now)
    incidents = _open_incidents(s)
    flagged, total = spread_share(s)
    outcomes = Counter((k.value, o.value) for k, o in s.execute(select(Decision.kind, Decision.outcome)))
    llm, xs = llm_spend(s, now), x_spend(s, now)
    agree = agreement(s)
    q = quadrants(s)
    policy = load_policy(DEFAULT_POLICY_PATH)
    pill = {"RUN": "ok", "PAUSE": "warn", "FLATTEN": "warn", "HALT": "bad"}[state.value]
    cards = [
        f'<div class="panel"><h2>Kill switch</h2><p>{_pill(pill, state.value)}</p>'
        f'<p class="muted">{len(incidents)} open incident(s) · <a href="/incidents">details</a></p></div>',
        f'<div class="panel"><h2>spread_unverified</h2><div class="big">{_pct(flagged, total)}</div>'
        f'<p class="muted">{flagged} of {total} decisions with a quote · quote_source '
        f'{_esc(policy.policy.quote_source)}</p></div>',
        f'<div class="panel"><h2>LLM spend today</h2><div class="big">{_usd(llm["today"])}</div>'
        f'<p class="muted"><a href="/spend">by model</a></p></div>',
        f'<div class="panel"><h2>X spend today</h2><div class="big">{_usd(xs["today"])}</div></div>',
        f'<div class="panel"><h2>LLM vs human</h2><div class="big">κ {_kappa(agree["quality_kappa"])}</div>'
        f'<p class="muted">quality labels, {agree["n"]} decisions; criteria κ {_kappa(agree["criteria_kappa"])}</p></div>',
    ]
    out_rows = [[_esc(k), _esc(o), str(n)] for (k, o), n in sorted(outcomes.items())]
    quad_rows = [[_esc(name), str(n)] for name, n in sorted(q.items())]
    body = (f'<div class="grid">{"".join(cards)}</div>'
            f"<h2>Decisions by outcome</h2>{_table(['kind', 'outcome', 'count'], out_rows, 'No decisions yet.')}"
            f"<h2>Quadrants</h2><p class='muted'>quality (sound ≥ 9 with no zero, unsound = any zero or ≤ 6) × "
            f"result</p>{_table(['quadrant', 'count'], quad_rows, 'Nothing evaluated yet.')}"
            f"<p class='muted'>policy {_esc(policy.hash[:12])}</p>")
    return page("Decision loop", body)


def render_decisions(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    reviews = {r.decision_id: r for r in s.scalars(select(RiskReview))}
    rows = []
    for d in s.scalars(select(Decision).order_by(Decision.decided_at.desc(), Decision.id.desc()).limit(LIMIT)):
        plan = d.plan or {}
        rev = reviews.get(d.id)
        risk = "" if rev is None else f"{_esc(rev.verdict.value)} {_esc(', '.join(rev.reasons))}"
        cls = {"trade": "up", "abstain": "muted", "wait": ""}[d.outcome.value]
        rows.append([_esc(_ny(d.decided_at)), _esc(d.kind.value), _esc(d.symbol), f'<span class="{cls}">{_esc(d.outcome.value)}</span>',
                     _esc(", ".join(d.reasons)), _esc(d.qty), _esc(plan.get("entry") or plan.get("exit_ref")),
                     _esc(plan.get("stop")), _esc(plan.get("take_profit")), _esc(_ny(datetime.fromisoformat(plan["expiry"])) if plan.get("expiry") else ""),
                     "yes" if d.spread_unverified else "", risk, f"<code>{_esc(d.content_hash[:10])}</code>"])
    return page("Decisions", f"<h2>Decisions (latest {LIMIT})</h2>" + _table(
        ["decided (NY)", "kind", "symbol", "outcome", "reasons", "qty", "entry/exit", "stop", "take-profit",
         "expiry", "spread unverified", "risk", "hash"], rows, "No decisions yet."))


def render_orders(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    rows = []
    for o in s.scalars(select(Order).order_by(Order.created_at.desc(), Order.id.desc()).limit(LIMIT)):
        evs = list(s.scalars(select(OrderEvent).where(OrderEvent.order_id == o.id)
                             .order_by(OrderEvent.first_observed_at, OrderEvent.id)))
        last = evs[-1] if evs else None
        fills = list(s.scalars(select(Fill).where(Fill.order_id == o.id)))
        qty = sum((f.qty for f in fills), Decimal(0))
        avg = (sum((f.qty * f.price for f in fills), Decimal(0)) / qty) if qty else None
        rows.append([_esc(_ny(o.created_at)), _esc(o.symbol), _esc(o.side.value), _esc(o.qty), _esc(o.limit_price),
                     f"<code>{_esc(o.client_order_id)}</code>", _esc(last.event if last else "unsent"),
                     _esc(last.broker_status if last else ""), _esc(format(qty.normalize(), "f")),
                     _esc(f"{avg:.4f}" if avg else ""), str(len(fills))])
    fill_rows = [[_esc(_ny(f.first_observed_at)), _esc(f.symbol), _esc(f.side.value), _esc(format(f.qty.normalize(), "f")),
                  _esc(f"{f.price:.4f}"), _esc(format(f.cum_qty.normalize(), "f"))]
                 for f in s.scalars(select(Fill).order_by(Fill.first_observed_at.desc(), Fill.id.desc()).limit(LIMIT))]
    return page("Orders and fills",
                "<h2>Orders</h2>" + _table(["created (NY)", "symbol", "side", "qty", "limit", "client_order_id",
                                            "last event", "broker status", "filled", "avg price", "fills"],
                                           rows, "No orders yet.")
                + "<h2>Fills</h2>" + _table(["observed (NY)", "symbol", "side", "qty", "price", "cumulative"],
                                            fill_rows, "No fills yet."))


def render_positions(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    policy = load_policy(DEFAULT_POLICY_PATH).policy
    pos_rows = [[_esc(p.symbol), _esc(format(p.qty.normalize(), "f")), _esc(p.cost), _esc(p.sector), _esc(p.cluster_id)]
                for p in load_positions(s, now, policy) if p.qty > 0]
    snaps = []
    for acct in ("T", "D"):
        snap = s.scalars(select(PositionSnapshot).where(PositionSnapshot.account == acct)
                         .order_by(PositionSnapshot.taken_at.desc(), PositionSnapshot.id.desc()).limit(1)).first()
        if snap:
            mismatch = '<span class="pill bad">mismatch</span>' if snap.mismatch else '<span class="pill ok">match</span>'
            snaps.append([_esc(acct), _esc(_ny(snap.taken_at)), _esc(snap.equity), _esc(snap.positions),
                          _esc(snap.log_positions if acct == "T" else ""), _esc(snap.open_orders), mismatch])
    return page("Positions",
                "<h2>Open positions (from fills)</h2>" + _table(["symbol", "qty", "cost", "sector", "cluster"],
                                                                pos_rows, "No open positions.")
                + "<h2>Latest broker snapshots</h2>" + _table(
                    ["account", "taken (NY)", "equity", "broker positions", "log positions", "open orders", "reconcile"],
                    snaps, "No snapshots yet (nta risk has not run)."))


def render_incidents(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    open_ids = {i.id for i in _open_incidents(s)}
    inc_rows = [[_esc(_ny(i.at)), f"sev{i.severity}", _esc(i.kind), _esc(i.process),
                 '<span class="pill bad">open</span>' if i.id in open_ids else '<span class="muted">closed</span>',
                 f"<code>{_esc(i.detail)}</code>"]
                for i in s.scalars(select(Incident).order_by(Incident.at.desc()).limit(LIMIT))]
    kill_rows = [[_esc(_ny(k.at)), _esc(k.state.value), _esc(k.actor), _esc(k.reason)]
                 for k in s.scalars(select(KillSwitchEvent).order_by(KillSwitchEvent.at.desc()).limit(LIMIT))]
    return page("Incidents",
                f"<h2>Kill switch: {_esc(load_kill_state(s, now).value)}</h2>"
                + _table(["at (NY)", "state", "actor", "reason"], kill_rows, "No kill switch events (state RUN).")
                + "<h2>Incidents</h2><p class='muted'>Open until the next human <code>nta kill resume</code>.</p>"
                + _table(["at (NY)", "severity", "kind", "process", "status", "detail"], inc_rows, "No incidents."))


def render_estimates(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    rows = []
    for e, symbol in s.execute(select(Estimate, Decision.symbol).join(Decision, Decision.id == Estimate.decision_id)
                               .order_by(Estimate.evaluated_at.desc()).limit(LIMIT)):
        rows.append([_esc(symbol), _esc(e.reason), _esc(e.entry_price), _esc(e.exit_price), _esc(e.exit_reason),
                     _esc(e.net_bps), _esc(e.pnl_usd), "yes" if e.uncertain else ""])
    by_reason = s.execute(select(Estimate.reason, func.count(), func.avg(Estimate.net_bps))
                          .group_by(Estimate.reason).order_by(Estimate.reason)).all()
    summary = [[_esc(r), str(n), _esc(f"{avg:.1f}" if avg is not None else "–")] for r, n, avg in by_reason]
    return page("Estimate ledger",
                "<p class='muted'>Hypothetical trades for abstentions (late, already_priced, cap_hit, uncorroborated), "
                "simulated on recorded prices. Never mixed with real P&amp;L.</p>"
                + "<h2>By reason</h2>" + _table(["reason", "count", "mean net bps"], summary, "No estimates yet.")
                + "<h2>Latest</h2>" + _table(["symbol", "reason", "entry", "exit", "exit reason", "net bps",
                                              "P&L USD", "uncertain"], rows, "No estimates yet."))


def render_spend(s: Session, now: datetime, *, page) -> str:  # noqa: ANN001
    llm, xs = llm_spend(s, now), x_spend(s, now)
    llm_rows = [[_esc(p), _esc(m), str(n), _usd(c), str(inv or 0)] for p, m, n, c, inv in llm["by"]]
    x_rows = [[_esc(_ny(d)), str(n), str(posts or 0), _usd(c)] for d, n, posts, c in xs["days"]]
    return page("Spend",
                f"<h2>LLM (today {_usd(llm['today'])})</h2>"
                + _table(["purpose", "model", "calls (7 d)", "cost (7 d)", "invalid"], llm_rows, "No LLM calls yet.")
                + f"<h2>X (today {_usd(xs['today'])})</h2>"
                + _table(["day", "requests", "posts returned", "cost"], x_rows, "No X polls (X ingestion is off by default)."))


PAGES = {"/loop": render_loop, "/decisions": render_decisions, "/orders": render_orders,
         "/positions": render_positions, "/incidents": render_incidents, "/estimates": render_estimates,
         "/spend": render_spend}
