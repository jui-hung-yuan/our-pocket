"""Average cost: what the shares still held cost (D-031)."""

from datetime import date
from decimal import Decimal

import pytest

from our_pocket.services import MissingBuys
from our_pocket.services.cost_basis import CostBasis

DAY = date(2025, 3, 1)


def test_buys_add_up():
    cost = CostBasis()
    cost.buy("X", Decimal(10), Decimal("101.00"))  # 10 x 10.00 plus a 1.00 fee
    cost.buy("X", Decimal(10), Decimal("201.00"))
    assert cost.total == Decimal("302.00")


def test_a_sale_removes_the_average_cost_of_the_shares_sold():
    cost = CostBasis()
    cost.buy("X", Decimal(10), Decimal("100"))
    cost.buy("X", Decimal(10), Decimal("300"))  # average 20 a share
    cost.sell("X", Decimal(5), DAY)
    assert cost.total == Decimal("300")  # 15 shares x 20, whatever the sale fetched


def test_selling_everything_leaves_no_cost_behind():
    cost = CostBasis()
    cost.buy("X", Decimal("3.3333"), Decimal("100"))
    cost.sell("X", Decimal("1.1111"), DAY)
    cost.sell("X", Decimal("2.2222"), DAY)
    assert cost.total == 0  # exactly, not a rounding remainder


def test_holdings_are_kept_apart_by_key():
    cost = CostBasis()
    cost.buy(("TR 1", "X"), Decimal(1), Decimal("10"))
    cost.buy(("TR 2", "X"), Decimal(1), Decimal("30"))
    cost.sell(("TR 1", "X"), Decimal(1), DAY)
    assert cost.total == Decimal("30")


@pytest.mark.parametrize("bought", [None, Decimal(1)])
def test_selling_more_than_held_is_refused(bought):
    cost = CostBasis()
    if bought:
        cost.buy("X", bought, Decimal("10"))
    with pytest.raises(MissingBuys, match="2025-03-01: sold 2 of X"):
        cost.sell("X", Decimal(2), DAY)
