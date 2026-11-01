"""Test fixtures.

DB tests run against a throwaway database (TEST_DATABASE_URL, default nta_test on the
docker-compose Postgres). It is dropped and rebuilt with `alembic upgrade head` once per
session, so the migration itself is under test. Each test runs inside a transaction that
is rolled back; application commits become savepoint releases.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://nta:nta@127.0.0.1:55432/nta_test"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _recreate_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{u.database}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{u.database}"'))
    except Exception as exc:  # make the cause obvious
        raise RuntimeError(
            f"Cannot reach test Postgres at {u.render_as_string(hide_password=True)}. "
            "Run `docker compose up -d` first."
        ) from exc
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    _recreate_database(TEST_DATABASE_URL)
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        trans = conn.begin()
        yield conn
        trans.rollback()


@pytest.fixture
def session_factory(connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    s = session_factory()
    yield s
    s.close()
