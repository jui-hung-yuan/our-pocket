"""Data passed between the ETL stages."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Account:
    """The account a file is loaded into."""

    id: int
    name: str
    institution: str
    household_id: int


@dataclass(frozen=True)
class Flag:
    """Something a human should know about before a row reaches the ledger.

    error   blocks committing the batch
    warning blocks commit unless explicitly accepted
    info    recorded for the audit trail only
    """

    code: str
    severity: Severity
    message: str
    field: str | None = None


@dataclass(frozen=True)
class RawRow:
    """One data line exactly as extracted: column name → string."""

    line_number: int
    data: dict[str, str]


@dataclass
class ParsedRow:
    """A transformer's typed reading of one raw row. Fields are None when unparseable."""

    line_number: int
    external_id: str | None = None
    booked_at: datetime | None = None
    booking_date: date | None = None
    entry_type: str | None = None
    source_type: str | None = None
    isin: str | None = None
    instrument_name: str | None = None
    asset_class: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    fee: Decimal | None = None
    tax: Decimal | None = None
    cash_effect: Decimal | None = None
    currency: str | None = None
    original_amount: Decimal | None = None
    original_currency: str | None = None
    fx_rate: Decimal | None = None
    description: str | None = None
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    flags: list[Flag] = field(default_factory=list)

    def flag(self, code: str, severity: Severity, message: str, field: str | None = None) -> None:
        self.flags.append(Flag(code, severity, message, field))


# The typed fields shared by ParsedRow, staged_row and ledger_entry.
ENTRY_FIELDS: tuple[str, ...] = tuple(
    name for name in ParsedRow.__dataclass_fields__ if name not in ("line_number", "flags")
)
