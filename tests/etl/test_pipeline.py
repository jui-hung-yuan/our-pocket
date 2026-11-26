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
    AccountRequired,
    BatchStore,
    CommitBlocked,
    DuplicateFile,
    InvalidBatchState,
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
    return ImportPipeline(conn).run(export.to_csv_bytes(), name, account)


def _count(conn, table, **where) -> int:
    return conn.execute(
        select(func.count()).select_from(table).where(*(table.c[k] == v for k, v in where.items()))
    ).scalar_one()


def test_happy_path_reaches_the_ledger_with_ground_truth_holdings(conn, tr_account, synthetic):
    batch_id = _stage(conn, synthetic, tr_account)

    report = BatchStore(conn).report(batch_id)
    assert report.status == "staged"
    assert (report.schema_id, report.schema_version, report.parser_version) == (
        "trade_republic.transactions_csv", 1, "4",
    )  # fmt: skip
    assert report.row_count == len(synthetic.rows)
    assert report.errors == 0
    assert report.warnings == 0
    assert _count(conn, ledger_entry) == 0, "staging must never touch the ledger"

    result = BatchStore(conn).commit(batch_id)
    assert (result.inserted, result.skipped_duplicates) == (len(synthetic.rows), 0)
    assert BatchStore(conn).report(batch_id).status == "committed"

    ledger = LedgerService(conn)
    assert ledger.positions(tr_account.name) == synthetic.holdings
    totals = ledger.totals_by_type(tr_account.name)
    assert sum(n for n, _ in totals.values()) == len(synthetic.rows)
    perks = [Decimal(r["amount"]) for r in synthetic.rows if r["type"] == "STOCKPERK"]
    assert totals["bonus"] == (len(perks), sum(perks))  # STOCKPERK is cash (D-035)

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


def test_out_of_order_imports_end_with_the_same_ledger(conn, tr_account, synthetic):
    half = len(synthetic.rows) // 2
    later = SyntheticExport(rows=synthetic.rows[half:], holdings={})
    earlier = SyntheticExport(rows=synthetic.rows[:half], holdings={})
    for name, export in (("later.csv", later), ("earlier.csv", earlier), ("full.csv", synthetic)):
        BatchStore(conn).commit(_stage(conn, export, tr_account, name), accept_warnings=True)
    assert _count(conn, ledger_entry) == len(synthetic.rows)
    assert LedgerService(conn).positions() == synthetic.holdings
    assert LedgerService(conn).cash_balance().known == synthetic.cash


def test_overlapping_exports_staged_before_either_commit(conn, tr_account, synthetic):
    """Neither batch sees the other's rows in the ledger at staging; the unique constraint
    keeps the second commit from adding them twice (D-008)."""
    first = SyntheticExport(rows=synthetic.rows[:40], holdings={})
    early = _stage(conn, first, tr_account, "jan.csv")
    full = _stage(conn, synthetic, tr_account, "full.csv")
    BatchStore(conn).commit(full, accept_warnings=True)
    result = BatchStore(conn).commit(early, accept_warnings=True)
    assert result.inserted == 0
    assert _count(conn, ledger_entry) == len(synthetic.rows)
    assert LedgerService(conn).positions() == synthetic.holdings


def test_resaved_file_adds_nothing(conn, tr_account, synthetic):
    """Other bytes, same rows (re-saved with Windows line endings): the file hash differs,
    so only transaction ids stop the rows counting twice."""
    BatchStore(conn).commit(_stage(conn, synthetic, tr_account), accept_warnings=True)
    resaved = synthetic.to_csv_bytes().replace(b"\n", b"\r\n")
    batch_id = ImportPipeline(conn).run(resaved, "tr (1).csv", tr_account)
    result = BatchStore(conn).commit(batch_id, accept_warnings=True)
    assert (result.inserted, result.skipped_duplicates) == (0, len(synthetic.rows))


def test_batches_are_found_by_status(conn, tr_account, synthetic):
    store = BatchStore(conn)
    committed = _stage(
        conn, SyntheticExport(rows=synthetic.rows[:5], holdings={}), tr_account, "a.csv"
    )
    store.commit(committed, accept_warnings=True)
    rejected = _stage(
        conn, SyntheticExport(rows=synthetic.rows[5:9], holdings={}), tr_account, "b.csv"
    )
    store.reject(rejected, "test")
    staged = [
        _stage(conn, SyntheticExport(rows=synthetic.rows[:n], holdings={}), tr_account, name)
        for n, name in ((20, "c.csv"), (30, "d.csv"))
    ]
    assert store.find_batch_ids("staged") == staged
    assert store.find_batch_ids("committed") == [committed]
    assert store.find_batch_ids("rejected") == [rejected]


def test_raw_rows_are_found_by_line_number(conn, tr_account, synthetic):
    """The file's own fields, so a reviewer sees what a flagged line said. Line 1 is the
    header, so data row i is on line i + 2."""
    batch_id = _stage(conn, synthetic, tr_account)
    raw_rows = BatchStore(conn).find_raw_rows(batch_id, [2, 5, 10_000])
    assert sorted(raw_rows) == [2, 5]  # a line past the end is simply absent
    assert raw_rows[5]["transaction_id"] == synthetic.rows[3]["transaction_id"]


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
    batch_id = ImportPipeline(conn).run(content, "n26.csv", tr_account)
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


def _flag_codes(conn, batch_id: int) -> list[str]:
    return [f["code"] for f in BatchStore(conn).report(batch_id).flags]


def test_n26_end_to_end_balances_per_space(conn, n26_accounts, synthetic_n26: SyntheticN26):
    for space in SPACES:
        content = synthetic_n26.to_csv_bytes(space)
        batch_id = ImportPipeline(conn).run(content, f"{space}.csv", n26_accounts[space])
        BatchStore(conn).commit(batch_id)  # every new entry type passes the CHECK constraint
    assert _cash_by_account(conn) == {
        n26_accounts[space].name: balance for space, balance in synthetic_n26.balances().items()
    }


def test_n26_still_needs_an_account(conn, synthetic_n26):
    with pytest.raises(AccountRequired):
        ImportPipeline(conn).run(synthetic_n26.to_csv_bytes(MAIN), "n26.csv")
    assert _count(conn, import_batch) == 0


def test_unknown_format_without_account_raises(conn):
    with pytest.raises(UnknownFormat, match="closest known schema"):
        ImportPipeline(conn).run(b"Datum;Betrag\n2025-01-01;1\n", "bank.csv")
    assert _count(conn, import_batch) == 0


def test_deleted_space_rows_are_flagged_and_kept(conn, n26_accounts, synthetic_n26):
    content = synthetic_n26.mixed_csv_bytes()
    batch_id = ImportPipeline(conn).run(content, "main.csv", n26_accounts[MAIN])
    assert _flag_codes(conn, batch_id) == ["unknown_space", "unknown_space"]
    with pytest.raises(CommitBlocked):
        BatchStore(conn).commit(batch_id)
    result = BatchStore(conn).commit(batch_id, accept_warnings=True)
    assert result.inserted == content.count(b"\n") - 1


def test_wrong_account_flags_every_row(conn, n26_accounts, synthetic_n26):
    batch_id = ImportPipeline(conn).run(
        synthetic_n26.to_csv_bytes(SHARED), "shared.csv", n26_accounts[MAIN]
    )
    codes = _flag_codes(conn, batch_id)
    assert codes == ["other_account"] * len(synthetic_n26.space_rows(SHARED))


def test_rows_of_a_space_with_its_own_account_block_commit(conn, n26_accounts, synthetic_n26):
    """Accepting them would count them twice: once here, once from their own export."""
    shared_rows = synthetic_n26.to_csv_bytes(SHARED).split(b"\n", 1)[1]
    content = synthetic_n26.to_csv_bytes(MAIN) + shared_rows
    batch_id = ImportPipeline(conn).run(content, "main.csv", n26_accounts[MAIN])
    assert set(_flag_codes(conn, batch_id)) == {"other_account"}
    with pytest.raises(CommitBlocked):
        BatchStore(conn).commit(batch_id, accept_warnings=True)


def test_space_names_match_exactly_including_emoji(conn, synthetic_n26):
    shared = AccountService(conn).add(
        "Shared", "n26", export_account_name=SHARED.replace(" 🫶", "")
    )
    batch_id = ImportPipeline(conn).run(synthetic_n26.to_csv_bytes(SHARED), "shared.csv", shared)
    assert set(_flag_codes(conn, batch_id)) == {"unknown_space"}


def test_two_members_spaces_can_share_a_name(conn, n26_accounts, synthetic_n26):
    """Each member's export calls their own main space by the same default name."""
    sam_main = AccountService(conn).add("Sam Main", "n26", owner="Sam", export_account_name=MAIN)
    batch_id = ImportPipeline(conn).run(synthetic_n26.to_csv_bytes(MAIN), "sam.csv", sam_main)
    assert _flag_codes(conn, batch_id) == []


def test_overlapping_n26_export_adds_nothing(conn, n26_accounts, synthetic_n26):
    main = n26_accounts[MAIN]
    first = ImportPipeline(conn).run(synthetic_n26.to_csv_bytes(MAIN, months=9), "a.csv", main)
    BatchStore(conn).commit(first)
    full = ImportPipeline(conn).run(synthetic_n26.to_csv_bytes(MAIN), "b.csv", main)
    overlap = len(synthetic_n26.space_rows(MAIN, months=9))
    assert BatchStore(conn).report(full).duplicate_count == overlap
    result = BatchStore(conn).commit(full)
    assert (result.inserted, result.skipped_duplicates) == (
        len(synthetic_n26.space_rows(MAIN)) - overlap,
        overlap,
    )
    assert _count(conn, ledger_entry) == len(synthetic_n26.space_rows(MAIN))
