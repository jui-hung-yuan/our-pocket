"""Transform: turn raw string rows into typed, validated rows with flags.

One Transformer subclass per (institution, file schema version). The subclass *is* the
schema definition: it declares the exact columns it reads, and the registry matches
files to it by header fingerprint. A new export format gets a new subclass with a
higher `schema_version`; `parser_version` changes whenever parsing logic changes.

Transformers are pure: no database, no I/O. That makes them cheap to test and lets
them run over a stored batch again after a bug fix.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from our_pocket.etl.extract.csv_extractor import header_fingerprint
from our_pocket.etl.models import ParsedRow, RawRow


class Transformer(ABC):
    institution: ClassVar[str]
    schema_name: ClassVar[str]
    schema_version: ClassVar[int]
    parser_version: ClassVar[str]
    columns: ClassVar[tuple[str, ...]]
    delimiter: ClassVar[str] = ","
    # Column naming the account each row belongs to (N26 spaces). Rows naming another
    # account than the one imported into are flagged. None: the export names no account.
    account_column: ClassVar[str | None] = None

    @property
    def schema_id(self) -> str:
        return f"{self.institution}.{self.schema_name}"

    @property
    def fingerprint(self) -> str:
        return header_fingerprint(self.columns, self.delimiter)

    def transform(self, rows: Sequence[RawRow]) -> list[ParsedRow]:
        parsed = [self.transform_row(raw) for raw in rows]
        self._flag_duplicate_ids(parsed)
        return parsed

    @abstractmethod
    def transform_row(self, raw: RawRow) -> ParsedRow:
        """Parse one row and attach a flag for everything that can't be trusted."""

    @staticmethod
    def _flag_duplicate_ids(parsed: list[ParsedRow]) -> None:
        first_line: dict[str, int] = {}
        for row in parsed:
            if row.external_id is None:
                continue
            if row.external_id in first_line:
                row.flag(
                    "duplicate_in_file",
                    "error",
                    f"transaction_id also on line {first_line[row.external_id]}",
                    "transaction_id",
                )
            else:
                first_line[row.external_id] = row.line_number

    # --- value parsing shared by all transformers ----------------------------------
    # Each returns None and flags the row when the value is present but unparseable.

    @staticmethod
    def text(data: dict[str, str], key: str) -> str | None:
        value = data.get(key, "").strip()
        return value or None

    @classmethod
    def decimal(cls, row: ParsedRow, data: dict[str, str], key: str) -> Decimal | None:
        value = cls.text(data, key)
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            parsed = None
        if parsed is None or not parsed.is_finite():
            row.flag("unparseable_value", "error", f"{key}={value!r} is not a number", key)
            return None
        return parsed

    @classmethod
    def timestamp(cls, row: ParsedRow, data: dict[str, str], key: str) -> datetime | None:
        value = cls.text(data, key)
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            row.flag("unparseable_value", "error", f"{key}={value!r} is not a timestamp", key)
            return None
        if parsed.tzinfo is None:
            row.flag("unparseable_value", "error", f"{key}={value!r} has no timezone", key)
            return None
        return parsed

    @classmethod
    def iso_date(cls, row: ParsedRow, data: dict[str, str], key: str) -> date | None:
        value = cls.text(data, key)
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            row.flag("unparseable_value", "error", f"{key}={value!r} is not a date", key)
            return None

    @classmethod
    def require(cls, row: ParsedRow, data: dict[str, str], keys: Sequence[str], why: str) -> None:
        for key in keys:
            if cls.text(data, key) is None:
                row.flag("missing_field", "error", f"{key} is {why}", key)
