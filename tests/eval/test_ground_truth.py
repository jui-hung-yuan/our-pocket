"""Eval: field accuracy against the generators' own bookkeeping, over several seeds.

Ground truth is what each generator recorded while writing the export (holdings, cash, the
moves between spaces it made), never the parser's output. Target: exact, on every seed.
"""

from collections import defaultdict
from decimal import Decimal

import pytest

from our_pocket.etl.extract import CsvExtractor
from our_pocket.etl.load import BatchStore
from our_pocket.etl.models import ParsedRow
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.etl.registry import default_registry
from our_pocket.etl.transform.trade_republic import TYPE_MAP
from our_pocket.services import AccountService, LedgerService
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic import trade_republic as synth_tr

SEEDS = (1, 7, 42, 1234, 2026)
REGISTRY = default_registry()


def _read(content: bytes) -> list[ParsedRow]:
    raw_file = CsvExtractor().extract(content, "export.csv")
    transformer = REGISTRY.match(raw_file.signature)
    assert transformer is not None
    return transformer.transform(raw_file.rows)


@pytest.fixture(scope="module", params=SEEDS, ids=lambda seed: f"seed{seed}")
def trade_republic(request) -> synth_tr.SyntheticExport:
    return synth_tr.generate(seed=request.param)


@pytest.fixture(scope="module", params=SEEDS, ids=lambda seed: f"seed{seed}")
def n26(request) -> synth_n26.SyntheticN26:
    return synth_n26.generate(seed=request.param)


# --- Trade Republic --------------------------------------------------------------------

# ParsedRow field → export column it must carry, value for value.
TRADE_REPUBLIC_FIELDS = {
    "external_id": "transaction_id",
    "source_type": "type",
    "isin": "symbol",
    "instrument_name": "name",
    "asset_class": "asset_class",
    "quantity": "shares",
    "price": "price",
    "amount": "amount",
    "fee": "fee",
    "tax": "tax",
    "currency": "currency",
    "original_amount": "original_amount",
    "original_currency": "original_currency",
    "fx_rate": "fx_rate",
    "description": "description",
    "counterparty_name": "counterparty_name",
    "counterparty_iban": "counterparty_iban",
}
MONEY_FIELDS = {"fee", "tax"}  # empty means 0, not unknown
DECIMAL_FIELDS = {"quantity", "price", "amount", "original_amount", "fx_rate"}


def _expected(field: str, value: str) -> object:
    if field in MONEY_FIELDS:
        return Decimal(value or 0)
    if not value:
        return None
    return Decimal(value) if field in DECIMAL_FIELDS else value


def test_trade_republic_fields_match_the_export(trade_republic):
    parsed = _read(trade_republic.to_csv_bytes())
    assert len(parsed) == len(trade_republic.rows)
    for written, row in zip(trade_republic.rows, parsed, strict=True):
        for field, column in TRADE_REPUBLIC_FIELDS.items():
            assert getattr(row, field) == _expected(field, written[column]), (field, written)
        assert row.entry_type == TYPE_MAP[written["type"]]
        assert row.booking_date is not None and row.booking_date.isoformat() == written["date"]
        assert row.booked_at is not None and row.booked_at.utcoffset() is not None


def test_trade_republic_clean_data_has_no_flags(trade_republic):
    parsed = _read(trade_republic.to_csv_bytes())
    assert [(row.source_type, f.code) for row in parsed for f in row.flags] == []


def test_trade_republic_positions_and_cash_match_the_generator(trade_republic):
    parsed = _read(trade_republic.to_csv_bytes())
    held: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in parsed:
        if row.entry_type in ("buy", "sell"):
            assert row.isin is not None and row.quantity is not None
            held[row.isin] += row.quantity
    assert {isin: q for isin, q in held.items() if q} == trade_republic.holdings
    assert sum(r.cash_effect for r in parsed if r.cash_effect is not None) == trade_republic.cash


def test_seeds_together_cover_every_generated_type():
    """Every type the parser knows, except PRE_DETERMINED_TAX_BASE, which the generator
    doesn't write: EARNINGS covers the same reading (D-016)."""
    seen = set().union(*(synth_tr.generate(seed=seed).type_counts for seed in SEEDS))
    assert seen == set(TYPE_MAP) - {"PRE_DETERMINED_TAX_BASE"}


# --- N26 -------------------------------------------------------------------------------


def test_n26_spaces_have_no_flags_and_match_the_balances(n26):
    for space, balance in n26.balances().items():
        parsed = _read(n26.to_csv_bytes(space))
        assert [f for row in parsed for f in row.flags] == []
        assert sum(r.cash_effect for r in parsed if r.cash_effect is not None) == balance


def test_n26_space_transfers_are_exactly_the_planted_moves(n26):
    """Planted: both legs of every move the generator made between exported spaces, plus
    the other member's one-legged payments into the shared space. Nothing else."""
    position = {id(row): i for i, row in enumerate(n26.rows)}
    planted = {i for pair in n26.transfer_pairs for i in pair} | {
        i for i, row in enumerate(n26.rows) if row["Partner Name"] == synth_n26.OTHER_MEMBER_MAIN
    }
    flagged = set()
    for space in synth_n26.SPACES:
        parsed = _read(n26.to_csv_bytes(space))
        for written, row in zip(n26.space_rows(space), parsed, strict=True):
            if row.entry_type in ("space_transfer_in", "space_transfer_out"):
                flagged.add(position[id(written)])
    assert flagged == planted


# --- end to end, through the database ----------------------------------------------------


@pytest.mark.db
def test_trade_republic_ledger_matches_the_generator(conn, trade_republic):
    tr_account = AccountService(conn).add("TR Depot", "trade_republic", owner="Alex")
    batch_id = ImportPipeline(conn).run(trade_republic.to_csv_bytes(), "tr.csv", tr_account)
    BatchStore(conn).commit(batch_id)

    ledger = LedgerService(conn)
    assert ledger.positions(tr_account.name) == trade_republic.holdings
    cash = ledger.cash_balance(tr_account.name)
    assert (cash.known, cash.unverified_entries) == (trade_republic.cash, 0)


@pytest.mark.db
def test_n26_ledger_matches_the_generator(conn, n26):
    accounts = AccountService(conn)
    for i, space in enumerate(synth_n26.SPACES):
        added = accounts.add(f"N26 {i}", "n26", owner="Alex", export_account_name=space)
        batch_id = ImportPipeline(conn).run(n26.to_csv_bytes(space), f"{i}.csv", added)
        BatchStore(conn).commit(batch_id)
        cash = LedgerService(conn).cash_balance(added.name)
        assert (cash.known, cash.unverified_entries) == (n26.balances()[space], 0)
