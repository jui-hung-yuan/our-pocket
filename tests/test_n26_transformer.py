from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from our_pocket.etl.extract import CsvExtractor
from our_pocket.etl.models import RawRow
from our_pocket.etl.transform.n26 import N26V1
from our_pocket.synthetic import n26 as synth_n26

PARSER = N26V1()


def _row(line: int = 2, **overrides: str) -> RawRow:
    """A valid card payment in EUR; keys are the normalised column names."""
    data = {
        "booking date": "2025-03-04",
        "value date": "2025-03-04",
        "partner name": "Example Bakery",
        "partner iban": "",
        "type": "Presentment",
        "payment reference": "",
        "account name": "Alex Example",
        "amount (eur)": "-3.40",
        "original amount": "3.40",
        "original currency": "EUR",
        "exchange rate": "1",
    }
    return RawRow(line, data | overrides)


def _parse(raw: RawRow):
    return PARSER.transform([raw])[0]


def _codes(row) -> set[tuple[str, str]]:
    return {(f.severity, f.code) for f in row.flags}


def test_card_payment_in_foreign_currency():
    row = _parse(
        _row(
            **{
                "amount (eur)": "-13.38",
                "original amount": "100",
                "original currency": "DKK",
                "exchange rate": "0.1338",
            }
        )
    )
    assert (row.entry_type, row.amount, row.original_amount) == (
        "card_payment",
        Decimal("-13.38"),
        Decimal("-100"),
    )
    assert (row.original_currency, row.fx_rate, row.currency) == ("DKK", Decimal("0.1338"), "EUR")
    assert row.booked_at == datetime(2025, 3, 4, tzinfo=ZoneInfo("Europe/Berlin"))
    assert row.cash_effect == row.amount
    assert (row.fee, row.tax) == (0, 0)
    assert row.flags == []


@pytest.mark.parametrize(
    ("n26_type", "amount", "column"), [("Fee", "-2.50", "fee"), ("Tax", "-0.30", "tax")]
)
def test_fee_and_tax_rows_carry_the_money_in_their_column(n26_type, amount, column):
    no_fx = {"original amount": "", "original currency": "", "exchange rate": ""}
    row = _parse(_row(**{"type": n26_type, "amount (eur)": amount, "partner name": ""} | no_fx))
    assert row.entry_type == n26_type.lower()
    assert row.amount == 0
    assert getattr(row, column) == Decimal(amount)
    assert row.cash_effect == Decimal(amount)
    assert row.flags == []


@pytest.mark.parametrize(("amount", "entry_type"), [("25", "transfer_in"), ("-25", "transfer_out")])
def test_moneybeam_direction_follows_the_sign(amount, entry_type):
    no_fx = {"original amount": "", "original currency": "", "exchange rate": ""}
    row = _parse(_row(**{"type": "MoneyBeam", "amount (eur)": amount} | no_fx))
    assert row.entry_type == entry_type
    assert row.flags == []


def test_text_fields_map_to_counterparty_and_description():
    row = _parse(
        _row(
            **{
                "type": "Direct Debit",
                "partner iban": "DE89370400440532013000",
                "payment reference": "Contract 42",
                "original amount": "",
                "original currency": "",
                "exchange rate": "",
            }
        )
    )
    assert (row.counterparty_name, row.counterparty_iban, row.description) == (
        "Example Bakery",
        "DE89370400440532013000",
        "Contract 42",
    )
    assert (row.entry_type, row.source_type) == ("direct_debit", "Direct Debit")


def test_identical_rows_get_occurrence_numbers():
    first, second = PARSER.transform([_row(line=2), _row(line=3)])
    assert first.external_id.startswith("n26:")
    assert first.external_id.endswith(":1") and second.external_id.endswith(":2")
    assert first.external_id[:-2] == second.external_id[:-2]
    assert first.flags == second.flags == []


def test_external_id_ignores_line_number():
    assert _parse(_row(line=2)).external_id == _parse(_row(line=40)).external_id


def test_external_id_survives_a_space_rename():
    assert _parse(_row()).external_id == _parse(_row(**{"account name": "Renamed"})).external_id


def test_occurrences_are_numbered_per_space():
    main, shared = PARSER.transform([_row(line=2), _row(line=3, **{"account name": "Shared"})])
    assert main.external_id == shared.external_id
    assert main.external_id.endswith(":1")


def test_external_id_changes_with_any_value():
    assert _parse(_row()).external_id != _parse(_row(**{"value date": "2025-03-03"})).external_id


def test_zero_amount_has_no_sign_flag():
    row = _parse(_row(**{"amount (eur)": "0", "original amount": "0"}))
    assert ("warning", "unexpected_sign") not in _codes(row)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"type": "Chargeback"}, ("error", "unknown_type")),
        ({"amount (eur)": "5", "original amount": "5"}, ("warning", "unexpected_sign")),
        ({"booking date": ""}, ("error", "missing_field")),
        ({"type": ""}, ("error", "missing_field")),
        ({"amount (eur)": ""}, ("error", "missing_field")),
        ({"account name": ""}, ("error", "missing_field")),
        ({"booking date": "04.03.2025"}, ("error", "unparseable_value")),
        ({"amount (eur)": "1,5"}, ("error", "unparseable_value")),
        ({"exchange rate": ""}, ("error", "fx_incomplete")),
        ({"original amount": "200"}, ("error", "fx_mismatch")),
        ({"partner iban": "DE00370400440532013000"}, ("warning", "invalid_iban")),
    ],
)
def test_planted_defects_are_flagged(overrides, expected):
    assert expected in _codes(_parse(_row(**overrides)))


@pytest.mark.parametrize("space", synth_n26.SPACES)
def test_synthetic_n26_has_no_flags(synthetic_n26, space):
    raw_file = CsvExtractor().extract(synthetic_n26.to_csv_bytes(space), f"{space}.csv")
    assert [f for p in PARSER.transform(raw_file.rows) for f in p.flags] == []


def test_synthetic_n26_cash_effect_sums_to_the_balance(synthetic_n26):
    for space, balance in synthetic_n26.balances().items():
        raw_file = CsvExtractor().extract(synthetic_n26.to_csv_bytes(space), f"{space}.csv")
        assert sum(p.cash_effect for p in PARSER.transform(raw_file.rows)) == balance
