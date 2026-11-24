"""Eval: planted defects, measured by unflagged errors and false flags.

Each defect corrupts one row of a clean synthetic export, and the whole file is read again.
The corrupted row must carry the expected flag (else it's an unflagged error) and no other
row may gain a flag (else it's a false flag). Targets (PROJECT_PLAN §6): 0 and 0.

BLIND_SPOTS lists defects the parser can't see by design, with what catches them instead.
They're asserted to pass unflagged, so a parser that learns to catch one fails here and
the case moves to the defect lists.
"""

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from our_pocket.etl.extract import CsvExtractor
from our_pocket.etl.models import ParsedRow
from our_pocket.etl.registry import default_registry
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic import trade_republic as synth_tr

Rows = list[dict[str, str]]
Mutation = dict[str, str] | Callable[[Rows, int], None]

TRANSFORM_SOURCES = Path(__file__).resolve().parents[1] / "src/our_pocket/etl/transform"
REGISTRY = default_registry()
CLEAN_TRADE_REPUBLIC = synth_tr.generate(seed=42)
CLEAN_N26 = synth_n26.generate(seed=42)
# Flags on clean data that aren't false flags: STOCKPERK's cash meaning is still open (Q1).
EXPECTED_ON_CLEAN = {("STOCKPERK", "warning", "cash_semantics_unverified")}


@dataclass(frozen=True)
class Defect:
    name: str
    target: str  # source type of the row to corrupt (its first row in the file)
    mutation: Mutation
    expected: tuple[str, str] | None  # (severity, code) on that row; None: a blind spot
    having: tuple[str, ...] = ()  # columns that must be filled on the chosen row


def negate(column: str) -> Mutation:
    def mutate(rows: Rows, index: int) -> None:
        rows[index][column] = str(-Decimal(rows[index][column]))

    return mutate


def break_check_digit(column: str, position: int) -> Mutation:
    def mutate(rows: Rows, index: int) -> None:
        value = rows[index][column]
        digit = str((int(value[position]) + 1) % 10)
        rows[index][column] = value[:position] + digit + value[position + 1 :]

    return mutate


def copy_from_previous_row(column: str) -> Mutation:
    def mutate(rows: Rows, index: int) -> None:
        rows[index][column] = rows[index - 1][column]

    return mutate


def scale(columns: tuple[str, ...], factor: str) -> Mutation:
    def mutate(rows: Rows, index: int) -> None:
        for column in columns:
            rows[index][column] = str(Decimal(rows[index][column]) * Decimal(factor))

    return mutate


TRADE_REPUBLIC_DEFECTS = [
    Defect("unknown type", "BUY", {"type": "CRYPTO_SWAP"}, ("error", "unknown_type")),
    Defect("empty type", "BUY", {"type": ""}, ("error", "missing_field")),
    Defect("buy amount off", "BUY", {"amount": "-999.00"}, ("error", "trade_arithmetic")),
    Defect("sell amount off", "SELL", {"amount": "1.00"}, ("error", "trade_arithmetic")),
    Defect("buy shares negative", "BUY", negate("shares"), ("error", "unexpected_sign")),
    Defect("sell shares positive", "SELL", negate("shares"), ("error", "unexpected_sign")),
    Defect("sell amount negative", "SELL", negate("amount"), ("error", "unexpected_sign")),
    Defect("fee positive", "BUY", {"fee": "1.00"}, ("warning", "unexpected_sign"), ("fee",)),
    Defect("deposit negative", "CUSTOMER_INBOUND", negate("amount"), ("error", "unexpected_sign")),
    Defect(
        "withdrawal positive",
        "CUSTOMER_OUTBOUND_REQUEST",
        negate("amount"),
        ("error", "unexpected_sign"),
    ),
    Defect("dividend negative", "DIVIDEND", negate("amount"), ("error", "unexpected_sign")),
    Defect("distribution negative", "DISTRIBUTION", negate("amount"), ("error", "unexpected_sign")),
    Defect("interest negative", "INTEREST_PAYMENT", negate("amount"), ("error", "unexpected_sign")),
    Defect("price empty", "BUY", {"price": ""}, ("error", "missing_field")),
    Defect("dividend without ISIN", "DIVIDEND", {"symbol": ""}, ("error", "missing_field")),
    Defect("transaction id empty", "BUY", {"transaction_id": ""}, ("error", "missing_field")),
    Defect("currency empty", "BUY", {"currency": ""}, ("error", "missing_field")),
    Defect("decimal comma", "BUY", {"amount": "1.234,56"}, ("error", "unparseable_value")),
    Defect("amount NaN", "BUY", {"amount": "NaN"}, ("error", "unparseable_value")),
    Defect("shares not a number", "BUY", {"shares": "abc"}, ("error", "unparseable_value")),
    Defect(
        "timestamp without zone",
        "BUY",
        {"datetime": "2025-01-02 08:00:00"},
        ("error", "unparseable_value"),
    ),
    Defect("German date", "BUY", {"date": "02.01.2025"}, ("error", "unparseable_value")),
    Defect("currency not ISO", "BUY", {"currency": "euro"}, ("error", "invalid_currency")),
    Defect("ISIN check digit", "BUY", break_check_digit("symbol", -1), ("error", "invalid_isin")),
    Defect(
        "IBAN check digits",
        "CUSTOMER_INBOUND",
        break_check_digit("counterparty_iban", 3),
        ("warning", "invalid_iban"),
    ),
    Defect(
        "unknown account type",
        "BUY",
        {"account_type": "JUNIOR"},
        ("warning", "unexpected_account_type"),
    ),
    Defect(
        "FX rate missing",
        "DIVIDEND",
        {"fx_rate": ""},
        ("error", "fx_incomplete"),
        ("fx_rate",),
    ),
    Defect(
        "FX currency missing",
        "DIVIDEND",
        {"original_currency": ""},
        ("error", "fx_incomplete"),
        ("fx_rate",),
    ),
    Defect("FX rate off", "DIVIDEND", {"fx_rate": "1.5"}, ("warning", "fx_mismatch"), ("fx_rate",)),
    Defect(
        "tax refund with amount",
        "TAX_OPTIMIZATION",
        {"amount": "12.50"},
        ("warning", "tax_row_has_amount"),
    ),
    Defect(
        "Vorabpauschale with amount",
        "EARNINGS",
        {"amount": "3.00"},
        ("warning", "tax_row_has_amount"),
    ),
    Defect(
        "duplicate transaction id",
        "BUY",
        copy_from_previous_row("transaction_id"),
        ("error", "duplicate_in_file"),
    ),
]

N26_DEFECTS = [
    Defect("unknown type", "Presentment", {"Type": "Chargeback"}, ("error", "unknown_type")),
    Defect("empty type", "Presentment", {"Type": ""}, ("error", "missing_field")),
    Defect("booking date empty", "Presentment", {"Booking Date": ""}, ("error", "missing_field")),
    Defect("amount empty", "Presentment", {"Amount (EUR)": ""}, ("error", "missing_field")),
    Defect("space name empty", "Presentment", {"Account Name": ""}, ("error", "missing_field")),
    Defect(
        "German date",
        "Presentment",
        {"Booking Date": "04.03.2025"},
        ("error", "unparseable_value"),
    ),
    Defect(
        "decimal comma", "Direct Debit", {"Amount (EUR)": "-85,00"}, ("error", "unparseable_value")
    ),
    Defect(
        "rate not a number",
        "Presentment",
        {"Exchange Rate": "abc"},
        ("error", "unparseable_value"),
    ),
    Defect("FX rate missing", "Presentment", {"Exchange Rate": ""}, ("error", "fx_incomplete")),
    Defect(
        "FX currency missing", "Presentment", {"Original Currency": ""}, ("error", "fx_incomplete")
    ),
    Defect("FX amount off", "Presentment", {"Original Amount": "200"}, ("error", "fx_mismatch")),
    Defect(
        "IBAN check digits",
        "Direct Debit",
        break_check_digit("Partner Iban", 3),
        ("warning", "invalid_iban"),
    ),
    Defect(
        "card payment positive",
        "Presentment",
        negate("Amount (EUR)"),
        ("warning", "unexpected_sign"),
    ),
    Defect(
        "refund negative",
        "Presentment Refund",
        negate("Amount (EUR)"),
        ("warning", "unexpected_sign"),
    ),
    Defect(
        "direct debit positive",
        "Direct Debit",
        negate("Amount (EUR)"),
        ("warning", "unexpected_sign"),
    ),
    Defect(
        "space move out positive",
        "Debit Transfer",
        negate("Amount (EUR)"),
        ("warning", "unexpected_sign"),
    ),
    Defect("fee positive", "Fee", negate("Amount (EUR)"), ("warning", "unexpected_sign")),
    Defect("interest negative", "Interest", negate("Amount (EUR)"), ("warning", "unexpected_sign")),
    Defect("tax positive", "Tax", negate("Amount (EUR)"), ("warning", "unexpected_sign")),
]

BLIND_SPOTS = {
    "trade_republic": [
        # Nothing in a deposit row says how much it should be.
        Defect("deposit amount changed [cash reconciliation]", "CUSTOMER_INBOUND",
               {"amount": "1001"}, None),
        # Shares and amount scaled together still satisfy amount = shares x price.
        Defect("buy shares and amount scaled [position reconciliation]", "BUY",
               scale(("shares", "amount"), "2"), None),
        # `date` isn't compared with `datetime`; they may differ by one day (late trades).
        Defect("booking date moved a week [no check yet]", "BUY", {"date": "2025-01-09"}, None),
    ],
    "n26": [
        # A card amount changed together with its original amount is self-consistent.
        Defect("card amount and original changed [balance reconciliation]", "Presentment",
               scale(("Amount (EUR)", "Original Amount"), "2"), None),
        # `Value Date` isn't parsed.
        Defect("value date garbage [not read]", "Presentment", {"Value Date": "soon"}, None),
    ],
}  # fmt: skip


# --- planting --------------------------------------------------------------------------


def _first(rows: Rows, type_column: str, defect: Defect) -> int:
    for index, row in enumerate(rows):
        if row[type_column] == defect.target and all(row[c] for c in defect.having):
            return index
    raise LookupError(defect.target)


def _mutate(rows: Rows, index: int, mutation: Mutation) -> None:
    if isinstance(mutation, dict):
        rows[index].update(mutation)
    else:
        mutation(rows, index)


def _read(content: bytes) -> list[ParsedRow]:
    raw_file = CsvExtractor().extract(content, "planted.csv")
    transformer = REGISTRY.match(raw_file.signature)
    assert transformer is not None
    return transformer.transform(raw_file.rows)


def plant_trade_republic(defect: Defect) -> tuple[list[ParsedRow], int]:
    rows = [dict(r) for r in CLEAN_TRADE_REPUBLIC.rows]
    index = _first(rows, "type", defect)
    _mutate(rows, index, defect.mutation)
    return _read(synth_tr.SyntheticExport(rows=rows, holdings={}).to_csv_bytes()), index


def plant_n26(defect: Defect) -> tuple[list[ParsedRow], int]:
    """Planted in the first space whose export has a row of the target type."""
    for space in synth_n26.SPACES:
        rows = [dict(r) for r in CLEAN_N26.space_rows(space)]
        try:
            index = _first(rows, "Type", defect)
        except LookupError:
            continue
        _mutate(rows, index, defect.mutation)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=synth_n26.HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return _read(buffer.getvalue().encode()), index
    raise LookupError(defect.target)


PLANT = {"trade_republic": plant_trade_republic, "n26": plant_n26}


def _flags(row: ParsedRow) -> set[tuple[str, str]]:
    return {(f.severity, f.code) for f in row.flags}


def _false_flags(parsed: list[ParsedRow], planted: int) -> list[tuple[int, str]]:
    return [
        (row.line_number, flag.code)
        for i, row in enumerate(parsed)
        if i != planted
        for flag in row.flags
        if (row.source_type, flag.severity, flag.code) not in EXPECTED_ON_CLEAN
    ]


def _cases(institution: str, defects: list[Defect]) -> list[object]:
    return [pytest.param(institution, d, id=f"{institution}: {d.name}") for d in defects]


# --- the suite -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "institution, defect",
    _cases("trade_republic", TRADE_REPUBLIC_DEFECTS) + _cases("n26", N26_DEFECTS),
)
def test_planted_defect_is_flagged_and_nothing_else(institution, defect):
    parsed, planted = PLANT[institution](defect)
    assert defect.expected in _flags(parsed[planted]), f"unflagged error: {parsed[planted].flags}"
    assert _false_flags(parsed, planted) == []


@pytest.mark.parametrize(
    "institution, defect",
    [case for institution, spots in BLIND_SPOTS.items() for case in _cases(institution, spots)],
)
def test_blind_spot_passes_unflagged(institution, defect):
    parsed, planted = PLANT[institution](defect)
    assert parsed[planted].flags == [], "the parser catches this now: move it to the defects"
    assert _false_flags(parsed, planted) == []


def _codes_in(path: Path) -> set[str]:
    return set(re.findall(r'\.flag\(\s*"(\w+)"', path.read_text()))


def test_every_row_flag_the_parsers_can_raise_has_a_planted_defect():
    """A new check without a planted defect fails here. Shared checks (base.py) need a
    defect in either institution."""
    shared = _codes_in(TRANSFORM_SOURCES / "base.py")
    planted = {
        "trade_republic": {d.expected[1] for d in TRADE_REPUBLIC_DEFECTS if d.expected},
        "n26": {d.expected[1] for d in N26_DEFECTS if d.expected},
    }
    raised_on_clean = {code for _, _, code in EXPECTED_ON_CLEAN}
    for institution, codes in planted.items():
        own = _codes_in(TRANSFORM_SOURCES / f"{institution}.py") - raised_on_clean
        assert own - codes == set(), institution
    assert shared - set().union(*planted.values()) == set()
    assert len(TRADE_REPUBLIC_DEFECTS) + len(N26_DEFECTS) >= 30
