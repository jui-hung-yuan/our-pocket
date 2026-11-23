import csv
import io
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from our_pocket.etl.extract.csv_extractor import normalise_column
from our_pocket.etl.transform import n26
from our_pocket.etl.transform.trade_republic import TYPE_MAP
from our_pocket.etl.transform.validators import is_valid_iban, is_valid_isin
from our_pocket.synthetic import FIXTURES
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic import trade_republic as synth_tr

ROOT = Path(__file__).resolve().parents[1]


def test_generator_is_deterministic():
    assert synth_tr.generate(seed=7).to_csv_bytes() == synth_tr.generate(seed=7).to_csv_bytes()
    assert synth_tr.generate(seed=7).to_csv_bytes() != synth_tr.generate(seed=8).to_csv_bytes()


@pytest.mark.parametrize("rel", FIXTURES)
def test_committed_fixtures_are_reproducible(rel):
    """data/synthetic is regenerated, never hand-edited (see `make synthetic`)."""
    assert (ROOT / rel).read_bytes() == FIXTURES[rel]()


def test_every_synthetic_csv_is_a_registered_fixture():
    on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / "data/synthetic").rglob("*.csv")}
    assert on_disk == set(FIXTURES)


def test_covers_the_types_v1_must_handle(synthetic):
    expected = {
        "BUY", "SELL", "DIVIDEND", "DISTRIBUTION", "INTEREST_PAYMENT", "CUSTOMER_INBOUND",
        "TRANSFER_INBOUND", "CUSTOMER_OUTBOUND_REQUEST", "EARNINGS", "TAX_OPTIMIZATION",
        "STOCKPERK",
    }  # fmt: skip
    assert expected <= set(synthetic.type_counts)
    assert set(synthetic.type_counts) <= set(TYPE_MAP)


def test_identifiers_are_valid_and_invented(synthetic):
    for instrument in synth_tr.INSTRUMENTS:
        assert is_valid_isin(instrument.isin)
    assert is_valid_iban(synth_tr.OWNER_IBAN)
    assert all(q > 0 for q in synthetic.holdings.values())


# --- N26 ---------------------------------------------------------------------------


def test_n26_generator_is_deterministic():
    seven = synth_n26.generate(seed=7)
    assert seven.to_csv_bytes(synth_n26.MAIN) == synth_n26.generate(seed=7).to_csv_bytes(
        synth_n26.MAIN
    )
    assert seven.to_csv_bytes(synth_n26.MAIN) != synth_n26.generate(seed=8).to_csv_bytes(
        synth_n26.MAIN
    )


def test_n26_header_is_the_registered_one():
    assert tuple(normalise_column(c) for c in synth_n26.HEADER) == n26.COLUMNS_V1


def test_n26_covers_every_type(synthetic_n26):
    assert {r["Type"] for r in synthetic_n26.rows} == set(n26.TYPE_MAP) | {n26.MONEYBEAM}


def test_n26_transfer_pairs_net_to_zero(synthetic_n26):
    assert synthetic_n26.transfer_pairs
    for debit, credit in synthetic_n26.transfer_pairs:
        out, back = synthetic_n26.rows[debit], synthetic_n26.rows[credit]
        assert Decimal(out["Amount (EUR)"]) == -Decimal(back["Amount (EUR)"]) < 0
        assert out["Partner Name"] == back["Account Name"]
        assert back["Partner Name"] == out["Account Name"]


def test_n26_has_identical_row_groups(synthetic_n26):
    assert max(Counter(tuple(r.values()) for r in synthetic_n26.rows).values()) >= 2


def test_n26_identifiers_are_valid(synthetic_n26):
    ibans = {r["Partner Iban"] for r in synthetic_n26.rows if r["Partner Iban"]}
    assert ibans and all(is_valid_iban(iban) for iban in ibans)


def test_n26_mixed_file_holds_a_deleted_space_out_of_order(synthetic_n26):
    rows = list(csv.DictReader(io.StringIO(synthetic_n26.mixed_csv_bytes().decode())))
    assert {r["Account Name"] for r in rows} == {synth_n26.MAIN, synth_n26.DELETED}
    dates = [r["Booking Date"] for r in rows]
    assert dates != sorted(dates)


def test_n26_months_filter_keeps_the_first_months(synthetic_n26):
    full = synthetic_n26.to_csv_bytes(synth_n26.MAIN)
    first_months = synthetic_n26.to_csv_bytes(synth_n26.MAIN, months=9)
    assert full.startswith(first_months) and len(first_months) < len(full)
