import re
import time

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from nta.ids import new_ulid
from nta.schema import APPEND_ONLY_TABLES
from tests.factories import one_of_each

APPEND_ONLY_ERROR = re.compile(r"append-only table \w+: (UPDATE|DELETE|TRUNCATE) is not allowed")


def test_every_table_is_covered(connection: Connection) -> None:
    rows = connection.execute(text(
        "SELECT event_object_table, event_manipulation FROM information_schema.triggers "
        "WHERE trigger_name LIKE '%_append_only'"
    )).all()
    covered = {(t, op) for t, op in rows}
    for table in APPEND_ONLY_TABLES:
        assert (table, "UPDATE") in covered, table
        assert (table, "DELETE") in covered, table


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
@pytest.mark.parametrize("statement", [
    "UPDATE {t} SET created_at = created_at WHERE id = :id",
    "DELETE FROM {t} WHERE id = :id",
    "TRUNCATE {t} CASCADE",
])
def test_append_only_trigger_fires(session: Session, table: str, statement: str) -> None:
    row_id = one_of_each(session)[table].id
    session.execute(text("SAVEPOINT before_mutation"))
    with pytest.raises(DBAPIError, match=APPEND_ONLY_ERROR):
        session.execute(text(statement.format(t=table)), {"id": row_id})
    session.execute(text("ROLLBACK TO SAVEPOINT before_mutation"))
    # The row is still there and unchanged.
    assert session.execute(text(f"SELECT count(*) FROM {table} WHERE id = :id"),
                           {"id": row_id}).scalar_one() == 1


def test_orm_update_is_rejected(session: Session) -> None:
    ev = one_of_each(session)["source_events"]
    ev.first_observed_at = ev.first_observed_at.replace(year=2030)
    with pytest.raises(DBAPIError, match=APPEND_ONLY_ERROR):
        session.flush()


def test_duplicate_accession_is_rejected(session: Session) -> None:
    from nta.schema import SourceEvent
    ev = one_of_each(session)["source_events"]
    dup = SourceEvent(**{c: getattr(ev, c) for c in (
        "source_id", "external_id", "form_type", "cik", "published_at",
        "first_observed_at", "ingested_at", "url")})
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.flush()


def test_ulid_shape_and_ordering() -> None:
    a, b = new_ulid(now_ms=1_000), new_ulid(now_ms=2_000)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", a)
    assert a < b
    assert len({new_ulid() for _ in range(1000)}) == 1000


def test_ulid_is_monotonic_within_a_millisecond() -> None:
    now_ms = int(time.time() * 1000)
    ids = [new_ulid(now_ms=now_ms) for _ in range(10_000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    fast = [new_ulid() for _ in range(10_000)]
    assert fast == sorted(fast)


# ---------- phase 1 constraints ----------

def _flush_fails(session: Session, row: object) -> None:
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(row)
            session.flush()


def test_new_quote_kinds_are_accepted(session: Session) -> None:
    from nta.schema import Quote, QuoteKind
    rows = one_of_each(session)
    for kind in (QuoteKind.decision, QuoteKind.manage):
        session.add(Quote(instrument_id=rows["instruments"].id, source_event_id=rows["source_events"].id,
                          ts=rows["quotes"].ts, first_observed_at=rows["quotes"].ts, last=1,
                          source="alpaca_iex", kind=kind))
    session.flush()


def test_one_entry_decision_per_signal(session: Session) -> None:
    from nta.schema import Decision
    dec = one_of_each(session)["decisions"]
    copy = Decision(**{c.name: getattr(dec, c.name) for c in Decision.__table__.columns
                       if c.name not in ("id", "created_at")})
    _flush_fails(session, copy)


def test_trade_decision_needs_side_qty_and_plan(session: Session) -> None:
    from nta.schema import Decision
    dec = one_of_each(session)["decisions"]
    fields = {c.name: getattr(dec, c.name) for c in Decision.__table__.columns
              if c.name not in ("id", "created_at", "signal_id")}
    fields.update(kind="close", parent_decision_id=dec.id, plan=None)
    _flush_fails(session, Decision(**fields))


def test_client_order_id_is_unique(session: Session) -> None:
    from nta.schema import Order
    o = one_of_each(session)["orders"]
    _flush_fails(session, Order(decision_id=o.decision_id, leg=2, client_order_id=o.client_order_id,
                                symbol="AAPL", side="buy", qty=1, limit_price=1, hmac="x" * 64,
                                policy_version="p" * 64))
