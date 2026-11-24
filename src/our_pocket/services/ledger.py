"""Read-only queries over committed ledger entries (L3).

This is the surface future agent tools will wrap: it only reads ledger_entry, never
the staging tables, so an agent can't see unreviewed data.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Connection, Select, func, select

from our_pocket.db.schema import account, ledger_entry


@dataclass(frozen=True)
class CashBalance:
    """Summed cash effect, and the entries left out because their cash meaning is unverified
    (cash_effect NULL): their count and summed `amount`, in case it does turn out to be cash."""

    known: Decimal
    unverified_entries: int
    unverified_amount: Decimal


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

    @staticmethod
    def _until(as_of: date | None) -> list[ColumnElement[bool]]:
        return [] if as_of is None else [ledger_entry.c.booking_date <= as_of]

    @staticmethod
    def _for_account(stmt: Select[Any], account_name: str | None) -> Select[Any]:
        if account_name is None:
            return stmt
        return stmt.join(account, account.c.id == ledger_entry.c.account_id).where(
            account.c.name == account_name
        )
