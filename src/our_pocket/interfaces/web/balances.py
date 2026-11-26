"""Balances page: total, cash and positions at cost over time, for everything, one
institution or one account, and each account's total side by side."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import altair as alt
import streamlit as st

from our_pocket.interfaces.web import database
from our_pocket.services import AccountService, HistoryPoint, MissingBuys

INSTITUTION_NAMES = {"n26": "N26", "trade_republic": "Trade Republic"}
PERIODS: dict[str, int | None] = {"3 months": 91, "1 year": 365, "All": None}
# Fixed colours for the balance lines, so total and cash never get two shades of one hue.
BALANCE_COLOURS = {"total": "#4c4c4c", "cash": "#1f77b4", "positions at cost": "#ff7f0e"}


@dataclass(frozen=True)
class Scope:
    account_name: str | None = None
    institution: str | None = None


def point_on(points: Sequence[HistoryPoint], day: date) -> HistoryPoint:
    """The balances at the end of `day`: the latest point on or before it, or zero before
    the first entry."""
    earlier = [p for p in points if p.day <= day]
    return earlier[-1] if earlier else HistoryPoint(day, Decimal(0), Decimal(0))


def chart_records(
    series: Mapping[str, Sequence[tuple[date, Decimal]]], start: date, end: date
) -> list[dict[str, Any]]:
    """One record per change, for a step chart from `start` to `end`: each line starts with
    its value on `start` and runs on to `end`, even if it last changed earlier."""
    records = []
    for name, values in series.items():
        before = [v for d, v in values if d <= start]
        steps = [(start, before[-1] if before else Decimal(0))]
        steps += [(d, v) for d, v in values if start < d <= end]
        steps.append((end, steps[-1][1]))
        records += [{"day": d.isoformat(), "series": name, "euros": float(v)} for d, v in steps]
    return records


def _chart(records: list[dict[str, Any]], colours: dict[str, str] | None = None) -> alt.Chart:
    if colours:
        scale = alt.Scale(domain=list(colours), range=list(colours.values()))
    else:
        scale = alt.Scale(scheme="tableau10")
    data = alt.Data(values=records)  # type: ignore[no-untyped-call]  # altair leaves it untyped
    chart: alt.Chart = (
        alt.Chart(data)
        .mark_line(interpolate="step-after")
        .encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("euros:Q", title="EUR"),
            color=alt.Color("series:N", title=None, sort=None, scale=scale),
            tooltip=[
                alt.Tooltip("day:T"),
                alt.Tooltip("series:N"),
                alt.Tooltip("euros:Q", format=",.2f"),
            ],
        )
    )
    return chart


def _euros(value: Decimal) -> str:
    return f"{value:,.2f} €"


def render() -> None:
    st.title("Balances")
    version = database.ledger_version()
    if not version:
        st.info("No committed entries yet: import a file and commit it on the Review page.")
        return
    with database.engine().connect() as conn:
        accounts = AccountService(conn).list_accounts()
    institutions = sorted({a.institution for a in accounts})
    scopes = {"All accounts": Scope()}
    scopes |= {
        f"{INSTITUTION_NAMES.get(i, i)}: all accounts": Scope(institution=i) for i in institutions
    }
    scopes |= {a.name: Scope(account_name=a.name) for a in accounts}

    left, right = st.columns([3, 2])
    scope = scopes[left.selectbox("Show", list(scopes))]
    period = right.radio("Period", list(PERIODS), index=1, horizontal=True)
    try:
        points = database.history(version, scope.account_name, scope.institution)
    except MissingBuys as exc:
        st.error(f"Positions at cost can't be computed: {exc}")
        return
    if not points:
        st.info("No committed entries for this selection.")
        return

    end = points[-1].day
    days = PERIODS[period or "All"]
    start = points[0].day if days is None else max(points[0].day, end - timedelta(days=days))
    latest, then = points[-1], point_on(points, start)
    columns = st.columns(3)
    for column, label, now, before in [
        (columns[0], "Total", latest.total, then.total),
        (columns[1], "Cash", latest.cash, then.cash),
        (columns[2], "Positions at cost", latest.positions_at_cost, then.positions_at_cost),
    ]:
        column.metric(label, _euros(now), delta=_euros(now - before), border=True)
    st.caption(
        f"As of {end}; change since {start}. Positions at cost: average cost, fees included "
        "(market value needs daily prices). Cash leaves out entries whose cash meaning is "
        "unverified (STOCKPERK)."
    )
    series = {
        "total": [(p.day, p.total) for p in points],
        "cash": [(p.day, p.cash) for p in points],
        "positions at cost": [(p.day, p.positions_at_cost) for p in points],
    }
    st.altair_chart(_chart(chart_records(series, start, end), BALANCE_COLOURS), width="stretch")

    members = [a for a in accounts if scope.institution in (None, a.institution)]
    if scope.account_name is not None or len(members) < 2:
        return
    st.subheader("By account")
    by_account = {a.name: database.history(version, a.name) for a in members}
    totals = {name: [(p.day, p.total) for p in history] for name, history in by_account.items()}
    st.altair_chart(_chart(chart_records(totals, start, end)), width="stretch")
    st.dataframe(
        [
            {
                "account": a.name,
                "institution": INSTITUTION_NAMES.get(a.institution, a.institution),
                "cash": float(point_on(by_account[a.name], end).cash),
                "positions at cost": float(point_on(by_account[a.name], end).positions_at_cost),
                "total": float(point_on(by_account[a.name], end).total),
            }
            for a in members
        ],
        hide_index=True,
        column_config={
            name: st.column_config.NumberColumn(format="euro")
            for name in ("cash", "positions at cost", "total")
        },
    )
