"""space transfer entry types; export account names no longer unique

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES_0003 = (
    "buy",
    "sell",
    "dividend",
    "interest",
    "deposit",
    "withdrawal",
    "tax_adjustment",
    "tax_base",
    "bonus",
    "card_payment",
    "card_refund",
    "transfer_in",
    "transfer_out",
    "direct_debit",
    "fee",
    "tax",
)
SPACE_TRANSFER_TYPES = ("space_transfer_in", "space_transfer_out")
ENTRY_TYPE_CHECK = "ck_ledger_entry_entry_type"
EXPORT_NAME_UNIQUE = "uq_account_export_account_name"


def _replace_entry_type_check(types: tuple[str, ...]) -> None:
    # `alembic check` doesn't compare CHECK contents, so this is written by hand.
    op.drop_constraint(op.f(ENTRY_TYPE_CHECK), "ledger_entry", schema="core", type_="check")
    op.create_check_constraint(
        op.f(ENTRY_TYPE_CHECK),
        "ledger_entry",
        f"entry_type IN ({', '.join(repr(t) for t in types)})",
        schema="core",
    )


def upgrade() -> None:
    _replace_entry_type_check(ENTRY_TYPES_0003 + SPACE_TRANSFER_TYPES)
    # Imports name their account now, so two members' spaces may share a name.
    op.drop_constraint(op.f(EXPORT_NAME_UNIQUE), "account", schema="core", type_="unique")


def downgrade() -> None:
    # Fails, and changes nothing, once the ledger holds space transfers or two members'
    # spaces share a name.
    op.create_unique_constraint(
        op.f(EXPORT_NAME_UNIQUE),
        "account",
        ["household_id", "institution", "export_account_name"],
        schema="core",
    )
    _replace_entry_type_check(ENTRY_TYPES_0003)
