"""The pre-commit guard that keeps real bank and broker exports out of git."""

import importlib.util
from pathlib import Path

import pytest

from our_pocket.etl.transform.trade_republic import COLUMNS_V1
from our_pocket.synthetic import FIXTURES

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("guard", ROOT / "scripts/guard_real_data.py")
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

TR_HEADER = ",".join(f'"{c}"' for c in COLUMNS_V1) + "\n"


def _write(root: Path, rel: str, content: bytes) -> str:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(content)
    return rel


@pytest.mark.parametrize(
    "rel, content, reason",
    [
        ("export.csv", b"a,b\n1,2\n", ".csv files are only allowed"),
        ("docs/statement.PDF", b"%PDF-1.7", ".pdf files are only allowed"),
        ("tests/fixtures/real.xlsx", b"PK", ".xlsx files are only allowed"),
        ("data/raw/n26/x.json", b"{}", "files under data/ stay local"),
        ("data/notes.txt", b"hi", "files under data/ stay local"),
        ("notes.txt", TR_HEADER.encode(), "trade_republic.transactions_csv export format"),
        ("scan.bin", b"%PDF-1.4 ...", "PDF document"),
        ("data/synthetic/trade_republic/mine.csv", TR_HEADER.encode(), "not a registered"),
    ],
)
def test_blocked(tmp_path, rel, content, reason):
    _write(tmp_path, rel, content)
    blocked = guard.check(rel, tmp_path)
    assert blocked is not None and reason in blocked


def test_tampered_synthetic_fixture_is_blocked(tmp_path):
    rel = next(iter(FIXTURES))
    _write(tmp_path, rel, FIXTURES[rel]() + b'"real","row"\n')
    assert "differs from its generator" in guard.check(rel, tmp_path)


@pytest.mark.parametrize(
    "rel, content",
    [
        ("src/our_pocket/x.py", b"print('hi')\n"),
        ("data/README.md", b"# data\n"),
        ("README.md", b"# our-pocket\n"),
        ("data/synthetic/README.md", b"generated\n"),
    ],
)
def test_allowed(tmp_path, rel, content):
    _write(tmp_path, rel, content)
    assert guard.check(rel, tmp_path) is None


def test_registered_fixture_is_allowed(tmp_path):
    rel = next(iter(FIXTURES))
    _write(tmp_path, rel, FIXTURES[rel]())
    assert guard.check(rel, tmp_path) is None


def test_deleted_file_is_ignored(tmp_path):
    assert guard.check("gone.csv", tmp_path) is None


def test_repository_is_clean_right_now():
    assert guard.main(["--all"]) == 0
