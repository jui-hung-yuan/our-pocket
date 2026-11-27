"""Read-only queries over committed ledger entries (L3).

This is the surface future agent tools will wrap: it only reads ledger_entry, never
the staging tables, so an agent can't see unreviewed data.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Connection, Select, func, select

from our_pocket.db.schema import account, ledger_entry
from our_pocket.services.cost_basis import CostBasis


@dataclass(frozen=True)
class CashBalance:
    """Summed cash effect, and the entries left out because their cash meaning is unverified
    (cash_effect NULL): their count and summed `amount`, in case it does turn out to be cash."""

    known: Decimal
    unverified_entries: int
    unverified_amount: Decimal


@dataclass(frozen=True)
class HistoryPoint:
    """The end of one day with entries: cash from cash effects (unverified types left out,
    as in CashBalance.known) and the positions held, at cost (D-031), per asset class as the
    export names it ("" for none, D-036). A class with nothing held is absent."""

    day: date
    cash: Decimal
    positions_by_asset_class: Mapping[str, Decimal]

    @property
    def positions_at_cost(self) -> Decimal:
        return sum(self.positions_by_asset_class.values(), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.cash + self.positions_at_cost


class LedgerService:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def positions(
        self, account_name: str | None = None, as_of: date | None = None
    ) -> dict[str, Decimal]:
        """Net quantity per ISIN from buys and sells (non-zero positions only), counting
        entries booked on or before `as_of`."""
        qty = func.sum(ledger_entry.c.quantity)
        stmt = (
            select(ledger_entry.c.isin, qty)
            .where(ledger_entry.c.entry_type.in_(("buy", "sell")), *self._until(as_of))
            .group_by(ledger_entry.c.isin)
            .having(qty != 0)
            .order_by(ledger_entry.c.isin)
        )
        return dict(self.conn.execute(self._for_account(stmt, account_name)).all())

    def cash_balance(
        self, account_name: str | None = None, as_of: date | None = None
    ) -> CashBalance:
        """Cash at the end of `as_of` (or of everything committed), from cash effects."""
        unverified = ledger_entry.c.cash_effect.is_(None)
        stmt = select(
            func.coalesce(func.sum(ledger_entry.c.cash_effect), 0),
            func.count().filter(unverified),
            func.coalesce(func.sum(ledger_entry.c.amount).filter(unverified), 0),
        ).where(*self._until(as_of))
        known, entries, amount = self.conn.execute(self._for_account(stmt, account_name)).one()
        return CashBalance(Decimal(known), entries, Decimal(amount))

    def totals_by_type(
        self, account_name: str | None = None
    ) -> dict[str, tuple[int, Decimal | None]]:
        """Entry count and summed cash effect per entry type."""
        stmt = (
            select(ledger_entry.c.entry_type, func.count(), func.sum(ledger_entry.c.cash_effect))
            .group_by(ledger_entry.c.entry_type)
            .order_by(ledger_entry.c.entry_type)
        )
        return {
            t: (n, cash) for t, n, cash in self.conn.execute(self._for_account(stmt, account_name))
        }

    def taxes(self, account_name: str | None = None) -> dict[str, tuple[int, Decimal]]:
        """Entry count and summed `tax` per entry type, for entries with non-zero tax.

        Negative is tax withheld, positive is tax refunded (e.g. loss offsetting).
        """
        stmt = (
            select(ledger_entry.c.entry_type, func.count(), func.sum(ledger_entry.c.tax))
            .where(ledger_entry.c.tax != 0)
            .group_by(ledger_entry.c.entry_type)
            .order_by(ledger_entry.c.entry_type)
        )
        return {
            t: (n, tax) for t, n, tax in self.conn.execute(self._for_account(stmt, account_name))
        }

    def history(
        self, account_name: str | None = None, institution: str | None = None
    ) -> list[HistoryPoint]:
        """Cash and positions at cost at the end of every day with entries, oldest first,
        for one account, one institution's accounts, or all of them. Raises MissingBuys if
        a sale is larger than the holding before it in its asset class."""
        le = ledger_entry.c
        stmt = select(
            le.booking_date,
            le.account_id,
            le.entry_type,
            le.isin,
            le.asset_class,
            le.quantity,
            le.cash_effect,
        ).order_by(le.booking_date, le.booked_at.nulls_first(), le.id)
        points: list[HistoryPoint] = []
        cash = Decimal(0)
        costs: defaultdict[str, CostBasis] = defaultdict(CostBasis)
        for entry in self.conn.execute(self._for_account(stmt, account_name, institution)):
            cash += entry.cash_effect or 0
            holding = (entry.account_id, entry.isin)
            if entry.entry_type == "buy":
                costs[entry.asset_class or ""].buy(
                    holding,
                    entry.quantity,
                    -entry.cash_effect,  # fees included
                )
            elif entry.entry_type == "sell":
                costs[entry.asset_class or ""].sell(holding, -entry.quantity, entry.booking_date)
            by_class = {asset_class: c.total for asset_class, c in costs.items() if c.total}
            point = HistoryPoint(entry.booking_date, cash, by_class)
            if points and points[-1].day == point.day:
                points[-1] = point
            else:
                points.append(point)
        return points

    def last_entry_id(self) -> int:
        """Changes whenever entries are committed: a cheap cache key for the history."""
        latest: int | None = self.conn.execute(select(func.max(ledger_entry.c.id))).scalar_one()
        return latest or 0

    @staticmethod
    def _until(as_of: date | None) -> list[ColumnElement[bool]]:
        return [] if as_of is None else [ledger_entry.c.booking_date <= as_of]

    @staticmethod
    def _for_account(
        stmt: Select[Any], account_name: str | None, institution: str | None = None
    ) -> Select[Any]:
        conditions = [account.c.name == account_name] if account_name is not None else []
        if institution is not None:
            conditions.append(account.c.institution == institution)
        if not conditions:
            return stmt
        return stmt.join(account, account.c.id == ledger_entry.c.account_id).where(*conditions)
