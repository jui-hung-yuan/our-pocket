"""LedgerService.history: cash and positions at cost at the end of each day."""

from datetime import date
from decimal import Decimal

import pytest

from our_pocket.etl.load import BatchStore
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.services import AccountService, LedgerService, MissingBuys
from our_pocket.services.cost_basis import CostBasis
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic.trade_republic import SyntheticExport

pytestmark = pytest.mark.db


def _commit(conn, account, content: bytes, name: str = "export.csv") -> None:
    batch_id = ImportPipeline(conn).run(content, name, account)
    BatchStore(conn).commit(batch_id, accept_warnings=True)


@pytest.fixture
def trade_republic(conn, tr_account, synthetic):
    _commit(conn, tr_account, synthetic.to_csv_bytes())
    return synthetic


def test_cash_on_every_day_is_the_cash_balance_up_to_that_day(conn, trade_republic):
    ledger = LedgerService(conn)
    history = ledger.history()
    assert [p.day for p in history] == sorted(
        {date.fromisoformat(r["date"]) for r in trade_republic.rows}
    )
    for point in history:
        assert point.cash == ledger.cash_balance(as_of=point.day).known, point.day
    assert history[-1].cash == trade_republic.cash


def test_positions_at_cost_follow_the_trades_in_the_export(conn, trade_republic):
    """Replays the export's own rows (not the ledger) through the cost basis: checks the
    history picks the trades, their order and their signs from the ledger correctly."""
    expected = CostBasis()
    by_day: dict[str, Decimal] = {}
    for row in sorted(trade_republic.rows, key=lambda r: (r["date"], r["datetime"])):
        if row["type"] == "BUY":
            paid = -(Decimal(row["amount"]) + Decimal(row["fee"] or 0) + Decimal(row["tax"] or 0))
            expected.buy(row["symbol"], Decimal(row["shares"]), paid)
        elif row["type"] == "SELL":
            expected.sell(row["symbol"], -Decimal(row["shares"]), date.fromisoformat(row["date"]))
        by_day[row["date"]] = expected.total
    history = LedgerService(conn).history()
    assert {p.day.isoformat(): p.positions_at_cost for p in history} == by_day
    assert history[-1].positions_at_cost > 0


def test_household_total_counts_every_space_once(conn, synthetic_n26):
    """P2: moves between spaces leave the household total unchanged, so the total is the
    sum of the spaces' balances, and of each account's own history."""
    accounts = AccountService(conn)
    names = []
    for i, space in enumerate(synth_n26.SPACES):
        account = accounts.add(f"N26 {i}", "n26", owner="Alex", export_account_name=space)
        _commit(conn, account, synthetic_n26.to_csv_bytes(space), f"{i}.csv")
        names.append(account.name)
    ledger = LedgerService(conn)
    household = ledger.history()[-1]
    assert household.total == sum(synthetic_n26.balances().values())
    assert household.total == sum(ledger.history(name)[-1].total for name in names)
    assert household.positions_at_cost == 0


def test_one_account_only(conn, trade_republic, synthetic_n26):
    space = synth_n26.SPACES[0]
    n26 = AccountService(conn).add("N26", "n26", owner="Alex", export_account_name=space)
    _commit(conn, n26, synthetic_n26.to_csv_bytes(space), "n26.csv")
    assert LedgerService(conn).history("TR Depot")[-1].cash == trade_republic.cash


def test_one_institution_only(conn, trade_republic, synthetic_n26):
    accounts = AccountService(conn)
    for i, space in enumerate(synth_n26.SPACES):
        account = accounts.add(f"N26 {i}", "n26", owner="Alex", export_account_name=space)
        _commit(conn, account, synthetic_n26.to_csv_bytes(space), f"{i}.csv")
    ledger = LedgerService(conn)
    assert ledger.history(institution="n26")[-1].total == sum(synthetic_n26.balances().values())
    assert ledger.history(institution="trade_republic") == ledger.history("TR Depot")
    assert ledger.history("TR Depot", institution="n26") == []  # both conditions apply


def test_last_entry_id_changes_with_each_commit(conn, tr_account, synthetic):
    ledger = LedgerService(conn)
    assert ledger.last_entry_id() == 0
    _commit(conn, tr_account, SyntheticExport(rows=synthetic.rows[:5], holdings={}).to_csv_bytes())
    first = ledger.last_entry_id()
    _commit(conn, tr_account, synthetic.to_csv_bytes(), "full.csv")
    assert 0 < first < ledger.last_entry_id()


def test_a_sale_without_its_buys_is_refused(conn, tr_account, synthetic):
    sold = next(r["symbol"] for r in synthetic.rows if r["type"] == "SELL")
    rows = [r for r in synthetic.rows if not (r["type"] == "BUY" and r["symbol"] == sold)]
    _commit(conn, tr_account, SyntheticExport(rows=rows, holdings={}).to_csv_bytes())
    with pytest.raises(MissingBuys, match=f"sold .* of .*{sold}"):
        LedgerService(conn).history()


def test_a_sale_only_uses_buys_in_its_own_account(conn, tr_account, synthetic):
    """The shares were bought in another depot; this one can't sell them."""
    sold = next(r["symbol"] for r in synthetic.rows if r["type"] == "SELL")
    bought_there = [r for r in synthetic.rows if r["type"] == "BUY" and r["symbol"] == sold]
    sold_here = [r for r in synthetic.rows if r not in bought_there]
    other = AccountService(conn).add("TR Other", "trade_republic", owner="Alex")
    _commit(conn, other, SyntheticExport(rows=bought_there, holdings={}).to_csv_bytes())
    _commit(conn, tr_account, SyntheticExport(rows=sold_here, holdings={}).to_csv_bytes())
    with pytest.raises(MissingBuys):
        LedgerService(conn).history()


def test_empty_ledger_has_no_history(conn):
    assert LedgerService(conn).history() == []
