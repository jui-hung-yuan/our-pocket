"""Read-only queries over committed ledger entries (L3).

This is the surface future agent tools will wrap: it only reads ledger_entry, never
the staging tables, so an agent can't see unreviewed data.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Select, func, select

from our_pocket.db.schema import account, ledger_entry


class LedgerService:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def positions(self, account_name: str | None = None) -> dict[str, Decimal]:
        """Net quantity per ISIN from buys and sells (non-zero positions only)."""
        qty = func.sum(ledger_entry.c.quantity)
        stmt = (
            select(ledger_entry.c.isin, qty)
            .where(ledger_entry.c.entry_type.in_(("buy", "sell")))
            .group_by(ledger_entry.c.isin)
            .having(qty != 0)
            .order_by(ledger_entry.c.isin)
        )
        return dict(self.conn.execute(self._for_account(stmt, account_name)).all())

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
    def _for_account(stmt: Select[Any], account_name: str | None) -> Select[Any]:
        if account_name is None:
            return stmt
        return stmt.join(account, account.c.id == ledger_entry.c.account_id).where(
            account.c.name == account_name
        )
