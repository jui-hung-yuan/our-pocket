"""postgres schemas raw / staging / core; raw_row is append-only

Moves every table out of `public` (data, indexes, constraints and identity sequences move
with it) and adds a trigger that refuses UPDATE, DELETE and TRUNCATE on raw.raw_row.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_SCHEMAS = {
    "household": "core",
    "person": "core",
    "account": "core",
    "ledger_entry": "core",
    "import_batch": "staging",
    "staged_row": "staging",
    "import_flag": "staging",
    "raw_row": "raw",
}


def upgrade() -> None:
    for schema in ("raw", "staging", "core"):
        op.execute(f"CREATE SCHEMA {schema}")
    for table, schema in TABLE_SCHEMAS.items():
        op.execute(f"ALTER TABLE public.{table} SET SCHEMA {schema}")

    op.execute(
        """
        CREATE FUNCTION raw.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% on %.% is not allowed: the raw layer is append-only',
                TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege',
                      HINT = 'Fix a parser bug by re-parsing, not by editing raw rows.';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER raw_row_append_only BEFORE UPDATE OR DELETE ON raw.raw_row "
        "FOR EACH ROW EXECUTE FUNCTION raw.refuse_change()"
    )
    op.execute(
        "CREATE TRIGGER raw_row_no_truncate BEFORE TRUNCATE ON raw.raw_row "
        "FOR EACH STATEMENT EXECUTE FUNCTION raw.refuse_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER raw_row_no_truncate ON raw.raw_row")
    op.execute("DROP TRIGGER raw_row_append_only ON raw.raw_row")
    op.execute("DROP FUNCTION raw.refuse_change()")
    for table, schema in TABLE_SCHEMAS.items():
        op.execute(f"ALTER TABLE {schema}.{table} SET SCHEMA public")
    for schema in ("raw", "staging", "core"):
        op.execute(f"DROP SCHEMA {schema}")
