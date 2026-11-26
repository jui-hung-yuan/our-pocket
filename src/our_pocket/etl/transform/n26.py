"""N26 transaction CSV export.

Conventions observed in real exports (see docs/decisions.md, D-019 to D-023):

- One file per space, imported into that space's account. `Account Name` says which space
  a row is from; a deleted space's rows can turn up in Main's export. The pipeline flags
  rows whose space isn't the account's (`account_column`).
- No transaction id, so `external_id` is a hash of the row's values except names the user
  can edit (the space name, and the partner name on moves between spaces), plus its
  occurrence number within the file: identical rows (two equal card payments on one day)
  are real and both count.
- Dates only, no time. `Amount (EUR)` is signed. The FX columns are filled only on card
  rows: `Original Amount` is unsigned and |amount| = original x rate.
- `Authorization` is a card hold that never became a `Presentment` (seen once, positive). Its
  sign gives the type, like MoneyBeam's, and it is cash: a reconciliation showed N26 counts
  it in the balance (D-030, D-034).
- A transfer without a partner IBAN is a move between N26 spaces, and its partner name is
  the other space's name. That space can belong to another member of a shared space, so
  the name doesn't say whose it is.
"""

import hashlib
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from our_pocket.etl.models import ParsedRow, RawRow
from our_pocket.etl.transform.base import Transformer
from our_pocket.etl.transform.validators import is_valid_iban

COLUMNS_V1: tuple[str, ...] = (
    "booking date",
    "value date",
    "partner name",
    "partner iban",
    "type",
    "payment reference",
    "account name",
    "amount (eur)",
    "original amount",
    "original currency",
    "exchange rate",
)

TYPE_MAP: dict[str, str] = {
    "Presentment": "card_payment",
    "Presentment Refund": "card_refund",
    "Credit Transfer": "transfer_in",
    "Debit Transfer": "transfer_out",
    "Direct Debit": "direct_debit",
    "Fee": "fee",
    "Interest": "interest",
    "Tax": "tax",
}
# MoneyBeam (N26's transfer to another N26 user) goes either way; its sign gives the type.
MONEYBEAM = "MoneyBeam"
# A card authorization: a hold (negative) or a released hold or refund (positive), by sign.
AUTHORIZATION = "Authorization"
SIGNED_TYPES: dict[str, tuple[str, str]] = {  # source type: (entry type if < 0, if > 0)
    MONEYBEAM: ("transfer_out", "transfer_in"),
    AUTHORIZATION: ("card_payment", "card_refund"),
}
# Transfers without a partner IBAN move money between N26 spaces.
SPACE_TRANSFER: dict[str, str] = {
    "transfer_in": "space_transfer_in",
    "transfer_out": "space_transfer_out",
}

EXPECTED_SIGN: dict[str, int] = {
    "card_payment": -1,
    "card_refund": 1,
    "transfer_in": 1,
    "transfer_out": -1,
    "space_transfer_in": 1,
    "space_transfer_out": -1,
    "direct_debit": -1,
    "fee": -1,
    "interest": 1,
    "tax": -1,
}

FX_COLUMNS = ("original amount", "original currency", "exchange rate")
# Ledger ids are unique per account already, and a space can be renamed in the app; on a
# move between spaces the partner name is a space name too.
HASHED_COLUMNS = tuple(c for c in COLUMNS_V1 if c != "account name")
SPACE_TRANSFER_HASHED_COLUMNS = tuple(c for c in HASHED_COLUMNS if c != "partner name")
REQUIRED = ("booking date", "type", "amount (eur)", "account name")
BERLIN = ZoneInfo("Europe/Berlin")
TOLERANCE = Decimal("0.01")


class N26V1(Transformer):
    institution = "n26"
    schema_name = "transactions_csv"
    schema_version = 1
    parser_version = "4"
    columns = COLUMNS_V1
    account_column = "account name"

    def transform(self, rows: Sequence[RawRow]) -> list[ParsedRow]:
        """Parse every row, then number identical rows so each gets its own id."""
        occurrences: Counter[str] = Counter()
        parsed = []
        for raw in rows:
            row = self.transform_row(raw)
            hashed = (
                SPACE_TRANSFER_HASHED_COLUMNS
                if row.entry_type in SPACE_TRANSFER.values()
                else HASHED_COLUMNS
            )
            digest = hashlib.sha256(
                "\x1f".join(raw.data.get(c, "") for c in hashed).encode()
            ).hexdigest()[:32]
            occurrences[digest] += 1
            row.external_id = f"n26:{digest}:{occurrences[digest]}"
            parsed.append(row)
        return parsed

    def transform_row(self, raw: RawRow) -> ParsedRow:
        d = raw.data
        row = ParsedRow(raw.line_number)
        self.require(row, d, REQUIRED, "empty")

        row.source_type = self.text(d, "type")
        row.counterparty_name = self.text(d, "partner name")
        row.counterparty_iban = self.text(d, "partner iban")
        row.description = self.text(d, "payment reference")
        row.currency = "EUR"
        row.original_currency = self.text(d, "original currency")
        row.fx_rate = self.decimal(row, d, "exchange rate")

        row.booking_date = self.iso_date(row, d, "booking date")
        if row.booking_date is not None:
            row.booked_at = datetime.combine(row.booking_date, time(), BERLIN)

        money = self.decimal(row, d, "amount (eur)")
        original = self.decimal(row, d, "original amount")
        if original is not None and money is not None and money < 0:
            original = -original
        row.original_amount = original

        row.entry_type = self._entry_type(row, money)
        self._set_money(row, money)
        self._check_sign(row, money)
        self._check_fx(row, d, money)
        if row.counterparty_iban is not None and not is_valid_iban(row.counterparty_iban):
            row.flag(
                "invalid_iban", "warning", "partner IBAN fails the mod-97 check", "partner iban"
            )
        return row

    @staticmethod
    def _entry_type(row: ParsedRow, money: Decimal | None) -> str | None:
        if row.source_type is None:
            return None
        if row.source_type in SIGNED_TYPES:
            if money is None:
                return None
            negative, positive = SIGNED_TYPES[row.source_type]
            return negative if money < 0 else positive
        entry_type = TYPE_MAP.get(row.source_type)
        if entry_type is None:
            row.flag("unknown_type", "error", f"unknown type {row.source_type!r}", "type")
        elif entry_type in SPACE_TRANSFER and row.counterparty_iban is None:
            return SPACE_TRANSFER[entry_type]
        return entry_type

    @staticmethod
    def _set_money(row: ParsedRow, money: Decimal | None) -> None:
        """Fees and taxes go in their own column, so cash_effect = amount + fee + tax holds
        for every institution and tax totals cover N26 too."""
        if money is None:
            return
        row.amount, row.fee, row.tax = money, Decimal(0), Decimal(0)
        if row.entry_type == "fee":
            row.amount, row.fee = Decimal(0), money
        elif row.entry_type == "tax":
            row.amount, row.tax = Decimal(0), money
        row.cash_effect = row.amount + row.fee + row.tax

    @staticmethod
    def _check_sign(row: ParsedRow, money: Decimal | None) -> None:
        expected = EXPECTED_SIGN.get(row.entry_type or "")
        if expected is None or money is None or row.source_type in SIGNED_TYPES or money == 0:
            return
        if (money > 0) != (expected > 0):
            row.flag(
                "unexpected_sign",
                "warning",
                f"amount={money} has the wrong sign for {row.source_type}",
                "amount (eur)",
            )

    def _check_fx(self, row: ParsedRow, data: dict[str, str], money: Decimal | None) -> None:
        filled = [self.text(data, column) is not None for column in FX_COLUMNS]
        if not any(filled):
            return
        if not all(filled):
            row.flag(
                "fx_incomplete",
                "error",
                "Original Amount, Original Currency and Exchange Rate must be all set or all empty",
                "exchange rate",
            )
            return
        if money is None or row.original_amount is None or row.fx_rate is None:
            return
        expected = abs(row.original_amount) * row.fx_rate
        if abs(abs(money) - expected) > TOLERANCE:
            row.flag(
                "fx_mismatch",
                "error",
                f"|Original Amount| x Exchange Rate = {expected:.2f}, |amount| = {abs(money)}",
                "amount (eur)",
            )
