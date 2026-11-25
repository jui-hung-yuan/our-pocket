"""Reconciliation of the ledger against app numbers, run here on synthetic data: the
snapshot is what the generator says the app would show."""

from datetime import date
from decimal import Decimal

import pytest

from our_pocket.etl.load import BatchStore
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.interfaces.cli import _print_reconciliation
from our_pocket.services import AccountNotFound, AppSnapshot, CashBalance, ReconciliationService
from our_pocket.services.reconciliation import CashCheck, PositionCheck, Reconciliation

LAST_DAY = date(2026, 2, 28)  # the 14-month synthetic export ends in February 2026


def _snapshot(**overrides) -> str:
    fields = {"account": '"TR Depot"', "as_of": '"2026-02-28"', "cash": '"10.00"'} | overrides
    return "{" + ", ".join(f'"{k}": {v}' for k, v in fields.items()) + "}"


# --- the snapshot file -------------------------------------------------------------------


def test_snapshot_keeps_the_precision_typed():
    snapshot = AppSnapshot.from_json(_snapshot(cash="1234.50", positions='{"X": "12.3400"}'))
    assert snapshot.cash == Decimal("1234.50") and str(snapshot.cash) == "1234.50"
    assert str(snapshot.positions["X"]) == "12.3400"
    assert snapshot.as_of == LAST_DAY


def test_snapshot_without_positions_is_cash_only():
    assert AppSnapshot.from_json(_snapshot()).positions is None


@pytest.mark.parametrize(
    "text, message",
    [
        (_snapshot(postions="{}"), "unknown \\['postions'\\]"),
        ('{"account": "TR Depot", "cash": "1"}', "missing \\['as_of'\\]"),
        (_snapshot(cash='"lots"'), "cash must be a number"),
        (_snapshot(cash="true"), "cash must be a number"),
        (_snapshot(positions='["X"]'), "positions must map"),
        ("[]", "JSON object"),
    ],
)
def test_bad_snapshots_are_refused(text, message):
    with pytest.raises(ValueError, match=message):
        AppSnapshot.from_json(text)


@pytest.mark.parametrize(
    "ledger, app, matches",
    [
        ("12.34567", "12.3456", True),  # the app cut off the last digits
        ("12.3458", "12.3456", False),
        ("5", "5", True),
        ("0", "3", False),  # in the app, not in the ledger
    ],
)
def test_positions_match_to_the_digits_typed(ledger, app, matches):
    assert PositionCheck("X", Decimal(ledger), Decimal(app)).matches is matches


def test_position_missing_from_the_app_never_matches():
    assert not PositionCheck("X", Decimal(1), None).matches


# --- against a ledger ----------------------------------------------------------------------


@pytest.fixture
def committed(conn, tr_account, synthetic):
    batch_id = ImportPipeline(conn).run(synthetic.to_csv_bytes(), "tr.csv", tr_account)
    BatchStore(conn).commit(batch_id, accept_warnings=True)
    return synthetic


def _app_view(export, **overrides) -> AppSnapshot:
    fields = {
        "account": "TR Depot",
        "as_of": LAST_DAY,
        "cash": export.cash.quantize(Decimal("0.01")),
        "positions": dict(export.holdings),
    }
    return AppSnapshot(**(fields | overrides))


@pytest.mark.db
def test_ledger_matches_what_the_app_would_show(conn, committed):
    result = ReconciliationService(conn).reconcile(_app_view(committed))
    assert result.matches
    assert [p.isin for p in result.positions] == sorted(committed.holdings)
    assert result.cash.ledger.unverified_entries == 1  # the STOCKPERK row
    assert not result.cash.matches_if_unverified_is_cash


@pytest.mark.db
def test_cash_gap_of_exactly_the_unverified_amounts_is_reported(conn, committed):
    """The STOCKPERK question (Q1): if the app's cash is higher by their amount, they're cash."""
    app_cash = (committed.cash + Decimal("25.00")).quantize(Decimal("0.01"))
    result = ReconciliationService(conn).reconcile(_app_view(committed, cash=app_cash))
    assert not result.cash.matches
    assert result.cash.matches_if_unverified_is_cash


@pytest.mark.db
def test_positions_differing_either_way_are_reported(conn, committed):
    held = dict(committed.holdings)
    dropped_isin, _ = held.popitem()
    held["XX0000000000"] = Decimal(3)
    result = ReconciliationService(conn).reconcile(_app_view(committed, positions=held))
    mismatched = {p.isin: (p.ledger, p.app) for p in result.positions if not p.matches}
    assert mismatched == {
        dropped_isin: (committed.holdings[dropped_isin], None),
        "XX0000000000": (Decimal(0), Decimal(3)),
    }


@pytest.mark.db
def test_only_entries_up_to_the_snapshot_date_count(conn, committed):
    as_of = date(2025, 6, 30)
    upto = [r for r in committed.rows if r["date"] <= as_of.isoformat()]
    held: dict[str, Decimal] = {}
    for r in upto:
        if r["type"] in ("BUY", "SELL"):
            held[r["symbol"]] = held.get(r["symbol"], Decimal(0)) + Decimal(r["shares"])
    cash = sum(
        Decimal(r["amount"]) + Decimal(r["fee"] or 0) + Decimal(r["tax"] or 0)
        for r in upto
        if r["type"] != "STOCKPERK"
    )
    snapshot = _app_view(committed, as_of=as_of, cash=cash, positions=held)
    result = ReconciliationService(conn).reconcile(snapshot)
    assert held and held != committed.holdings
    assert {p.isin: p.ledger for p in result.positions} == held
    assert result.cash.ledger.known == cash
    assert result.matches


@pytest.mark.db
def test_unknown_account_is_refused(conn):
    with pytest.raises(AccountNotFound):
        ReconciliationService(conn).reconcile(AppSnapshot.from_json(_snapshot()))


# --- the printed report --------------------------------------------------------------------


def test_report_says_when_unverified_entries_close_the_gap(capsys):
    snapshot = AppSnapshot("TR Depot", LAST_DAY, Decimal("125.00"), {"X": Decimal("2")})
    cash = CashCheck(CashBalance(Decimal(100), 1, Decimal(25)), snapshot.cash)
    _print_reconciliation(
        Reconciliation(snapshot, cash, [PositionCheck("X", Decimal(2), Decimal(2))])
    )
    out = capsys.readouterr().out
    assert "X" in out and "ok" in out
    assert "MISMATCH" in out
    assert "1 entry with unverified cash meaning, amount 25.00" in out
    assert "closes the gap: they move cash" in out
    assert out.rstrip().endswith("result: differs")
