"""End-to-end: synthetic export → extract → transform → stage (flags) → commit → ledger."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from our_pocket.db.schema import (
    account,
    import_batch,
    import_flag,
    ledger_entry,
    raw_row,
    staged_row,
)
from our_pocket.etl.load import (
    AccountNotAllowed,
    AccountRequired,
    BatchStore,
    CommitBlocked,
    DuplicateFile,
    InvalidBatchState,
    UnknownExportAccount,
    UnknownFormat,
)
from our_pocket.etl.models import Account
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.etl.transform.trade_republic import COLUMNS_V1
from our_pocket.services import AccountService, LedgerService
from our_pocket.synthetic.n26 import MAIN, SAVINGS, SHARED, SPACES, SyntheticN26
from our_pocket.synthetic.trade_republic import SyntheticExport

pytestmark = pytest.mark.db


def _stage(conn, export: SyntheticExport, account: Account, name: str = "tr.csv") -> int:
    [batch_id] = ImportPipeline(conn).run(export.to_csv_bytes(), name, account)
    return batch_id


def _count(conn, table, **where) -> int:
    return conn.execute(
        select(func.count()).select_from(table).where(*(table.c[k] == v for k, v in where.items()))
    ).scalar_one()


def test_happy_path_reaches_the_ledger_with_ground_truth_holdings(conn, tr_account, synthetic):
    batch_id = _stage(conn, synthetic, tr_account)

    report = BatchStore(conn).report(batch_id)
    assert report.status == "staged"
    assert (report.schema_id, report.schema_version, report.parser_version) == (
        "trade_republic.transactions_csv", 1, "2",
    )  # fmt: skip
    assert report.row_count == len(synthetic.rows)
    assert report.errors == 0
    assert report.warnings > 0  # STOCKPERK: cash_semantics_unverified
    assert _count(conn, ledger_entry) == 0, "staging must never touch the ledger"

    with pytest.raises(CommitBlocked, match="warning"):
        BatchStore(conn).commit(batch_id)

    result = BatchStore(conn).commit(batch_id, accept_warnings=True)
    assert (result.inserted, result.skipped_duplicates) == (len(synthetic.rows), 0)
    assert BatchStore(conn).report(batch_id).status == "committed"

    ledger = LedgerService(conn)
    assert ledger.positions(tr_account.name) == synthetic.holdings
    totals = ledger.totals_by_type(tr_account.name)
    assert sum(n for n, _ in totals.values()) == len(synthetic.rows)
    assert totals["bonus"][1] is None  # STOCKPERK: unverified cash semantics stay NULL

    tax_column = [Decimal(r["tax"]) for r in synthetic.rows if r["tax"]]
    taxes = ledger.taxes(tr_account.name)
    assert sum(tax for _, tax in taxes.values()) == sum(tax_column)
    assert taxes["tax_adjustment"][1] > 0  # refunds
    assert totals["tax_adjustment"][1] == taxes["tax_adjustment"][1]
    assert totals["tax_base"][1] == taxes["tax_base"][1]  # Vorabpauschale tax is cash


def test_every_ledger_entry_traces_back_to_its_raw_row(conn, tr_account, synthetic):
    batch_id = _stage(conn, synthetic, tr_account)
    BatchStore(conn).commit(batch_id, accept_warnings=True)
    rows = conn.execute(
        select(ledger_entry.c.external_id, ledger_entry.c.amount, raw_row.c.data)
        .join(staged_row, staged_row.c.id == ledger_entry.c.staged_row_id)
        .join(raw_row, raw_row.c.id == staged_row.c.raw_row_id)
    ).all()
    assert len(rows) == len(synthetic.rows)
    for external_id, amount, data in rows:
        assert data["transaction_id"] == external_id
        assert Decimal(data["amount"]) == amount
        assert set(data) == set(COLUMNS_V1)


def test_same_file_twice_is_refused(conn, tr_account, synthetic):
    _stage(conn, synthetic, tr_account)
    with pytest.raises(DuplicateFile):
        _stage(conn, synthetic, tr_account)


def test_overlapping_export_only_adds_new_rows(conn, tr_account, synthetic):
    first = SyntheticExport(rows=synthetic.rows[:40], holdings={})
    BatchStore(conn).commit(_stage(conn, first, tr_account, "jan.csv"), accept_warnings=True)

    batch_id = _stage(conn, synthetic, tr_account, "full.csv")
    report = BatchStore(conn).report(batch_id)
    assert report.duplicate_count == 40
    assert sum(f["code"] == "already_in_ledger" for f in report.flags) == 40

    result = BatchStore(conn).commit(batch_id, accept_warnings=True)
    assert (result.inserted, result.skipped_duplicates) == (len(synthetic.rows) - 40, 40)
    assert _count(conn, ledger_entry) == len(synthetic.rows)
    assert LedgerService(conn).positions() == synthetic.holdings


def test_errors_block_commit_until_rejected_and_reimported(conn, tr_account, synthetic):
    rows = [dict(r) for r in synthetic.rows]
    rows[5]["amount"] = "not-a-number"
    broken = SyntheticExport(rows=rows, holdings={})
    batch_id = _stage(conn, broken, tr_account)

    report = BatchStore(conn).report(batch_id)
    assert report.errors == 1
    [error] = [f for f in report.flags if f["severity"] == "error"]
    assert (error["code"], error["line_number"], error["field"]) == (
        "unparseable_value",
        7,
        "amount",
    )

    with pytest.raises(CommitBlocked, match="error"):
        BatchStore(conn).commit(batch_id, accept_warnings=True)

    BatchStore(conn).reject(batch_id, "amount column corrupted")
    assert BatchStore(conn).report(batch_id).status == "rejected"
    with pytest.raises(InvalidBatchState):
        BatchStore(conn).commit(batch_id, accept_warnings=True)

    # A rejected file may be staged again (e.g. after fixing the parser).
    assert _stage(conn, broken, tr_account) != batch_id
    assert _count(conn, ledger_entry) == 0


def test_unknown_schema_is_rejected_at_intake(conn, tr_account):
    content = (
        b'"Booking Date","Value Date","Partner Name","Amount (EUR)"\n"2025-01-01","","x","1"\n'
    )
    [batch_id] = ImportPipeline(conn).run(content, "n26.csv", tr_account)
    report = BatchStore(conn).report(batch_id)
    assert report.status == "rejected"
    assert report.schema_id is None
    [flag] = report.flags
    assert flag["code"] == "unknown_schema"
    assert "closest known schema" in flag["message"]
    assert _count(conn, raw_row, batch_id=batch_id) == 0


def test_file_for_another_institution_is_rejected_at_intake(conn, synthetic):
    accounts = AccountService(conn)
    accounts.add("Giro", "trade_republic")
    conn.execute(account.update().where(account.c.name == "Giro").values(institution="n26"))
    batch_id = _stage(conn, synthetic, accounts.get("Giro"))
    report = BatchStore(conn).report(batch_id)
    assert report.status == "rejected"
    assert [f["code"] for f in report.flags] == ["institution_mismatch"]


def test_commit_is_once_only(conn, tr_account, synthetic):
    batch_id = _stage(conn, synthetic, tr_account)
    BatchStore(conn).commit(batch_id, accept_warnings=True)
    with pytest.raises(InvalidBatchState):
        BatchStore(conn).commit(batch_id, accept_warnings=True)
    with pytest.raises(InvalidBatchState):
        BatchStore(conn).reject(batch_id, "too late")


def test_flags_are_linked_to_staged_rows(conn, tr_account, synthetic):
    batch_id = _stage(conn, synthetic, tr_account)
    orphans = conn.execute(
        select(func.count())
        .select_from(import_flag)
        .where(import_flag.c.batch_id == batch_id, import_flag.c.staged_row_id.is_(None))
    ).scalar_one()
    assert orphans == 0


# --- N26: one account per space, rows routed by `Account Name` -----------------------


@pytest.fixture
def n26_accounts(conn) -> dict[str, Account]:
    accounts = AccountService(conn)
    return {
        MAIN: accounts.add("N26 Main", "n26", owner="Alex", export_account_name=MAIN),
        SHARED: accounts.add("N26 Shared", "n26", export_account_name=SHARED),
        SAVINGS: accounts.add("N26 Savings", "n26", owner="Alex", export_account_name=SAVINGS),
    }


def _cash_by_account(conn) -> dict[str, Decimal]:
    rows = conn.execute(
        select(account.c.name, func.sum(ledger_entry.c.cash_effect))
        .join(account, account.c.id == ledger_entry.c.account_id)
        .group_by(account.c.name)
    ).all()
    return dict(rows)


def _stage_n26(conn, content: bytes, name: str = "n26.csv") -> list[int]:
    return ImportPipeline(conn).run(content, name)


def test_n26_end_to_end_balances_per_space(conn, n26_accounts, synthetic_n26: SyntheticN26):
    for space in SPACES:
        [batch_id] = _stage_n26(conn, synthetic_n26.to_csv_bytes(space), f"{space}.csv")
        BatchStore(conn).commit(batch_id)  # every new entry type passes the CHECK constraint
    assert _cash_by_account(conn) == {
        n26_accounts[space].name: balance for space, balance in synthetic_n26.balances().items()
    }


def test_mixed_file_becomes_one_batch_per_space(conn, n26_accounts, synthetic_n26):
    content = synthetic_n26.mixed_csv_bytes()
    reports = [BatchStore(conn).report(b) for b in _stage_n26(conn, content, "mixed.csv")]
    assert {r.account for r in reports} == {"N26 Main", "N26 Shared"}
    assert {r.status for r in reports} == {"staged"}
    assert sum(r.row_count for r in reports) == content.count(b"\n") - 1
    shared = next(r for r in reports if r.account == "N26 Shared")
    assert shared.row_count == 2


def test_unmapped_space_stages_nothing(conn, synthetic_n26):
    AccountService(conn).add("N26 Main", "n26", export_account_name=MAIN)
    with pytest.raises(UnknownExportAccount) as excinfo:
        _stage_n26(conn, synthetic_n26.mixed_csv_bytes())
    assert excinfo.value.names == [SHARED]
    assert "--export-name" in str(excinfo.value)
    assert _count(conn, import_batch) == 0


def test_n26_rows_go_to_the_given_households_accounts(conn, n26_accounts, synthetic_n26):
    theirs = AccountService(conn).add(
        "Their Main", "n26", household_name="neighbours", export_account_name=MAIN
    )
    [batch_id] = ImportPipeline(conn).run(
        synthetic_n26.to_csv_bytes(MAIN), "n26.csv", household_name="neighbours"
    )
    assert BatchStore(conn).report(batch_id).account == theirs.name


def test_unmapped_space_in_another_household_suggests_that_household(conn, synthetic_n26):
    with pytest.raises(UnknownExportAccount) as excinfo:
        ImportPipeline(conn).run(
            synthetic_n26.to_csv_bytes(MAIN), "n26.csv", household_name="neighbours"
        )
    assert "--household 'neighbours'" in str(excinfo.value)


def test_account_argument_is_refused_for_n26(conn, n26_accounts, synthetic_n26):
    with pytest.raises(AccountNotAllowed):
        ImportPipeline(conn).run(synthetic_n26.to_csv_bytes(MAIN), "n26.csv", n26_accounts[MAIN])
    assert _count(conn, import_batch) == 0


def test_trade_republic_still_needs_an_account(conn, synthetic):
    with pytest.raises(AccountRequired):
        ImportPipeline(conn).run(synthetic.to_csv_bytes(), "tr.csv")
    assert _count(conn, import_batch) == 0


def test_unknown_format_without_account_raises(conn):
    with pytest.raises(UnknownFormat, match="closest known schema"):
        ImportPipeline(conn).run(b"Datum;Betrag\n2025-01-01;1\n", "bank.csv")
    assert _count(conn, import_batch) == 0


def test_overlapping_n26_export_adds_nothing(conn, n26_accounts, synthetic_n26):
    [first] = _stage_n26(conn, synthetic_n26.to_csv_bytes(MAIN, months=9), "jan-sep.csv")
    BatchStore(conn).commit(first)
    [full] = _stage_n26(conn, synthetic_n26.to_csv_bytes(MAIN), "jan-feb-next-year.csv")
    overlap = len(synthetic_n26.space_rows(MAIN, months=9))
    assert BatchStore(conn).report(full).duplicate_count == overlap
    result = BatchStore(conn).commit(full)
    assert (result.inserted, result.skipped_duplicates) == (
        len(synthetic_n26.space_rows(MAIN)) - overlap,
        overlap,
    )
    assert _count(conn, ledger_entry) == len(synthetic_n26.space_rows(MAIN))


def test_duplicate_mixed_file_stages_nothing(conn, n26_accounts, synthetic_n26):
    content = synthetic_n26.mixed_csv_bytes()
    _stage_n26(conn, content)
    batches = _count(conn, import_batch)
    with pytest.raises(DuplicateFile):
        _stage_n26(conn, content)
    assert _count(conn, import_batch) == batches


def _batches_by_account(conn, batch_ids: list[int]) -> dict[str, int]:
    return {BatchStore(conn).report(b).account: b for b in batch_ids}


def test_rejected_space_of_a_mixed_file_can_be_reimported(conn, n26_accounts, synthetic_n26):
    content = synthetic_n26.mixed_csv_bytes()
    batches = _batches_by_account(conn, _stage_n26(conn, content))
    BatchStore(conn).commit(batches["N26 Shared"])
    BatchStore(conn).reject(batches["N26 Main"], "parser fix pending")
    assert list(_batches_by_account(conn, _stage_n26(conn, content))) == ["N26 Main"]


def test_fully_committed_mixed_file_is_refused(conn, n26_accounts, synthetic_n26):
    content = synthetic_n26.mixed_csv_bytes()
    for batch_id in _stage_n26(conn, content):
        BatchStore(conn).commit(batch_id)
    with pytest.raises(DuplicateFile, match="committed"):
        _stage_n26(conn, content)


def test_mixed_file_rows_are_duplicates_in_the_space_export(conn, n26_accounts, synthetic_n26):
    batches = _batches_by_account(conn, _stage_n26(conn, synthetic_n26.mixed_csv_bytes()))
    BatchStore(conn).commit(batches["N26 Shared"])
    [shared] = _stage_n26(conn, synthetic_n26.to_csv_bytes(SHARED), "shared.csv")
    assert BatchStore(conn).report(shared).duplicate_count == 2


def test_empty_space_name_gets_no_account_add_suggestion(conn, n26_accounts, synthetic_n26):
    content = synthetic_n26.to_csv_bytes(MAIN).replace(MAIN.encode(), b"", 1)
    with pytest.raises(UnknownExportAccount) as excinfo:
        _stage_n26(conn, content)
    assert excinfo.value.names == [""]
    assert "--export-name ''" not in str(excinfo.value)


def test_space_names_match_exactly_including_emoji(conn, synthetic_n26):
    AccountService(conn).add("N26 Main", "n26", export_account_name=MAIN)
    AccountService(conn).add("Wrong", "n26", export_account_name=SHARED.replace(" 🫶", ""))
    with pytest.raises(UnknownExportAccount) as excinfo:
        _stage_n26(conn, synthetic_n26.mixed_csv_bytes())
    assert excinfo.value.names == [SHARED]
    assert repr(SHARED) in str(excinfo.value)


def test_routing_ignores_other_households(conn, synthetic_n26):
    AccountService(conn).add(
        "Their Main", "n26", household_name="neighbours", export_account_name=MAIN
    )
    with pytest.raises(UnknownExportAccount):
        _stage_n26(conn, synthetic_n26.to_csv_bytes(MAIN))
