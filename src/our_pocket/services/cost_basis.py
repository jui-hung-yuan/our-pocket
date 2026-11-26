"""What the shares still held cost, by the average-cost method (D-031).

A buy adds the cash it took, fees included. A sale removes the average cost of the shares
sold, so the remaining shares keep their average. Market value needs daily prices (V2).
"""

from collections.abc import Hashable
from datetime import date
from decimal import Decimal


class MissingBuys(ValueError):
    """A sale of more shares than the ledger holds: buys before it aren't in the ledger, so
    what the rest cost can't be known."""


class CostBasis:
    def __init__(self) -> None:
        self._held: dict[Hashable, tuple[Decimal, Decimal]] = {}  # key → (quantity, cost)

    def buy(self, key: Hashable, quantity: Decimal, cost: Decimal) -> None:
        held, paid = self._held.get(key, (Decimal(0), Decimal(0)))
        self._held[key] = (held + quantity, paid + cost)

    def sell(self, key: Hashable, quantity: Decimal, day: date) -> None:
        """Remove `quantity` (positive) shares of `key`, sold on `day`."""
        held, paid = self._held.get(key, (Decimal(0), Decimal(0)))
        if quantity > held:
            raise MissingBuys(
                f"{day}: sold {quantity} of {key} but the ledger holds {held}; "
                "buys before this sale are missing"
            )
        if quantity == held:
            self._held.pop(key, None)
        else:
            self._held[key] = (held - quantity, paid * (held - quantity) / held)

    @property
    def total(self) -> Decimal:
        return sum((paid for _, paid in self._held.values()), Decimal(0))
