from collections import Counter
from decimal import Decimal

import pytest

from our_pocket.etl.models import RawRow
from our_pocket.etl.transform.trade_republic import (
    TYPE_MAP,
    UNVERIFIED_CASH_TYPES,
    TradeRepublicV1,
)

PARSER = TradeRepublicV1()


def _raw(synthetic) -> list[RawRow]:
    return [RawRow(i + 2, dict(r)) for i, r in enumerate(synthetic.rows)]


def _first(synthetic, tr_type: str, **having) -> dict[str, str]:
    for row in synthetic.rows:
        if row["type"] == tr_type and all(bool(row[k]) == v for k, v in having.items()):
            return dict(row)
    raise LookupError(tr_type)


def _codes(row) -> set[tuple[str, str]]:
    return {(f.severity, f.code) for f in row.flags}


def test_synthetic_export_has_no_errors(synthetic):
    parsed = PARSER.transform(_raw(synthetic))
    flags = Counter((f.severity, f.code) for p in parsed for f in p.flags)
    unverified = sum(synthetic.type_counts[t] for t in UNVERIFIED_CASH_TYPES)
    assert flags == Counter({("warning", "cash_semantics_unverified"): unverified})


def test_every_row_gets_a_normalised_type(synthetic):
    parsed = PARSER.transform(_raw(synthetic))
    assert all(p.entry_type == TYPE_MAP[p.source_type] for p in parsed)


def test_buy_with_fee(synthetic):
    row = PARSER.transform([RawRow(2, _first(synthetic, "BUY", fee=True))])[0]
    assert row.entry_type == "buy"
    assert row.quantity > 0 and row.amount < 0
    assert row.fee == Decimal("-1.00")
    assert row.cash_effect == row.amount + row.fee
    assert row.flags == []


def test_foreign_dividend_keeps_original_currency(synthetic):
    row = PARSER.transform([RawRow(2, _first(synthetic, "DIVIDEND", fx_rate=True))])[0]
    assert row.original_currency == "USD"
    assert abs(row.original_amount * row.fx_rate - row.amount) <= Decimal("0.01")
    assert row.tax < 0
    assert row.cash_effect == row.amount + row.tax


def test_booking_date_is_the_local_date_not_the_utc_date(synthetic):
    parsed = PARSER.transform(_raw(synthetic))
    shifted = [p for p in parsed if p.booking_date != p.booked_at.date()]
    assert shifted, "generator should produce late-evening trades booked on the next local day"
    assert all(p.booking_date > p.booked_at.date() for p in shifted)


@pytest.mark.parametrize(
    "tr_type, entry_type, tax_sign",
    [("TAX_OPTIMIZATION", "tax_adjustment", 1), ("EARNINGS", "tax_base", -1)],
)
def test_tax_only_rows_move_cash_through_tax(synthetic, tr_type, entry_type, tax_sign):
    row = PARSER.transform([RawRow(2, _first(synthetic, tr_type))])[0]
    assert row.entry_type == entry_type
    assert row.amount == 0
    assert (row.tax > 0) - (row.tax < 0) == tax_sign
    assert row.cash_effect == row.tax
    assert row.flags == []


def test_unverified_types_have_no_cash_effect(synthetic):
    row = PARSER.transform([RawRow(2, _first(synthetic, "STOCKPERK"))])[0]
    assert row.entry_type == "bonus"
    assert row.cash_effect is None
    assert _codes(row) == {("warning", "cash_semantics_unverified")}


# (source type, mutation, expected (severity, code))
PERTURBATIONS = [
    ("BUY", {"type": "CRYPTO_SWAP"}, ("error", "unknown_type")),
    ("BUY", {"amount": "-999.00"}, ("error", "trade_arithmetic")),
    ("BUY", {"symbol": "IE00B4L5Y984"}, ("error", "invalid_isin")),
    ("BUY", {"shares": "-1.5", "amount": "-142.50", "price": "95"}, ("error", "unexpected_sign")),
    ("BUY", {"fee": "1.00"}, ("warning", "unexpected_sign")),
    ("BUY", {"price": ""}, ("error", "missing_field")),
    ("BUY", {"amount": "1.234,56"}, ("error", "unparseable_value")),
    ("BUY", {"datetime": "2025-01-02 08:00:00"}, ("error", "unparseable_value")),
    ("BUY", {"date": "02.01.2025"}, ("error", "unparseable_value")),
    ("BUY", {"transaction_id": ""}, ("error", "missing_field")),
    ("BUY", {"account_type": "JUNIOR"}, ("warning", "unexpected_account_type")),
    ("BUY", {"currency": "euro"}, ("error", "invalid_currency")),
    ("DIVIDEND", {"fx_rate": ""}, ("error", "fx_incomplete")),
    ("DIVIDEND", {"fx_rate": "1.5"}, ("warning", "fx_mismatch")),
    ("CUSTOMER_INBOUND", {"amount": "-500"}, ("error", "unexpected_sign")),
    ("TAX_OPTIMIZATION", {"amount": "12.50"}, ("warning", "tax_row_has_amount")),
    (
        "CUSTOMER_INBOUND",
        {"counterparty_iban": "DE00370400440532013000"},
        ("warning", "invalid_iban"),
    ),
]


@pytest.mark.parametrize("tr_type, mutation, expected", PERTURBATIONS)
def test_perturbed_rows_are_flagged(synthetic, tr_type, mutation, expected):
    having = {"fx_rate": True} if tr_type == "DIVIDEND" else {}
    row = {**_first(synthetic, tr_type, **having), **mutation}
    parsed = PARSER.transform([RawRow(2, row)])[0]
    assert expected in _codes(parsed)


def test_duplicate_transaction_id_within_file(synthetic):
    rows = _raw(synthetic)[:3]
    rows.append(RawRow(99, dict(rows[1].data)))
    parsed = PARSER.transform(rows)
    assert ("error", "duplicate_in_file") in _codes(parsed[-1])
    assert "line 3" in parsed[-1].flags[0].message
    assert _codes(parsed[1]) == set()
