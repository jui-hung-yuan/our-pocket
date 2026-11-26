import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from our_pocket.db.schema import SCHEMAS
from our_pocket.etl.models import Account
from our_pocket.services import AccountService
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic import trade_republic as synth_tr

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://pocket:pocket@localhost:5432/pocket_test"
)


@pytest.fixture(scope="session")
def synthetic() -> synth_tr.SyntheticExport:
    return synth_tr.generate(seed=42)


@pytest.fixture(scope="session")
def synthetic_n26() -> synth_n26.SyntheticN26:
    return synth_n26.generate(seed=42)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def engine():
    """A freshly migrated test database. Skips DB tests if Postgres is unreachable,
    unless REQUIRE_DB=1 (set in CI), where an unreachable database is a failure."""
    eng = create_engine(TEST_DATABASE_URL)
    try:
        with eng.connect():
            pass
    except OperationalError as exc:
        if os.environ.get("REQUIRE_DB") == "1":
            raise
        pytest.skip(f"Postgres not reachable at {TEST_DATABASE_URL}: {exc.orig}")
    with eng.begin() as conn:
        for schema in SCHEMAS:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    """A connection whose work is rolled back after each test."""
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()


@pytest.fixture
def tr_account(conn) -> Account:
    return AccountService(conn).add("TR Depot", "trade_republic", owner="Alex")


class SharedConnection:
    """Stands in for the engine of code that opens its own connections (the CLI, the app):
    every one is the test's connection, rolled back afterwards. A transaction is a
    savepoint, so a failing command undoes its own writes, as against the real database."""

    def __init__(self, conn) -> None:
        self.conn = conn

    @contextmanager
    def begin(self):
        with self.conn.begin_nested():
            yield self.conn

    @contextmanager
    def connect(self):
        yield self.conn


@pytest.fixture
def shared_connection(conn) -> SharedConnection:
    return SharedConnection(conn)
