"""Database schema. Every table is append-only (enforced by a trigger in the migration).

Conventions:
- id: ULID (26 chars), created_at: DB clock at insert.
- Rows from outside carry the source's own timestamp (published_at / ts) and our clock
  at first sight (first_observed_at). first_observed_at is never overwritten: rows are
  never updated.
- "Latest row wins" is how state changes are expressed (allowlist membership, relabels).
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nta.ids import new_ulid

RAW_MAX_BYTES = 2 * 1024 * 1024


class QuoteKind(StrEnum):
    observe = "observe"
    mark_1h = "mark_1h"
    mark_1d = "mark_1d"
    mark_5d = "mark_5d"
    periodic = "periodic"
    decision = "decision"  # quote the agent decided an entry on (phase 1)
    manage = "manage"      # quote the position manager checked stops against (phase 1)


MARK_KINDS = (QuoteKind.mark_1h, QuoteKind.mark_1d, QuoteKind.mark_5d)


class Direction(StrEnum):
    up = "up"
    down = "down"
    unclear = "unclear"


class EventType(StrEnum):
    leadership = "leadership"
    guidance = "guidance"
    m_and_a = "m_and_a"
    legal = "legal"
    financing = "financing"
    product = "product"
    other = "other"


def _enum(e: type[StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK constraint; avoids ALTER TYPE pain if a value is added later.
    return Enum(e, name=name, native_enum=False, create_constraint=True, length=32,
                values_callable=lambda x: [m.value for m in x])


class Base(DeclarativeBase):
    pass


class _Row:
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Instrument(_Row, Base):
    """One row per allowlist membership interval. The latest row per figi is current;
    removal is a new row with removed_on set."""

    __tablename__ = "instruments"
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    figi: Mapped[str] = mapped_column(String(12), nullable=False)
    cik: Mapped[str] = mapped_column(String(10), nullable=False)  # zero-padded to 10
    name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    added_on: Mapped[date] = mapped_column(Date, nullable=False)
    removed_on: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (
        CheckConstraint("cik ~ '^[0-9]{10}$'", name="ck_instruments_cik_padded"),
        CheckConstraint("removed_on IS NULL OR removed_on >= added_on", name="ck_instruments_interval"),
        Index("ix_instruments_cik", "cik"),
        Index("ix_instruments_figi", "figi"),
    )


class OpenFigiResponse(_Row, Base):
    """Cache of OpenFIGI mapping responses (outside data, so it keeps provenance)."""

    __tablename__ = "openfigi_responses"
    request_key: Mapped[str] = mapped_column(Text, nullable=False)  # canonical JSON of the job
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_openfigi_request_key", "request_key"),)


class SourceEvent(_Row, Base):
    __tablename__ = "source_events"
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)  # accession number
    form_type: Mapped[str] = mapped_column(String(16), nullable=False)
    cik: Mapped[str] = mapped_column(String(10), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(ForeignKey("instruments.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)  # filing index page
    items: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")  # 8-K item numbers
    primary_doc_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[str | None] = mapped_column(Text)  # primary document as fetched, capped at 2 MB
    raw_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 hex of raw
    raw_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_events_source_external"),
        CheckConstraint(f"raw IS NULL OR octet_length(raw) <= {RAW_MAX_BYTES}", name="ck_source_events_raw_cap"),
        Index("ix_source_events_first_observed_at", "first_observed_at"),
        Index("ix_source_events_instrument_id", "instrument_id"),
    )


class QuoteSchedule(_Row, Base):
    """Persisted mark schedule. A schedule row is done when a quote with the same
    (source_event_id, kind) exists."""

    __tablename__ = "quote_schedule"
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id"), nullable=False)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    kind: Mapped[QuoteKind] = mapped_column(_enum(QuoteKind, "quote_schedule_kind"), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("source_event_id", "kind", name="uq_quote_schedule_event_kind"),
        Index("ix_quote_schedule_due_at", "due_at"),
    )


class Quote(_Row, Base):
    __tablename__ = "quotes"
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("source_events.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # source's own time
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    last: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[QuoteKind] = mapped_column(_enum(QuoteKind, "quote_kind"), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "(kind = 'periodic') = (source_event_id IS NULL)", name="ck_quotes_event_link"
        ),
        Index("ix_quotes_event_kind", "source_event_id", "kind"),
        Index("ix_quotes_instrument_ts", "instrument_id", "ts"),
    )


class Label(_Row, Base):
    """Human labels. Relabeling appends; the latest row per source_event_id wins."""

    __tablename__ = "labels"
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id"), nullable=False)
    labeler: Mapped[str] = mapped_column(String(32), nullable=False, server_default="human")
    material: Mapped[bool] = mapped_column(Boolean, nullable=False)
    direction: Mapped[Direction] = mapped_column(_enum(Direction, "label_direction"), nullable=False)
    event_type: Mapped[EventType] = mapped_column(_enum(EventType, "label_event_type"), nullable=False)
    labeled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    sample_seed: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (Index("ix_labels_source_event_id", "source_event_id"),)


class Run(_Row, Base):
    """One row per long-running process start. Stops are recorded in run_stops,
    because append-only rules out filling stopped_at later."""

    __tablename__ = "runs"
    process: Mapped[str] = mapped_column(String(32), nullable=False)  # poll_edgar | quotes
    code_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunStop(_Row, Base):
    __tablename__ = "run_stops"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, unique=True)
    stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


# ---------- phase 1: decision loop ----------


# Python None -> SQL NULL (plain JSONB stores JSON 'null', which defeats IS NULL checks).
JSONB_NULL = JSONB(none_as_null=True)


class SignalStatus(StrEnum):
    extracted = "extracted"
    triage_rejected = "triage_rejected"
    llm_invalid = "llm_invalid"
    llm_budget = "llm_budget"
    llm_error = "llm_error"


class DecisionKind(StrEnum):
    entry = "entry"
    close = "close"


class Outcome(StrEnum):
    trade = "trade"
    wait = "wait"
    abstain = "abstain"


class Side(StrEnum):
    buy = "buy"
    sell = "sell"


class KillState(StrEnum):
    RUN = "RUN"
    PAUSE = "PAUSE"
    FLATTEN = "FLATTEN"
    HALT = "HALT"


class Grader(StrEnum):
    llm = "llm"
    human = "human"


class RiskVerdict(StrEnum):
    approve = "approve"
    reject = "reject"


class LlmCall(_Row, Base):
    """Every model call, keyed by the hash of its full request. The cache: a request
    whose hash is here is never sent again (replay never sends at all)."""

    __tablename__ = "llm_calls"
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)  # triage | extract | grade
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    response: Mapped[dict | None] = mapped_column(JSONB_NULL)  # raw API response; NULL on transport error
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)  # output passed schema validation
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_llm_calls_first_observed_at", "first_observed_at"),)


class Signal(_Row, Base):
    """One per source_event the agent processed: extraction result plus cluster membership."""

    __tablename__ = "signals"
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id"), nullable=False, unique=True)
    instrument_id: Mapped[str | None] = mapped_column(ForeignKey("instruments.id"))
    origin: Mapped[str] = mapped_column(String(64), nullable=False)  # edgar | x:<author_id>
    source_class: Mapped[str] = mapped_column(String(16), nullable=False)  # primary | aggregator | commentator
    source_weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)  # normalized; untrusted free text
    minhash: Mapped[list] = mapped_column(JSONB, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cluster_id: Mapped[str] = mapped_column(String(26), nullable=False)  # source_event id of the cluster's first member
    is_repeat: Mapped[bool] = mapped_column(Boolean, nullable=False)
    corroboration: Mapped[int] = mapped_column(Integer, nullable=False)  # distinct origins in cluster
    corroborated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[SignalStatus] = mapped_column(_enum(SignalStatus, "signal_status"), nullable=False)
    triage_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.id"))
    extract_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.id"))
    extraction: Mapped[dict | None] = mapped_column(JSONB_NULL)  # validated model output; contains free text
    flags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")  # e.g. injection_suspected
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_signals_cluster", "cluster_id"),
                      Index("ix_signals_instrument_ready", "instrument_id", "ready_at"))


class Decision(_Row, Base):
    """One per signal (kind=entry) or per close trigger (kind=close). Holds no free text,
    so nta risk may read every column. content_hash covers everything replay must reproduce."""

    __tablename__ = "decisions"
    kind: Mapped[DecisionKind] = mapped_column(_enum(DecisionKind, "decision_kind"), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"))
    parent_decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id"), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(ForeignKey("instruments.id"))
    symbol: Mapped[str | None] = mapped_column(String(16))
    cluster_id: Mapped[str | None] = mapped_column(String(26))
    outcome: Mapped[Outcome] = mapped_column(_enum(Outcome, "decision_outcome"), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    side: Mapped[Side | None] = mapped_column(_enum(Side, "decision_side"))
    qty: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[dict | None] = mapped_column(JSONB_NULL)
    exposure: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    plan: Mapped[dict | None] = mapped_column(JSONB_NULL)  # entry, stop, take_profit, expiry
    expected_cost_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    spread_unverified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    hypothetical: Mapped[dict | None] = mapped_column(JSONB_NULL)  # e.g. the short we may not take
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    code_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    data_snapshot: Mapped[str | None] = mapped_column(String(26))  # max source_events.id visible
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        CheckConstraint("(kind = 'entry') = (signal_id IS NOT NULL)", name="ck_decisions_entry_signal"),
        CheckConstraint("(kind = 'close') = (parent_decision_id IS NOT NULL)", name="ck_decisions_close_parent"),
        CheckConstraint("outcome <> 'trade' OR (side IS NOT NULL AND qty > 0 AND plan IS NOT NULL)",
                        name="ck_decisions_trade_complete"),
        Index("uq_decisions_entry_signal", "signal_id", unique=True,
              postgresql_where="kind = 'entry'"),
        Index("ix_decisions_outcome", "outcome", "decided_at"),
        Index("ix_decisions_parent", "parent_decision_id"),
    )


class RiskReview(_Row, Base):
    """nta risk's independent re-check of a trade decision. Exactly one per decision."""

    __tablename__ = "risk_reviews"
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False, unique=True)
    verdict: Mapped[RiskVerdict] = mapped_column(_enum(RiskVerdict, "risk_verdict"), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    quote: Mapped[dict | None] = mapped_column(JSONB_NULL)
    checks: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Order(_Row, Base):
    """An order nta risk approved and signed. hmac covers (decision_id, side, qty, limit)."""

    __tablename__ = "orders"
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    leg: Mapped[int] = mapped_column(Integer, nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[Side] = mapped_column(_enum(Side, "order_side"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False, server_default="day")
    hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("decision_id", "leg", name="uq_orders_decision_leg"),
                      CheckConstraint("qty > 0", name="ck_orders_qty"))


class OrderEvent(_Row, Base):
    __tablename__ = "order_events"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    broker_status: Mapped[str | None] = mapped_column(String(32))
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    filled_avg_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_order_events_order", "order_id", "first_observed_at"),)


class Fill(_Row, Base):
    """One per increase of an order's cumulative filled quantity."""

    __tablename__ = "fills"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[Side] = mapped_column(_enum(Side, "fill_side"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    cum_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    broker_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("order_id", "cum_qty", name="uq_fills_order_cum"),
                      Index("ix_fills_decision", "decision_id"))


class PositionSnapshot(_Row, Base):
    __tablename__ = "position_snapshots"
    account: Mapped[str] = mapped_column(String(1), nullable=False)  # T (trading) | D (data, watchdog)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    last_equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))  # previous close (Alpaca)
    cash: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    positions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")  # broker: symbol -> qty
    open_orders: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    log_positions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")  # from fills
    mismatch: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    __table_args__ = (CheckConstraint("account IN ('T', 'D')", name="ck_position_snapshots_account"),
                      Index("ix_position_snapshots_taken", "account", "taken_at"))


class KillSwitchEvent(_Row, Base):
    """The latest row is the state in force. Only a human writes RUN (resume)."""

    __tablename__ = "kill_switch_events"
    state: Mapped[KillState] = mapped_column(_enum(KillState, "kill_state"), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)  # human | reconciler | watchdog | risk
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Incident(_Row, Base):
    """Open until the next human resume (a RUN kill_switch_events row after it)."""

    __tablename__ = "incidents"
    severity: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = most severe
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    process: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("severity BETWEEN 1 AND 3", name="ck_incidents_severity"),)


class Grade(_Row, Base):
    """Rubric grade of a decision's frozen packet (outcome hidden). Latest per grader wins."""

    __tablename__ = "grades"
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    grader: Mapped[Grader] = mapped_column(_enum(Grader, "grade_grader"), nullable=False)
    scores: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 6 criteria, 0-2 each
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    has_zero: Mapped[bool] = mapped_column(Boolean, nullable=False)
    packet_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    llm_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.id"))
    graded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_grades_decision", "decision_id", "grader"),)


class Postmortem(_Row, Base):
    __tablename__ = "postmortems"
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    packet_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    packet: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    economic: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    quality: Mapped[str | None] = mapped_column(String(16))   # sound | unsound | insufficient
    result: Mapped[str | None] = mapped_column(String(8))     # win | loss | flat
    quadrant: Mapped[str | None] = mapped_column(String(32))  # quality x result
    __table_args__ = (Index("ix_postmortems_decision", "decision_id"),)


class Estimate(_Row, Base):
    """Estimate ledger: hypothetical trades for abstentions (never mixed with real P&L)."""

    __tablename__ = "estimates"
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    exit_reason: Mapped[str | None] = mapped_column(String(16))
    net_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_estimates_decision", "decision_id"),)


class GuardrailProposal(_Row, Base):
    """Suggested policy changes. Never applied automatically: a human edits policy.yaml."""

    __tablename__ = "guardrail_proposals"
    rule: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    current: Mapped[dict] = mapped_column(JSONB, nullable=False)
    proposed: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PolicyVersion(_Row, Base):
    """Every policy file seen at startup: full text, sha256, and nta risk's HMAC of the hash."""

    __tablename__ = "policy_versions"
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str | None] = mapped_column(String(64))  # NULL when seen by a non-signing process
    process: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    __table_args__ = (Index("ix_policy_versions_hash", "policy_hash"),)


class XPoll(_Row, Base):
    """One row per X timeline request (or skipped request), for spend and since_id."""

    __tablename__ = "x_polls"
    author_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # ok | error | budget_skipped
    posts_returned: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    posts_billable: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    newest_id: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_x_polls_author_time", "author_id", "first_observed_at"),)


APPEND_ONLY_TABLES = [t.name for t in Base.metadata.sorted_tables]
