"""Seeded generators for synthetic institution exports.

FIXTURES lists every file committed under data/synthetic/ and how to regenerate it.
The real-data guard (scripts/guard_real_data.py) only lets a CSV under data/synthetic/
into git if it is byte-identical to its generator's output, so a real export can't be
committed by copying it there.
"""

from collections.abc import Callable
from pathlib import Path

from our_pocket.synthetic import n26, trade_republic

FIXTURES: dict[str, Callable[[], bytes]] = {
    "data/synthetic/trade_republic/transactions_seed42.csv": (
        lambda: trade_republic.generate(seed=42).to_csv_bytes()
    ),
    "data/synthetic/n26/main_seed42.csv": lambda: n26.generate(seed=42).to_csv_bytes(n26.MAIN),
    "data/synthetic/n26/shared_seed42.csv": lambda: n26.generate(seed=42).to_csv_bytes(n26.SHARED),
    "data/synthetic/n26/savings_seed42.csv": (
        lambda: n26.generate(seed=42).to_csv_bytes(n26.SAVINGS)
    ),
    "data/synthetic/n26/main_mixed_seed42.csv": lambda: n26.generate(seed=42).mixed_csv_bytes(),
}


def write_fixtures(root: Path) -> list[Path]:
    written = []
    for rel, build in FIXTURES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(build())
        written.append(path)
    return written
