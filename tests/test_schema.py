import re

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
