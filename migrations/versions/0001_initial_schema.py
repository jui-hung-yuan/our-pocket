"""initial schema: accounts, import pipeline (L0-L2), ledger (L3)

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QTY = sa.Numeric(28, 10)
MONEY = sa.Numeric(20, 6)
RATE = sa.Numeric(20, 10)


def _entry_columns(*, staged: bool) -> list[sa.Column[Any]]:
    def col(
        name: str, type_: sa.types.TypeEngine[Any], *, required: bool = False
    ) -> sa.Column[Any]:
        return sa.Column(name, type_, nullable=staged or not required)

    return [
        col("external_id", sa.Text(), required=True),
        col("booked_at", sa.DateTime(timezone=True), required=True),
        col("booking_date", sa.Date(), required=True),
        col("entry_type", sa.Text(), required=True),
        col("source_type", sa.Text(), required=True),
        col("isin", sa.Text()),
        col("instrument_name", sa.Text()),
        col("asset_class", sa.Text()),
        col("quantity", QTY),
        col("price", QTY),
        col("amount", MONEY, required=True),
        col("fee", MONEY, required=True),
        col("tax", MONEY, required=True),
        col("cash_effect", MONEY),
        col("currency", sa.Text(), required=True),
        col("original_amount", MONEY),
        col("original_currency", sa.Text()),
        col("fx_rate", RATE),
        col("description", sa.Text()),
        col("counterparty_name", sa.Text()),
        col("counterparty_iban", sa.Text()),
    ]


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False)


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "household",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_household"),
        sa.UniqueConstraint("name", name="uq_household_name"),
    )
    op.create_table(
        "person",
        _id(),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name="fk_person_household_id_household"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_person"),
        sa.UniqueConstraint("household_id", "name", name="uq_person_household_id"),
    )
    op.create_table(
        "account",
        _id(),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_person_id", sa.BigInteger(), nullable=True),
        sa.Column("institution", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), server_default="EUR", nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name="fk_account_household_id_household"
        ),
        sa.ForeignKeyConstraint(
            ["owner_person_id"], ["person.id"], name="fk_account_owner_person_id_person"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account"),
        sa.UniqueConstraint("name", name="uq_account_name"),
    )
    op.create_table(
        "import_batch",
        _id(),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("encoding", sa.Text(), nullable=False),
        sa.Column("delimiter", sa.Text(), nullable=False),
        sa.Column("header_fingerprint", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("parser_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "warnings_accepted", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("decision_note", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('staged', 'committed', 'rejected')", name="ck_import_batch_status"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name="fk_import_batch_account_id_account"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batch"),
    )
    op.create_index(
        "uq_import_batch_active_file",
        "import_batch",
        ["account_id", "file_sha256"],
        unique=True,
        postgresql_where=sa.text("status != 'rejected'"),
    )
    op.create_table(
        "raw_row",
        _id(),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["import_batch.id"], name="fk_raw_row_batch_id_import_batch"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_row"),
        sa.UniqueConstraint("batch_id", "line_number", name="uq_raw_row_batch_id"),
    )
    op.create_table(
        "staged_row",
        _id(),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_row_id", sa.BigInteger(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        *_entry_columns(staged=True),
        sa.Column("duplicate_of", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["import_batch.id"], name="fk_staged_row_batch_id_import_batch"
        ),
        sa.ForeignKeyConstraint(
            ["raw_row_id"], ["raw_row.id"], name="fk_staged_row_raw_row_id_raw_row"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staged_row"),
        sa.UniqueConstraint("raw_row_id", name="uq_staged_row_raw_row_id"),
    )
    op.create_table(
        "import_flag",
        _id(),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("staged_row_id", sa.BigInteger(), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('error', 'warning', 'info')", name="ck_import_flag_severity"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["import_batch.id"], name="fk_import_flag_batch_id_import_batch"
        ),
        sa.ForeignKeyConstraint(
            ["staged_row_id"], ["staged_row.id"], name="fk_import_flag_staged_row_id_staged_row"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_flag"),
    )
    op.create_index("ix_import_flag_batch_id", "import_flag", ["batch_id"])
    op.create_table(
        "ledger_entry",
        _id(),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("staged_row_id", sa.BigInteger(), nullable=False),
        *_entry_columns(staged=False),
        _created_at(),
        sa.CheckConstraint(
            "entry_type IN ('buy', 'sell', 'dividend', 'interest', 'deposit', 'withdrawal', "
            "'tax_adjustment', 'tax_base', 'bonus')",
            name="ck_ledger_entry_entry_type",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name="fk_ledger_entry_account_id_account"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["import_batch.id"], name="fk_ledger_entry_batch_id_import_batch"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name="fk_ledger_entry_household_id_household"
        ),
        sa.ForeignKeyConstraint(
            ["staged_row_id"], ["staged_row.id"], name="fk_ledger_entry_staged_row_id_staged_row"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entry"),
        sa.UniqueConstraint("account_id", "external_id", name="uq_ledger_entry_account_id"),
        sa.UniqueConstraint("staged_row_id", name="uq_ledger_entry_staged_row_id"),
    )


def downgrade() -> None:
    for table in (
        "ledger_entry",
        "import_flag",
        "staged_row",
        "raw_row",
        "import_batch",
        "account",
        "person",
        "household",
    ):
        op.drop_table(table)
