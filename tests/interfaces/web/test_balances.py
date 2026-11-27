"""Balances page: the numbers it shows, and the step-chart data behind it."""

from datetime import date
from decimal import Decimal

import pytest
from streamlit.testing.v1 import AppTest

from our_pocket.etl.load import BatchStore
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.interfaces.web.balances import (
    balance_series,
    chart_records,
    label_asset_class,
    point_on,
    select_metrics,
)
from our_pocket.services import AccountService, HistoryPoint, LedgerService
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic.trade_republic import SyntheticExport

POINTS = [
    HistoryPoint(date(2025, 1, 15), Decimal("100"), {}),
    HistoryPoint(date(2025, 3, 2), Decimal("40"), {"STOCK": Decimal("60")}),
]


# --- data for the chart (no database) ----------------------------------------------------


@pytest.mark.parametrize(
    "day, cash",
    [
        (date(2025, 1, 1), 0),
        (date(2025, 1, 15), 100),
        (date(2025, 2, 10), 100),
        (date(2026, 1, 1), 40),
    ],
)
def test_point_on_a_day_is_the_latest_before_it(day, cash):
    assert point_on(POINTS, day).cash == cash


def test_each_line_spans_the_whole_period():
    """Starts at its value on the first day, steps at each change, runs on to the end."""
    series = {"cash": [(p.day, p.cash) for p in POINTS], "new": [(date(2025, 3, 1), Decimal(5))]}
    records = chart_records(series, date(2025, 2, 1), date(2025, 4, 30))
    assert [(r["series"], r["day"], r["euros"]) for r in records] == [
        ("cash", "2025-02-01", 100.0),
        ("cash", "2025-03-02", 40.0),
        ("cash", "2025-04-30", 40.0),
        ("new", "2025-02-01", 0.0),  # opened later: zero before
        ("new", "2025-03-01", 5.0),
        ("new", "2025-04-30", 5.0),
    ]


def test_a_change_on_the_last_day_is_one_record_not_two():
    """Two records on one day would be counted twice where the chart stacks the series."""
    records = chart_records(
        {"cash": [(p.day, p.cash) for p in POINTS]}, POINTS[0].day, POINTS[-1].day
    )
    assert [(r["day"], r["euros"]) for r in records] == [
        ("2025-01-15", 100.0),
        ("2025-03-02", 40.0),
    ]


@pytest.mark.parametrize(
    "asset_class, label",
    [("STOCK", "Stocks"), ("FUND", "ETFs"), ("CRYPTO", "Crypto"), ("", "Unclassified")],
)
def test_asset_classes_get_readable_labels(asset_class, label):
    assert label_asset_class(asset_class) == label


def test_series_has_cash_then_each_class_seen_with_zero_when_absent():
    points = [
        HistoryPoint(date(2025, 1, 1), Decimal(10), {"FUND": Decimal(5), "": Decimal(1)}),
        HistoryPoint(date(2025, 2, 1), Decimal(10), {"STOCK": Decimal(7), "BOND": Decimal(2)}),
    ]
    days = [date(2025, 1, 1), date(2025, 2, 1)]
    assert balance_series(points) == {
        "Cash": list(zip(days, [Decimal(10), Decimal(10)], strict=True)),
        "Stocks": list(zip(days, [Decimal(0), Decimal(7)], strict=True)),
        "ETFs": list(zip(days, [Decimal(5), Decimal(0)], strict=True)),
        "Bond": list(zip(days, [Decimal(0), Decimal(2)], strict=True)),
        "Unclassified": list(zip(days, [Decimal(1), Decimal(0)], strict=True)),
    }
    assert list(balance_series(points)) == ["Cash", "Stocks", "ETFs", "Bond", "Unclassified"]


def test_metrics_keep_a_class_sold_out_in_the_period_and_drop_one_never_held():
    series = {
        "Cash": [(date(2025, 1, 1), Decimal(0))],
        "Stocks": [(date(2025, 1, 1), Decimal(7)), (date(2025, 6, 1), Decimal(0))],
        "ETFs": [(date(2025, 1, 1), Decimal(0)), (date(2025, 9, 1), Decimal(4))],
        "Bond": [(date(2025, 1, 1), Decimal(3)), (date(2025, 2, 1), Decimal(0))],
    }
    assert select_metrics(series, date(2025, 3, 1), date(2025, 12, 31)) == [
        ("Cash", Decimal(0), Decimal(0)),  # always shown
        ("Stocks", Decimal(0), Decimal(7)),  # sold out in the period
        ("ETFs", Decimal(4), Decimal(0)),  # bought in the period
    ]  # Bond: sold out before the period, not shown


# --- the page ----------------------------------------------------------------------------


def _page():
    from our_pocket.interfaces.web import balances

    balances.render()


def _commit(conn, account, content: bytes) -> None:
    BatchStore(conn).commit(
        ImportPipeline(conn).run(content, "x.csv", account), accept_warnings=True
    )


@pytest.fixture
def household(app_database, tr_account, synthetic, synthetic_n26):
    conn = app_database
    _commit(conn, tr_account, synthetic.to_csv_bytes())
    for space, name in [(synth_n26.MAIN, "N26 Main"), (synth_n26.SAVINGS, "N26 Savings")]:
        account = AccountService(conn).add(name, "n26", owner="Alex", export_account_name=space)
        _commit(conn, account, synthetic_n26.to_csv_bytes(space))
    return conn


def _metrics(page: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in page.metric}


def _euros(value: Decimal) -> str:
    return f"{value:,.2f} €"


@pytest.mark.db
def test_empty_ledger_says_what_to_do(app_database):
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert not page.exception
    assert "No committed entries yet" in page.info[0].value


@pytest.mark.db
def test_scopes_are_everything_each_institution_and_each_account(household):
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert page.selectbox[0].options == [
        "All accounts",
        "N26: all accounts",
        "Trade Republic: all accounts",
        "N26 Main",
        "N26 Savings",
        "TR Depot",
    ]


@pytest.mark.db
@pytest.mark.parametrize(
    "scope, account_name, institution",
    [
        ("All accounts", None, None),
        ("N26: all accounts", None, "n26"),
        ("TR Depot", "TR Depot", None),
    ],
)
def test_metrics_are_the_latest_balances(household, scope, account_name, institution):
    latest = LedgerService(household).history(account_name, institution)[-1]
    page = AppTest.from_function(_page, default_timeout=10).run()
    page.selectbox[0].select(scope).run()
    assert not page.exception
    expected = [("Total", _euros(latest.total)), ("Cash", _euros(latest.cash))]
    by_class = latest.positions_by_asset_class
    expected += [
        (label, _euros(by_class[asset_class]))
        for asset_class, label in (("STOCK", "Stocks"), ("FUND", "ETFs"))
        if asset_class in by_class
    ]
    assert list(_metrics(page).items()) == expected
    if institution == "n26":
        assert [label for label, _ in expected] == ["Total", "Cash"]


@pytest.mark.db
def test_accounts_side_by_side_only_for_more_than_one(household):
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert [h.value for h in page.subheader] == ["By account"]
    assert len(page.dataframe) == 1
    page.selectbox[0].select("TR Depot").run()
    assert not page.subheader and not page.dataframe


@pytest.mark.db
def test_a_commit_elsewhere_shows_without_a_restart(app_database, tr_account, synthetic):
    """The cached history is keyed on the ledger, so a CLI commit refreshes the page."""
    _commit(
        app_database,
        tr_account,
        SyntheticExport(rows=synthetic.rows[:20], holdings={}).to_csv_bytes(),
    )
    page = AppTest.from_function(_page, default_timeout=10).run()
    first = _metrics(page)["Total"]
    _commit(app_database, tr_account, synthetic.to_csv_bytes())
    page.run()
    assert _metrics(page)["Total"] != first
    assert _metrics(page)["Total"] == _euros(LedgerService(app_database).history()[-1].total)


@pytest.mark.db
def test_missing_buys_are_shown_not_crashed(app_database, tr_account, synthetic):
    sold = next(r["symbol"] for r in synthetic.rows if r["type"] == "SELL")
    rows = [r for r in synthetic.rows if not (r["type"] == "BUY" and r["symbol"] == sold)]
    _commit(app_database, tr_account, SyntheticExport(rows=rows, holdings={}).to_csv_bytes())
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert not page.exception
    assert "Positions at cost can't be computed" in page.error[0].value
