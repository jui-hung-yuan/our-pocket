from pathlib import Path

import pytest

from our_pocket.etl.transform.trade_republic import TYPE_MAP
from our_pocket.etl.transform.validators import is_valid_iban, is_valid_isin
from our_pocket.synthetic import FIXTURES
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
