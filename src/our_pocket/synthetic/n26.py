"""Synthetic N26 transaction exports (file schema v1), one per space.

Three invented spaces: a personal main account, a shared space and a savings space.
Money moves between them every month, and both legs of each move are recorded in
`transfer_pairs`. The other member of the shared space also pays into it from a space
of theirs that isn't exported, so those moves have one leg only. Values follow the real
export's shape (see our_pocket.etl.transform.n26): signed `Amount (EUR)` without
trailing zeros, unsigned `Original Amount` on card rows only, dates without times.
"""

import csv
import io
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from our_pocket.etl.transform.validators import iban_check_digits

HEADER: tuple[str, ...] = (
    "Booking Date",
    "Value Date",
    "Partner Name",
    "Partner Iban",
    "Type",
    "Payment Reference",
    "Account Name",
    "Amount (EUR)",
    "Original Amount",
    "Original Currency",
    "Exchange Rate",
)

MAIN = "Alex Example"
SHARED = "Alex & Sam Example 🫶"
SAVINGS = "Savings Example"
SPACES: tuple[str, str, str] = (MAIN, SHARED, SAVINGS)
# The other member's space: a partner name only, never an `Account Name` here.
OTHER_MEMBER_MAIN = "Sam Main Example"
# A space deleted in the app; its rows turn up in the main space's export only.
DELETED = "Old Trip Example"

CENT = Decimal("0.01")
SHOPS = ("Example Bakery", "Example Cafe", "Example Books", "Example Pharmacy")
GROCERS = ("Example Market", "Example Organic", "Example Discount")
# EUR per unit of the foreign currency.
FX_RATES = {"DKK": Decimal("0.1338053097"), "USD": Decimal("0.8689075630")}


def _iban(bban: str) -> str:
    return f"DE{iban_check_digits('DE', bban)}{bban}"


EMPLOYER = ("Example Employer GmbH", _iban("370400440532013001"))
ENERGY = ("Example Energy AG", _iban("370400440532013002"))
TELECOM = ("Example Telecom", _iban("370400440532013003"))
FRIEND = ("Sam Example", _iban("370400440532013004"))


def _amount(value: Decimal) -> str:
    """Like the real export: no trailing zeros (`-3.4`, `-12`)."""
    return format(value.normalize(), "f")


def _add_months(start: date, months: int) -> date:
    y, m = divmod(start.month - 1 + months, 12)
    return date(start.year + y, m + 1, 1)


@dataclass
class SyntheticN26:
    start: date
    rows: list[dict[str, str]] = field(default_factory=list)
    # (debit row, credit row) indexes into `rows`, one pair per transfer between spaces.
    transfer_pairs: list[tuple[int, int]] = field(default_factory=list)

    def balances(self) -> dict[str, Decimal]:
        totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for row in self.rows:
            totals[row["Account Name"]] += Decimal(row["Amount (EUR)"])
        return dict(totals)

    def space_rows(self, space: str, months: int | None = None) -> list[dict[str, str]]:
        """One space's rows in file order (by booking date); `months` keeps the first N."""
        end = None if months is None else _add_months(self.start, months).isoformat()
        rows = [
            r
            for r in self.rows
            if r["Account Name"] == space and (end is None or r["Booking Date"] < end)
        ]
        return sorted(rows, key=lambda r: r["Booking Date"])

    def to_csv_bytes(self, space: str, months: int | None = None) -> bytes:
        return _csv(self.space_rows(space, months))

    def mixed_csv_bytes(self) -> bytes:
        """The main space's export with two rows of a deleted space appended, out of date
        order, as seen in a real export."""
        day = self.start.isoformat()
        blank = dict.fromkeys(HEADER, "")
        deleted_rows = [
            blank
            | {
                "Booking Date": day,
                "Value Date": day,
                "Partner Name": MAIN,
                "Type": "Credit Transfer",
                "Account Name": DELETED,
                "Amount (EUR)": "40",
            },
            blank
            | {
                "Booking Date": day,
                "Value Date": day,
                "Partner Name": SHOPS[1],
                "Type": "Presentment",
                "Account Name": DELETED,
                "Amount (EUR)": "-40",
                "Original Amount": "40",
                "Original Currency": "EUR",
                "Exchange Rate": "1",
            },
        ]
        return _csv(self.space_rows(MAIN) + deleted_rows)


def _csv(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


class _Generator:
    def __init__(self, seed: int, start: date) -> None:
        self.rng = random.Random(seed)
        self.export = SyntheticN26(start)
        self.savings = Decimal(0)

    def _emit(
        self,
        day: date,
        space: str,
        n26_type: str,
        amount: Decimal,
        *,
        partner: tuple[str, str] = ("", ""),
        reference: str = "",
        value_day: date | None = None,
        original: tuple[Decimal, str, Decimal] | None = None,
    ) -> int:
        original_amount, currency, rate = original or (None, "", None)
        self.export.rows.append(
            {
                "Booking Date": day.isoformat(),
                "Value Date": (value_day or day).isoformat(),
                "Partner Name": partner[0],
                "Partner Iban": partner[1],
                "Type": n26_type,
                "Payment Reference": reference,
                "Account Name": space,
                "Amount (EUR)": _amount(amount),
                "Original Amount": "" if original_amount is None else _amount(original_amount),
                "Original Currency": currency,
                "Exchange Rate": "" if rate is None else _amount(rate),
            }
        )
        return len(self.export.rows) - 1

    def card(self, day: date, space: str, shop: str, price: Decimal, currency: str = "EUR") -> None:
        rate = FX_RATES.get(currency, Decimal(1))
        eur = (price * rate).quantize(CENT, ROUND_HALF_UP)
        # Card rows are often booked a day after the purchase.
        value_day = day - timedelta(days=1) if self.rng.random() < 0.15 else day
        self._emit(
            day,
            space,
            "Presentment",
            -eur,
            partner=(shop, ""),
            value_day=value_day,
            original=(price, currency, rate),
        )

    def refund(self, day: date, shop: str, price: Decimal) -> None:
        self._emit(
            day,
            MAIN,
            "Presentment Refund",
            price,
            partner=(shop, ""),
            original=(price, "EUR", Decimal(1)),
        )

    def move(self, day: date, source: str, target: str, amount: Decimal, reference: str) -> None:
        debit = self._emit(
            day, source, "Debit Transfer", -amount, partner=(target, ""), reference=reference
        )
        credit = self._emit(
            day, target, "Credit Transfer", amount, partner=(source, ""), reference=reference
        )
        self.export.transfer_pairs.append((debit, credit))
        if target == SAVINGS:
            self.savings += amount

    def interest(self, day: date) -> None:
        interest = max(CENT, (self.savings * Decimal("0.02") / 12).quantize(CENT, ROUND_HALF_UP))
        tax = max(CENT, (interest * Decimal("0.26375")).quantize(CENT, ROUND_HALF_UP))
        self._emit(day, SAVINGS, "Interest", interest)
        self._emit(day, SAVINGS, "Tax", -tax)
        self.savings += interest - tax

    def price(self, low: int, high: int) -> Decimal:
        return Decimal(self.rng.randrange(low * 100, high * 100)) / 100


def generate(seed: int = 42, months: int = 14, start: date = date(2025, 1, 1)) -> SyntheticN26:
    g = _Generator(seed, start)
    rng = g.rng
    for i in range(months):
        first = _add_months(start, i)
        last = _add_months(start, i + 1) - timedelta(days=1)

        g._emit(first, MAIN, "Credit Transfer", Decimal(2500), partner=EMPLOYER, reference="Salary")
        g._emit(first + timedelta(days=2), MAIN, "Direct Debit", Decimal(-85), partner=ENERGY)
        g._emit(first + timedelta(days=4), MAIN, "Direct Debit", Decimal("-29.99"), partner=TELECOM)
        g.move(first + timedelta(days=1), MAIN, SHARED, Decimal(600), "Household")
        g._emit(
            first + timedelta(days=1),
            SHARED,
            "Credit Transfer",
            Decimal(400),
            partner=(OTHER_MEMBER_MAIN, ""),
            reference="Household",
        )
        g.move(first + timedelta(days=3), SHARED, SAVINGS, Decimal(rng.choice((100, 150, 200))), "")

        for _ in range(6):
            g.card(
                first + timedelta(days=rng.randrange(28)), MAIN, rng.choice(SHOPS), g.price(2, 60)
            )
        for _ in range(4):
            g.card(
                first + timedelta(days=rng.randrange(28)),
                SHARED,
                rng.choice(GROCERS),
                g.price(10, 90),
            )

        if i == 1:  # the same purchase twice on one day: identical rows, both real
            for _ in range(2):
                g._emit(
                    first + timedelta(days=9),
                    MAIN,
                    "Presentment",
                    Decimal("-3.4"),
                    partner=(SHOPS[0], ""),
                    original=(Decimal("3.4"), "EUR", Decimal(1)),
                )
        if i % 4 == 2:
            g.card(first + timedelta(days=12), MAIN, "Example Hygge Cafe", g.price(40, 300), "DKK")
        if i % 6 == 4:
            g.card(first + timedelta(days=15), MAIN, "Example Online Store", g.price(10, 80), "USD")
        if i == 3:
            g.refund(first + timedelta(days=20), SHOPS[2], Decimal("18.5"))
        if i % 3 == 0:
            g._emit(first + timedelta(days=18), MAIN, "Fee", Decimal("-2.5"), reference="ATM fee")
        g._emit(
            first + timedelta(days=rng.randrange(5, 25)),
            MAIN,
            "MoneyBeam",
            -g.price(5, 40),
            partner=FRIEND,
            reference="Dinner",
        )
        if i % 2 == 1:
            g._emit(
                first + timedelta(days=rng.randrange(5, 25)),
                MAIN,
                "MoneyBeam",
                g.price(5, 40),
                partner=FRIEND,
                reference="Tickets",
            )
        g.interest(last)
    return g.export
