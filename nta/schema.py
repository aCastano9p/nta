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


APPEND_ONLY_TABLES = [t.name for t in Base.metadata.sorted_tables]
