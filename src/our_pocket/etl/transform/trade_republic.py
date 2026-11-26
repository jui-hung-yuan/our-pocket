"""Trade Republic transaction CSV export.

Conventions observed in real exports (see docs/decisions.md, D-007):

- One row per event. `amount` is the gross value in `currency` (EUR); `fee` and `tax`
  are separate signed columns, negative when they cost money.
- BUY: shares > 0, amount = -(shares * price). SELL: shares < 0, amount = |shares| * price.
- Foreign-currency dividends: amount = original_amount * fx_rate.
- `datetime` is UTC; `date` is the booking date. It is usually the Berlin date of
  `datetime`, but can be a few days earlier, never later (D-028).
- Tax-only rows (TAX_OPTIMIZATION, EARNINGS, PRE_DETERMINED_TAX_BASE) have amount 0 and
  carry the money in `tax` (D-016).
- STOCKPERK credits its amount as cash (D-035).
"""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from our_pocket.etl.models import ParsedRow, RawRow
from our_pocket.etl.transform.base import Transformer
from our_pocket.etl.transform.validators import is_valid_iban, is_valid_isin

COLUMNS_V1: tuple[str, ...] = (
    "datetime",
    "date",
    "account_type",
    "category",
    "type",
    "asset_class",
    "name",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
    "original_amount",
    "original_currency",
    "fx_rate",
    "description",
    "transaction_id",
    "counterparty_name",
    "counterparty_iban",
    "payment_reference",
    "mcc_code",
)

TYPE_MAP: dict[str, str] = {
    "BUY": "buy",
    "SELL": "sell",
    "DIVIDEND": "dividend",
    "DISTRIBUTION": "dividend",
    "INTEREST_PAYMENT": "interest",
    "CUSTOMER_INBOUND": "deposit",
    "TRANSFER_INBOUND": "deposit",
    "CUSTOMER_OUTBOUND_REQUEST": "withdrawal",
    "TAX_OPTIMIZATION": "tax_adjustment",
    "EARNINGS": "tax_base",
    "PRE_DETERMINED_TAX_BASE": "tax_base",
    "STOCKPERK": "bonus",
}

# Rows where `amount` is 0 and the money is in `tax` (D-016): TAX_OPTIMIZATION refunds
# tax withheld earlier once losses offset gains (tax > 0); EARNINGS and
# PRE_DETERMINED_TAX_BASE debit the Vorabpauschale tax (tax < 0).
TAX_ONLY_TYPES = frozenset({"TAX_OPTIMIZATION", "EARNINGS", "PRE_DETERMINED_TAX_BASE"})

# Types whose `amount` may not be cash that moved. Their cash_effect stays NULL until
# verified against an account balance. None left: STOCKPERK was, until it proved cash (D-035).
UNVERIFIED_CASH_TYPES: frozenset[str] = frozenset()

KNOWN_ACCOUNT_TYPES = frozenset({"DEFAULT"})

# (amount sign, quantity sign) expected per entry type; None = not checked.
EXPECTED_SIGNS: dict[str, tuple[int | None, int | None]] = {
    "buy": (-1, 1),
    "sell": (1, -1),
    "dividend": (1, None),
    "interest": (1, None),
    "deposit": (1, None),
    "withdrawal": (-1, None),
}

REQUIRED_ALWAYS = ("transaction_id", "datetime", "date", "type", "amount", "currency")
REQUIRED_BY_ENTRY_TYPE = {
    "buy": ("symbol", "shares", "price"),
    "sell": ("symbol", "shares", "price"),
    "dividend": ("symbol",),
}

TOLERANCE = Decimal("0.01")

BERLIN = ZoneInfo("Europe/Berlin")
# How far `date` may fall before the Berlin date of `datetime`. Real exports show up to 2
# days; 4 covers a long weekend (D-028).
MAX_BOOKING_LAG = timedelta(days=4)


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


class TradeRepublicV1(Transformer):
    institution = "trade_republic"
    schema_name = "transactions_csv"
    schema_version = 1
    parser_version = "4"
    columns = COLUMNS_V1

    def transform_row(self, raw: RawRow) -> ParsedRow:
        d = raw.data
        row = ParsedRow(raw.line_number)
        self.require(row, d, REQUIRED_ALWAYS, "empty")

        row.external_id = self.text(d, "transaction_id")
        row.source_type = self.text(d, "type")
        row.isin = self.text(d, "symbol")
        row.instrument_name = self.text(d, "name")
        row.asset_class = self.text(d, "asset_class")
        row.currency = self.text(d, "currency")
        row.original_currency = self.text(d, "original_currency")
        row.description = self.text(d, "description")
        row.counterparty_name = self.text(d, "counterparty_name")
        row.counterparty_iban = self.text(d, "counterparty_iban")

        row.booked_at = self.timestamp(row, d, "datetime")
        row.booking_date = self.iso_date(row, d, "date")
        row.quantity = self.decimal(row, d, "shares")
        row.price = self.decimal(row, d, "price")
        row.amount = self.decimal(row, d, "amount")
        row.fee = self.decimal(row, d, "fee") or Decimal(0)
        row.tax = self.decimal(row, d, "tax") or Decimal(0)
        row.original_amount = self.decimal(row, d, "original_amount")
        row.fx_rate = self.decimal(row, d, "fx_rate")

        account_type = self.text(d, "account_type")
        if account_type not in KNOWN_ACCOUNT_TYPES:
            row.flag(
                "unexpected_account_type",
                "warning",
                f"account_type {account_type!r} not seen before; is this a sub-account?",
                "account_type",
            )

        if row.source_type is not None:
            row.entry_type = TYPE_MAP.get(row.source_type)
            if row.entry_type is None:
                row.flag("unknown_type", "error", f"unknown type {row.source_type!r}", "type")
            else:
                required = REQUIRED_BY_ENTRY_TYPE.get(row.entry_type, ())
                self.require(row, d, required, f"required for {row.source_type}")

        self._check_booking_date(row)
        self._check_signs(row)
        self._check_trade_arithmetic(row)
        self._check_fx(row)
        self._check_identifiers(row)
        self._set_cash_effect(row)
        return row

    # --- checks -------------------------------------------------------------------

    @staticmethod
    def _check_booking_date(row: ParsedRow) -> None:
        if row.booked_at is None or row.booking_date is None:
            return
        local_date = row.booked_at.astimezone(BERLIN).date()
        lag = local_date - row.booking_date
        if timedelta(0) <= lag <= MAX_BOOKING_LAG:
            return
        when = "after" if lag < timedelta(0) else f"{lag.days} days before"
        row.flag(
            "booking_date_mismatch",
            "warning",
            f"date {row.booking_date} is {when} the Berlin date of datetime ({local_date})",
            "date",
        )

    @staticmethod
    def _check_signs(row: ParsedRow) -> None:
        amount_sign, quantity_sign = EXPECTED_SIGNS.get(row.entry_type or "", (None, None))
        checks = (("amount", row.amount, amount_sign), ("shares", row.quantity, quantity_sign))
        for name, value, expected in checks:
            if expected is not None and value is not None and _sign(value) != expected:
                row.flag(
                    "unexpected_sign",
                    "error",
                    f"{name}={value} has the wrong sign for {row.source_type}",
                    name,
                )
        if row.fee is not None and row.fee > 0:
            row.flag("unexpected_sign", "warning", f"fee={row.fee} is positive (refund?)", "fee")

    @staticmethod
    def _check_trade_arithmetic(row: ParsedRow) -> None:
        if row.entry_type not in ("buy", "sell"):
            return
        if row.quantity is None or row.price is None or row.amount is None:
            return
        expected = abs(row.quantity) * row.price
        if abs(abs(row.amount) - expected) > TOLERANCE:
            row.flag(
                "trade_arithmetic",
                "error",
                f"|amount| {abs(row.amount)} != |shares| x price {expected:.2f}",
                "amount",
            )

    @staticmethod
    def _check_fx(row: ParsedRow) -> None:
        present = [v is not None for v in (row.original_amount, row.original_currency, row.fx_rate)]
        if not any(present):
            return
        if not all(present):
            row.flag(
                "fx_incomplete",
                "error",
                "original_amount, original_currency and fx_rate must be all set or all empty",
                "fx_rate",
            )
            return
        if row.amount is None:
            return
        assert row.original_amount is not None and row.fx_rate is not None
        expected = row.original_amount * row.fx_rate
        if abs(expected - row.amount) > TOLERANCE:
            row.flag(
                "fx_mismatch",
                "warning",
                f"original_amount x fx_rate = {expected:.2f}, amount = {row.amount}",
                "amount",
            )

    @staticmethod
    def _check_identifiers(row: ParsedRow) -> None:
        if row.isin is not None and not is_valid_isin(row.isin):
            row.flag("invalid_isin", "error", f"{row.isin!r} fails the ISIN check digit", "symbol")
        if row.counterparty_iban is not None and not is_valid_iban(row.counterparty_iban):
            row.flag(
                "invalid_iban",
                "warning",
                "counterparty IBAN fails the mod-97 check",
                "counterparty_iban",
            )
        if row.currency is not None and not (len(row.currency) == 3 and row.currency.isupper()):
            row.flag("invalid_currency", "error", f"{row.currency!r} is not ISO 4217", "currency")

    @staticmethod
    def _set_cash_effect(row: ParsedRow) -> None:
        if row.source_type in UNVERIFIED_CASH_TYPES:
            row.flag(
                "cash_semantics_unverified",
                "warning",
                f"cash meaning of {row.source_type} is unverified; cash_effect left empty",
                "amount",
            )
            return
        if row.source_type in TAX_ONLY_TYPES and row.amount:
            row.flag(
                "tax_row_has_amount",
                "warning",
                f"{row.source_type} normally has amount 0 and the money in tax; "
                f"amount={row.amount}, so cash_effect left empty",
                "amount",
            )
            return
        if row.amount is not None and row.fee is not None and row.tax is not None:
            row.cash_effect = row.amount + row.fee + row.tax
