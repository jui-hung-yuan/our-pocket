"""Balances page: total, cash and positions at cost per asset class over time, for
everything, one institution or one account, and each account's total side by side."""

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
ASSET_CLASS_LABELS = {"STOCK": "Stocks", "FUND": "ETFs", "": "Unclassified"}
# Stack order after cash; other classes follow alphabetically, Unclassified last.
ASSET_CLASS_ORDER = ("STOCK", "FUND")
# Fixed colours, so a series keeps its colour whichever classes a scope holds.
SERIES_COLOURS = {"Cash": "#1f77b4", "Stocks": "#ff7f0e", "ETFs": "#2ca02c"}
OTHER_COLOURS = ("#9467bd", "#8c564b", "#e377c2", "#bcbd22", "#17becf")
UNCLASSIFIED_COLOUR = "#7f7f7f"
TOTAL_COLOUR = "#4c4c4c"


@dataclass(frozen=True)
class Scope:
    account_name: str | None = None
    institution: str | None = None


def label_asset_class(asset_class: str) -> str:
    return ASSET_CLASS_LABELS.get(asset_class) or asset_class.title()


def balance_series(points: Sequence[HistoryPoint]) -> dict[str, list[tuple[date, Decimal]]]:
    """Cash, then positions at cost per asset class seen anywhere in `points`, each with a
    value on every point's day (0 while the class isn't held), in stack order."""
    seen = {c for p in points for c in p.positions_by_asset_class}
    ordered = sorted(
        seen,
        key=lambda c: (
            c == "",
            ASSET_CLASS_ORDER.index(c) if c in ASSET_CLASS_ORDER else len(ASSET_CLASS_ORDER),
            label_asset_class(c),
        ),
    )
    series = {"Cash": [(p.day, p.cash) for p in points]}
    for asset_class in ordered:
        series[label_asset_class(asset_class)] = [
            (p.day, p.positions_by_asset_class.get(asset_class, Decimal(0))) for p in points
        ]
    return series


def _value_on(values: Sequence[tuple[date, Decimal]], day: date) -> Decimal:
    earlier = [v for d, v in values if d <= day]
    return earlier[-1] if earlier else Decimal(0)


def choose_metrics_to_show(
    series: Mapping[str, Sequence[tuple[date, Decimal]]], start: date, end: date
) -> list[tuple[str, Decimal, Decimal]]:
    """(name, value on `end`, value on `start`) for Cash and each class held on either day,
    so a class sold out during the period still shows, at 0."""
    metrics = []
    for name, values in series.items():
        now, before = _value_on(values, end), _value_on(values, start)
        if name == "Cash" or now or before:
            metrics.append((name, now, before))
    return metrics


def point_on(points: Sequence[HistoryPoint], day: date) -> HistoryPoint:
    """The balances at the end of `day`: the latest point on or before it, or zero before
    the first entry."""
    earlier = [p for p in points if p.day <= day]
    return earlier[-1] if earlier else HistoryPoint(day, Decimal(0), {})


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
        if steps[-1][0] < end:
            steps.append((end, steps[-1][1]))
        records += [{"day": d.isoformat(), "series": name, "euros": float(v)} for d, v in steps]
    return records


def _tooltip() -> list[alt.Tooltip]:
    return [alt.Tooltip("day:T"), alt.Tooltip("series:N"), alt.Tooltip("euros:Q", format=",.2f")]


def _chart(records: list[dict[str, Any]]) -> alt.Chart:
    """One line per series."""
    data = alt.Data(values=records)  # type: ignore[no-untyped-call]  # altair leaves it untyped
    chart: alt.Chart = (
        alt.Chart(data)
        .mark_line(interpolate="step-after")
        .encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("euros:Q", title="EUR"),
            color=alt.Color("series:N", title=None, sort=None, scale=alt.Scale(scheme="tableau10")),
            tooltip=_tooltip(),
        )
    )
    return chart


def _colours(names: Sequence[str]) -> list[str]:
    others = iter(OTHER_COLOURS * len(names))
    return [
        SERIES_COLOURS.get(name)
        or (UNCLASSIFIED_COLOUR if name == ASSET_CLASS_LABELS[""] else next(others))
        for name in names
    ]


def balance_chart(
    records: list[dict[str, Any]], names: Sequence[str], total_records: list[dict[str, Any]]
) -> alt.LayerChart:
    """The series stacked in `names` order, with the total as a line over them: with cash
    below zero, the stack's top edge is not the total."""
    ranked = [r | {"rank": names.index(r["series"])} for r in records]
    areas = alt.Data(values=ranked)  # type: ignore[no-untyped-call]  # altair leaves it untyped
    totals = alt.Data(values=total_records)  # type: ignore[no-untyped-call]
    scale = alt.Scale(domain=list(names), range=_colours(names))
    x = alt.X("day:T", title=None)
    stack = (
        alt.Chart(areas)
        .mark_area(interpolate="step-after")
        .encode(
            x=x,
            y=alt.Y("euros:Q", title="EUR", stack="zero"),
            color=alt.Color("series:N", title=None, sort=list(names), scale=scale),
            order=alt.Order("rank:Q"),
            tooltip=_tooltip(),
        )
    )
    total = (
        alt.Chart(totals)
        .mark_line(interpolate="step-after", color=TOTAL_COLOUR)
        .encode(x=x, y=alt.Y("euros:Q"), tooltip=_tooltip())
    )
    return alt.LayerChart(layer=[stack, total])


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
    series = balance_series(points)
    metrics = [("Total", latest.total, then.total), *choose_metrics_to_show(series, start, end)]
    for column, (label, now, before) in zip(st.columns(len(metrics)), metrics, strict=True):
        column.metric(label, _euros(now), delta=_euros(now - before), border=True)
    st.caption(
        f"As of {end}; change since {start}. The dark line is the total. Positions at "
        "cost: average cost, fees included (market value needs daily prices). Cash leaves out "
        "entries whose cash meaning is unverified."
    )
    total = {"Total": [(p.day, p.total) for p in points]}
    chart = balance_chart(
        chart_records(series, start, end), list(series), chart_records(total, start, end)
    )
    st.altair_chart(chart, width="stretch")

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
