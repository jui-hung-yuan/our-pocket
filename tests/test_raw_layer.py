"""The raw layer (L1) is append-only: the database refuses UPDATE, DELETE and TRUNCATE."""

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import ProgrammingError

from our_pocket.db.schema import raw_row
from our_pocket.etl.pipeline import ImportPipeline

pytestmark = pytest.mark.db


@pytest.fixture
def staged(conn, tr_account, synthetic) -> int:
    ImportPipeline(conn).run(synthetic.to_csv_bytes(), "tr.csv", tr_account)
    return conn.execute(select(func.count()).select_from(raw_row)).scalar_one()


@pytest.mark.parametrize(
    "statement",
    [
        update(raw_row).values(data={}),
        delete(raw_row),
        text("TRUNCATE raw.raw_row CASCADE"),
    ],
    ids=["update", "delete", "truncate"],
)
def test_raw_rows_cannot_be_changed(conn, staged, statement):
    with pytest.raises(ProgrammingError, match="append-only"), conn.begin_nested():
        conn.execute(statement)
    assert conn.execute(select(func.count()).select_from(raw_row)).scalar_one() == staged


def test_tables_live_in_their_layer_schema(conn):
    rows = conn.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('public', 'raw', 'staging', 'core') "
            "AND table_name != 'alembic_version'"
        )
    ).all()
    assert dict((name, schema) for schema, name in rows) == {
        "household": "core",
        "person": "core",
        "account": "core",
        "ledger_entry": "core",
        "import_batch": "staging",
        "staged_row": "staging",
        "import_flag": "staging",
        "raw_row": "raw",
    }
