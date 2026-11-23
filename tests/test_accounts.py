"""Accounts: lookup by name, and by the name an export uses for them (N26 spaces)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from our_pocket.db.schema import ENTRY_TYPES
from our_pocket.services import AccountNotFound, AccountService

pytestmark = pytest.mark.db


def test_unknown_account(conn):
    with pytest.raises(AccountNotFound):
        AccountService(conn).get("nope")


def test_export_name_is_stored_stripped(conn):
    created = AccountService(conn).add("Shared", "n26", export_account_name=" Alex & Sam 🫶 ")
    assert created.export_account_name == "Alex & Sam 🫶"
    assert AccountService(conn).by_export_name("n26") == {"Alex & Sam 🫶": created}


def test_export_name_is_unique_per_household(conn):
    accounts = AccountService(conn)
    accounts.add("A", "n26", export_account_name="Main")
    with pytest.raises(IntegrityError), conn.begin_nested():
        accounts.add("B", "n26", export_account_name="Main")


def test_two_households_can_use_the_same_export_name(conn):
    accounts = AccountService(conn)
    ours = accounts.add("Ours", "n26", export_account_name="Main")
    theirs = accounts.add("Theirs", "n26", household_name="neighbours", export_account_name="Main")
    assert accounts.by_export_name("n26") == {"Main": ours}
    assert accounts.by_export_name("n26", "neighbours") == {"Main": theirs}


@pytest.mark.parametrize("export_account_name", [None, "", "   "])
def test_n26_account_needs_an_export_name(conn, export_account_name):
    with pytest.raises(ValueError, match="--export-name"):
        AccountService(conn).add("N26", "n26", export_account_name=export_account_name)


def test_trade_republic_account_refuses_an_export_name(conn):
    with pytest.raises(ValueError, match="--export-name"):
        AccountService(conn).add("TR", "trade_republic", export_account_name="Main")


@pytest.mark.parametrize(
    "entry_type",
    ["card_payment", "card_refund", "transfer_in", "transfer_out", "direct_debit", "fee", "tax"],
)
def test_bank_entry_types_are_allowed(entry_type):
    assert entry_type in ENTRY_TYPES


def test_entry_type_check_in_the_database_allows_bank_types(conn):
    definition = conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_ledger_entry_entry_type'"
        )
    ).scalar_one()
    assert "'card_payment'" in definition and "'tax'" in definition


def test_check_constraints_have_the_names_schema_py_gives_them(conn):
    names = conn.execute(
        text(
            "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE contype = 'c' AND nspname IN ('raw', 'staging', 'core')"
        )
    ).scalars()
    assert set(names) == {
        "ck_ledger_entry_entry_type",
        "ck_import_batch_status",
        "ck_import_flag_severity",
    }


def test_export_name_unique_constraint_has_its_own_name(conn):
    columns = conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'uq_account_export_account_name'"
        )
    ).scalar_one()
    assert columns == "UNIQUE (household_id, institution, export_account_name)"
