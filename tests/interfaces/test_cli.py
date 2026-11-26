"""The command line: argument rules, exit codes and the messages a user sees.

Exit codes: 2 for arguments argparse refuses, 1 for an error (an `error: ...` message on
stderr, no traceback) and for a reconciliation that differs, 0 otherwise.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from our_pocket.db.schema import account, person
from our_pocket.interfaces import cli
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic import trade_republic as synth_tr

ROOT = Path(__file__).resolve().parents[2]
TRADE_REPUBLIC_FIXTURE = ROOT / "data/synthetic/trade_republic/transactions_seed42.csv"
N26_FIXTURE = ROOT / "data/synthetic/n26/main_seed42.csv"


def run(capsys, *argv: str) -> tuple[int, str, str]:
    """Run the CLI; returns (exit code, stdout, stderr)."""
    try:
        cli.main(list(argv))
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def database(conn, shared_connection, monkeypatch):
    monkeypatch.setattr(cli, "get_engine", lambda: shared_connection)
    return conn


# --- arguments (no database) ---------------------------------------------------------


@pytest.mark.parametrize(
    "whose, message",
    [
        ((), "one of the arguments --owner --joint is required"),
        (("--owner", "Alex", "--joint"), "not allowed with argument"),
    ],
    ids=["neither", "both"],
)
def test_account_add_needs_exactly_one_of_owner_or_joint(capsys, whose, message):
    code, _, err = run(capsys, "account", "add", "TR", "--institution", "trade_republic", *whose)
    assert code == 2
    assert message in err


@pytest.mark.parametrize(
    "argv",
    [
        ("account", "add", "X", "--institution", "comdirect", "--owner", "Alex"),
        ("batch", "show", "one"),
        ("batch", "reject", "1"),  # --reason is required
        ("import", "file.csv"),  # --account is required
    ],
    ids=["unknown institution", "batch id not a number", "reject without reason", "no account"],
)
def test_refused_arguments_exit_2(capsys, argv):
    assert run(capsys, *argv)[0] == 2


def test_synth_writes_the_generators_export(capsys, tmp_path):
    out = tmp_path / "export.csv"
    code, stdout, _ = run(capsys, "synth", "trade-republic", "--out", str(out), "--seed", "7")
    assert code == 0
    assert out.read_bytes() == synth_tr.generate(seed=7).to_csv_bytes()
    assert stdout.startswith("wrote ")


# --- accounts ------------------------------------------------------------------------


def _owner_of(conn, name: str) -> str | None:
    query = (
        select(person.c.name)
        .select_from(account.outerjoin(person, account.c.owner_person_id == person.c.id))
        .where(account.c.name == name)
    )
    return conn.execute(query).scalar_one()


@pytest.mark.db
def test_joint_account_has_no_owner_and_owned_account_has_one(capsys, database):
    assert run(capsys, "account", "add", "Mine", "--institution", "trade_republic",
               "--owner", "Alex")[0] == 0  # fmt: skip
    assert run(capsys, "account", "add", "Ours", "--institution", "n26", "--joint",
               "--export-name", "Alex & Sam")[0] == 0  # fmt: skip
    assert _owner_of(database, "Mine") == "Alex"
    assert _owner_of(database, "Ours") is None


@pytest.mark.db
def test_owner_name_is_stripped_so_it_is_the_same_person(capsys, database):
    run(capsys, "account", "add", "TR 1", "--institution", "trade_republic", "--owner", "Alex")
    run(capsys, "account", "add", "TR 2", "--institution", "trade_republic", "--owner", " Alex ")
    owners = database.execute(select(account.c.owner_person_id)).scalars().all()
    assert len(owners) == 2 and len(set(owners)) == 1


@pytest.mark.db
@pytest.mark.parametrize("owner", ["", "   "])
def test_empty_owner_is_refused_not_made_joint(capsys, database, owner):
    """D-024: `--owner ""` passes argparse; it must not create a joint account."""
    code, _, err = run(
        capsys, "account", "add", "TR", "--institution", "trade_republic", "--owner", owner
    )
    assert (code, err.strip()) == (1, "error: owner is empty; name the person, or use --joint")
    assert database.execute(select(account.c.id)).first() is None


# --- import, review, commit, reconcile -------------------------------------------------


def _batch_id(stdout: str) -> str:
    match = re.match(r"batch (\d+)  \[", stdout)
    assert match, stdout
    return match.group(1)


def _snapshot(tmp_path, export: synth_tr.SyntheticExport, cash: Decimal) -> str:
    path = tmp_path / "app.json"
    positions = {isin: str(quantity) for isin, quantity in export.holdings.items()}
    path.write_text(
        json.dumps(
            {
                "account": "TR Depot",
                "as_of": "2026-02-28",
                "cash": str(cash),
                "positions": positions,
            }
        )
    )
    return str(path)


@pytest.mark.db
def test_import_review_commit_and_reconcile(
    capsys, database, synthetic, tmp_path, stockperk_unverified
):
    run(capsys, "account", "add", "TR Depot", "--institution", "trade_republic", "--owner", "Alex")

    code, out, _ = run(capsys, "import", str(TRADE_REPUBLIC_FIXTURE), "--account", "TR Depot")
    assert code == 0
    batch = _batch_id(out)
    assert "[staged]" in out and "parser 4" in out
    assert "cash_semantics_unverified" in out
    assert f"batch commit {batch} --accept-warnings" in out

    code, _, err = run(capsys, "batch", "commit", batch)
    assert code == 1 and "warning(s)" in err

    code, out, _ = run(capsys, "batch", "commit", batch, "--accept-warnings")
    assert code == 0
    assert f"{len(synthetic.rows)} ledger entries, 0 duplicates skipped" in out

    code, out, _ = run(capsys, "ledger", "--account", "TR Depot")
    assert code == 0 and f"positions ({len(synthetic.holdings)})" in out

    # The app's cash as the ledger counts it while STOCKPERK is unverified: without the perk.
    perks = sum(Decimal(r["amount"]) for r in synthetic.rows if r["type"] == "STOCKPERK")
    cash = (synthetic.cash - perks).quantize(Decimal("0.01"))
    code, out, _ = run(capsys, "reconcile", _snapshot(tmp_path, synthetic, cash))
    assert (code, out.rstrip().splitlines()[-1]) == (0, "result: matches")

    code, out, _ = run(capsys, "reconcile", _snapshot(tmp_path, synthetic, cash + 1))
    assert (code, out.rstrip().splitlines()[-1]) == (1, "result: differs")
    assert "MISMATCH" in out


@pytest.mark.db
def test_rejected_batch_cannot_be_committed(capsys, database):
    run(capsys, "account", "add", "N26", "--institution", "n26", "--owner", "Alex",
        "--export-name", synth_n26.MAIN)  # fmt: skip
    _, out, _ = run(capsys, "import", str(N26_FIXTURE), "--account", "N26")
    batch = _batch_id(out)
    assert run(capsys, "batch", "reject", batch, "--reason", "test")[:2] == (
        0, f"batch {batch} rejected\n",
    )  # fmt: skip
    code, _, err = run(capsys, "batch", "commit", batch)
    assert (code, err.strip()) == (1, f"error: batch {batch} is rejected, not staged")


# --- errors: exit 1 with a message, no traceback ------------------------------------------------


def _bad_json(tmp_path) -> str:
    (tmp_path / "bad.json").write_text('{"account": "TR Depot", "cash": "1"}')
    return str(tmp_path / "bad.json")


@pytest.mark.db
@pytest.mark.parametrize(
    "argv, message",
    [
        (lambda tmp: ("import", str(TRADE_REPUBLIC_FIXTURE), "--account", "nope"),
         "no account named 'nope'"),
        (lambda tmp: ("import", str(tmp / "missing.csv"), "--account", "TR Depot"),
         "No such file"),
        (lambda tmp: ("account", "add", "TR Depot", "--institution", "trade_republic",
                      "--owner", "Sam"), "duplicate key"),
        (lambda tmp: ("account", "add", "TR", "--institution", "trade_republic", "--owner",
                      "Alex", "--export-name", "Main"), "drop --export-name"),
        (lambda tmp: ("batch", "show", "999999"), "no batch 999999"),
        (lambda tmp: ("reconcile", str(tmp / "missing.json")), "No such file"),
        (lambda tmp: ("reconcile", _bad_json(tmp)), "missing ['as_of']"),
    ],
    ids=["unknown account", "missing file", "account name taken", "export name for TR",
         "unknown batch", "missing snapshot", "snapshot without as_of"],
)  # fmt: skip
def test_errors_exit_1_with_a_message(capsys, database, tmp_path, argv, message):
    run(capsys, "account", "add", "TR Depot", "--institution", "trade_republic", "--owner", "Alex")
    code, _, err = run(capsys, *argv(tmp_path))
    assert code == 1
    assert err.startswith("error: "), err
    assert message in err
