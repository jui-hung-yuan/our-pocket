"""Balances page: the numbers it shows, and the step-chart data behind it."""

from datetime import date
from decimal import Decimal

import pytest
from streamlit.testing.v1 import AppTest

from our_pocket.etl.load import BatchStore
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.interfaces.web.balances import chart_records, point_on
from our_pocket.services import AccountService, HistoryPoint, LedgerService
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic.trade_republic import SyntheticExport

POINTS = [
    HistoryPoint(date(2025, 1, 15), Decimal("100"), Decimal("0")),
    HistoryPoint(date(2025, 3, 2), Decimal("40"), Decimal("60")),
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
    assert _metrics(page) == {
        "Total": _euros(latest.total),
        "Cash": _euros(latest.cash),
        "Positions at cost": _euros(latest.positions_at_cost),
    }


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
