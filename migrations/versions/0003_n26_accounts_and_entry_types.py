"""account.export_account_name; bank entry types (N26)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BROKER_TYPES = (
    "buy",
    "sell",
    "dividend",
    "interest",
    "deposit",
    "withdrawal",
    "tax_adjustment",
    "tax_base",
    "bonus",
)
BANK_TYPES = (
    "card_payment",
    "card_refund",
    "transfer_in",
    "transfer_out",
    "direct_debit",
    "fee",
    "tax",
)


# 0001 created its CHECK constraints with the naming convention applied twice
# (ck_<table>_ck_<table>_<name>). These are the names db/schema.py expects.
RENAMED_CHECKS = (
    ("staging", "import_batch", "ck_import_batch_ck_import_batch_status", "ck_import_batch_status"),
    ("staging", "import_flag", "ck_import_flag_ck_import_flag_severity", "ck_import_flag_severity"),
)
ENTRY_TYPE_CHECK_0001 = "ck_ledger_entry_ck_ledger_entry_entry_type"
ENTRY_TYPE_CHECK = "ck_ledger_entry_entry_type"
EXPORT_NAME_UNIQUE = "uq_account_export_account_name"


def _rename_constraint(schema: str, table: str, old: str, new: str) -> None:
    op.execute(f"ALTER TABLE {schema}.{table} RENAME CONSTRAINT {old} TO {new}")


def _replace_entry_type_check(old_name: str, new_name: str, types: tuple[str, ...]) -> None:
    # `alembic check` doesn't compare CHECK contents, so this is written by hand. op.f marks
    # a name as final, so the naming convention isn't applied to it again.
    op.drop_constraint(op.f(old_name), "ledger_entry", schema="core", type_="check")
    op.create_check_constraint(
        op.f(new_name),
        "ledger_entry",
        f"entry_type IN ({', '.join(repr(t) for t in types)})",
        schema="core",
    )


def upgrade() -> None:
    op.add_column("account", sa.Column("export_account_name", sa.Text()), schema="core")
    op.create_unique_constraint(
        op.f(EXPORT_NAME_UNIQUE),
        "account",
        ["household_id", "institution", "export_account_name"],
        schema="core",
    )
    _replace_entry_type_check(ENTRY_TYPE_CHECK_0001, ENTRY_TYPE_CHECK, BROKER_TYPES + BANK_TYPES)
    for schema, table, old, new in RENAMED_CHECKS:
        _rename_constraint(schema, table, old, new)


def downgrade() -> None:
    # Fails, and changes nothing, once the ledger holds bank entry types.
    for schema, table, old, new in RENAMED_CHECKS:
        _rename_constraint(schema, table, new, old)
    _replace_entry_type_check(ENTRY_TYPE_CHECK, ENTRY_TYPE_CHECK_0001, BROKER_TYPES)
    op.drop_constraint(op.f(EXPORT_NAME_UNIQUE), "account", schema="core", type_="unique")
    op.drop_column("account", "export_account_name", schema="core")
