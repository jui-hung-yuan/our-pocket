"""Reconciliation: the committed ledger against the numbers the institution's app shows.

The synthetic suites only prove the parsers agree with the generators, which encode my
reading of each format. This is the check on that reading (PROJECT_PLAN §6). The app's
numbers come from a snapshot typed in by hand and kept with the real exports:

    {"account": "TR", "as_of": "2026-11-24", "cash": "1234.56",
     "positions": {"<ISIN>": "12.3456"}}

`positions` is optional (bank accounts have none). A value matches when it differs by less
than one unit of the last digit typed, so `12.3456` allows 0.0001: apps round or cut off.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection

from our_pocket.services.accounts import AccountService
from our_pocket.services.ledger import CashBalance, LedgerService

SNAPSHOT_KEYS = {"account", "as_of", "cash", "positions"}


def _precision(value: Decimal) -> Decimal:
    """One unit of the last digit of `value` as typed: 12.30 → 0.01, 5 → 1."""
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return Decimal(1).scaleb(exponent)


def _decimal(value: Any, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ValueError(f"{what} must be a number, got {value!r}")
    try:
        return Decimal(value)
    except ArithmeticError:
        raise ValueError(f"{what} must be a number, got {value!r}") from None


@dataclass(frozen=True)
class AppSnapshot:
    account: str
    as_of: date
    cash: Decimal
    positions: dict[str, Decimal] | None = None

    @classmethod
    def from_json(cls, text: str) -> "AppSnapshot":
        data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
        if not isinstance(data, dict):
            raise ValueError("snapshot must be a JSON object")
        unknown = set(data) - SNAPSHOT_KEYS
        missing = {"account", "as_of", "cash"} - set(data)
        if unknown or missing:
            raise ValueError(
                f"snapshot keys: unknown {sorted(unknown)}, missing {sorted(missing)}; "
                f"expected {sorted(SNAPSHOT_KEYS)}"
            )
        positions = data.get("positions")
        if positions is not None and not isinstance(positions, dict):
            raise ValueError("positions must map ISIN to quantity")
        return cls(
            account=str(data["account"]),
            as_of=date.fromisoformat(str(data["as_of"])),
            cash=_decimal(data["cash"], "cash"),
            positions=None
            if positions is None
            else {isin: _decimal(q, f"position {isin}") for isin, q in positions.items()},
        )


@dataclass(frozen=True)
class PositionCheck:
    isin: str
    ledger: Decimal  # 0 when the ledger doesn't hold it
    app: Decimal | None  # None when the app doesn't show it

    @property
    def difference(self) -> Decimal:
        return self.ledger - (self.app or 0)

    @property
    def matches(self) -> bool:
        if self.app is None:
            return False
        return abs(self.difference) < _precision(self.app)


@dataclass(frozen=True)
class CashCheck:
    ledger: CashBalance
    app: Decimal

    @property
    def difference(self) -> Decimal:
        return self.ledger.known - self.app

    @property
    def matches(self) -> bool:
        return abs(self.difference) < _precision(self.app)

    @property
    def matches_if_unverified_is_cash(self) -> bool:
        """Would counting the unverified entries' amounts as cash close the gap? That's
        the evidence that settles what such a type moves (Q1)."""
        if self.ledger.unverified_entries == 0:
            return False
        gap = self.difference + self.ledger.unverified_amount
        return abs(gap) < _precision(self.app)


@dataclass(frozen=True)
class Reconciliation:
    snapshot: AppSnapshot
    cash: CashCheck
    positions: list[PositionCheck]  # empty when the snapshot has none

    @property
    def matches(self) -> bool:
        return self.cash.matches and all(p.matches for p in self.positions)


class ReconciliationService:
    def __init__(self, conn: Connection) -> None:
        self.accounts = AccountService(conn)
        self.ledger = LedgerService(conn)

    def reconcile(self, snapshot: AppSnapshot) -> Reconciliation:
        """Compare entries booked on or before `snapshot.as_of` with the app's numbers."""
        name = self.accounts.get(snapshot.account).name
        cash = CashCheck(self.ledger.cash_balance(name, snapshot.as_of), snapshot.cash)
        if snapshot.positions is None:
            return Reconciliation(snapshot, cash, [])
        held = self.ledger.positions(name, snapshot.as_of)
        positions = [
            PositionCheck(isin, held.get(isin, Decimal(0)), snapshot.positions.get(isin))
            for isin in sorted(held.keys() | snapshot.positions.keys())
        ]
        return Reconciliation(snapshot, cash, positions)
