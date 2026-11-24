"""our-pocket command line.

our-pocket account add "TR Depot" --institution trade_republic --owner Alex
our-pocket import data/synthetic/trade_republic/transactions_seed42.csv --account "TR Depot"
our-pocket account add "N26 Main" --institution n26 --owner Alex --export-name "Alex Example"
our-pocket import data/synthetic/n26/main_seed42.csv --account "N26 Main"   # one file per space
our-pocket batch show 1
our-pocket batch commit 1 --accept-warnings
our-pocket ledger --account "TR Depot"
our-pocket reconcile data/raw/trade_republic/app_2026-11-24.json   # ledger vs the app
our-pocket synth fixtures          # regenerate everything under data/synthetic/
"""

import argparse
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from our_pocket.db import get_engine
from our_pocket.db.schema import DEFAULT_HOUSEHOLD
from our_pocket.etl.extract import ExtractionError
from our_pocket.etl.load import BatchError, BatchReport, BatchStore
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.services import (
    AccountNotFound,
    AccountService,
    AppSnapshot,
    LedgerService,
    Reconciliation,
    ReconciliationService,
)
from our_pocket.synthetic import FIXTURES, write_fixtures
from our_pocket.synthetic import trade_republic as synth_tr


def _print_report(report: BatchReport, *, max_flags: int) -> None:
    schema = (
        f"{report.schema_id} v{report.schema_version} (parser {report.parser_version})"
        if report.schema_id
        else "unknown"
    )
    print(f"batch {report.batch_id}  [{report.status}]  {report.file_name} -> {report.account}")
    print(f"  schema:     {schema}")
    print(f"  rows:       {report.row_count} ({report.duplicate_count} already in ledger)")
    counts = ", ".join(f"{n} {s}" for s, n in sorted(report.flag_counts.items())) or "none"
    print(f"  flags:      {counts}")
    by_code = Counter((f["severity"], f["code"]) for f in report.flags)
    for (severity, code), n in sorted(by_code.items()):
        print(f"    {severity:<7} {code:<28} x{n}")
    shown = [f for f in report.flags if f["severity"] != "info"][:max_flags]
    for f in shown:
        where = f"line {f['line_number']}" if f["line_number"] else "file"
        print(f"    - {where}: [{f['code']}] {f['message']}")
    if report.status == "staged":
        if report.errors:
            print("  next: fix the source and re-import, or `batch reject`")
        elif report.warnings:
            print(
                f"  next: review warnings, then `batch commit {report.batch_id} --accept-warnings`"
            )
        else:
            print(f"  next: `batch commit {report.batch_id}`")


def cmd_account_add(args: argparse.Namespace) -> None:
    with get_engine().begin() as conn:
        account = AccountService(conn).add(
            args.name,
            args.institution,
            owner=args.owner,
            household_name=args.household,
            export_account_name=args.export_name,
        )
    print(f"account {account.id} {account.name!r} created")


def cmd_import(args: argparse.Namespace) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        batch_id = ImportPipeline(conn).run_path(
            Path(args.file), AccountService(conn).get(args.account)
        )
    with engine.connect() as conn:
        _print_report(BatchStore(conn).report(batch_id), max_flags=args.max_flags)


def cmd_batch_show(args: argparse.Namespace) -> None:
    with get_engine().connect() as conn:
        _print_report(BatchStore(conn).report(args.batch_id), max_flags=args.max_flags)


def cmd_batch_commit(args: argparse.Namespace) -> None:
    with get_engine().begin() as conn:
        result = BatchStore(conn).commit(args.batch_id, accept_warnings=args.accept_warnings)
    print(
        f"batch {result.batch_id} committed: {result.inserted} ledger entries, "
        f"{result.skipped_duplicates} duplicates skipped"
    )


def cmd_batch_reject(args: argparse.Namespace) -> None:
    with get_engine().begin() as conn:
        BatchStore(conn).reject(args.batch_id, args.reason)
    print(f"batch {args.batch_id} rejected")


def cmd_ledger(args: argparse.Namespace) -> None:
    with get_engine().connect() as conn:
        ledger = LedgerService(conn)
        totals = ledger.totals_by_type(args.account)
        taxes = ledger.taxes(args.account)
        held = ledger.positions(args.account)
    print(f"{'entry type':<16}{'count':>7}{'cash effect':>16}")
    for entry_type, (n, cash) in totals.items():
        print(f"{entry_type:<16}{n:>7}{'—' if cash is None else f'{cash:,.2f}':>16}")
    print(f"\n{'tax by type':<16}{'count':>7}{'tax':>16}")
    for entry_type, (n, tax) in taxes.items():
        print(f"{entry_type:<16}{n:>7}{tax:>16,.2f}")
    net = sum((tax for _, tax in taxes.values()), Decimal(0))
    print(f"{'net':<23}{net:>16,.2f}  (negative = paid, positive = refunded)")
    print(f"\npositions ({len(held)})")
    for isin, qty in held.items():
        print(f"  {isin}  {qty.normalize():f}")


def _number(value: Decimal | None) -> str:
    return "—" if value is None else f"{value.normalize():,f}"


def _print_reconciliation(result: Reconciliation) -> None:
    snapshot, cash = result.snapshot, result.cash
    print(f"{snapshot.account!r}: ledger entries booked up to {snapshot.as_of} vs the app")
    if result.positions:
        print(f"\n{'position':<14}{'ledger':>16}{'app':>16}{'difference':>16}")
        for p in result.positions:
            verdict = "ok" if p.matches else "MISMATCH"
            print(
                f"{p.isin:<14}{_number(p.ledger):>16}{_number(p.app):>16}"
                f"{_number(p.difference):>16}  {verdict}"
            )
    print(f"\n{'cash':<14}{'ledger':>16}{'app':>16}{'difference':>16}")
    verdict = "ok" if cash.matches else "MISMATCH"
    print(
        f"{'':<14}{cash.ledger.known:>16,.2f}{cash.app:>16,.2f}{cash.difference:>16,.2f}  {verdict}"
    )
    if cash.ledger.unverified_entries:
        count = cash.ledger.unverified_entries
        print(
            f"  left out: {count} {'entry' if count == 1 else 'entries'} with unverified cash "
            f"meaning, amount {cash.ledger.unverified_amount:,.2f}"
        )
        if cash.matches_if_unverified_is_cash:
            print("  counting their amount as cash closes the gap: they move cash")
        elif cash.matches:
            print("  the cash matches without them: they move no cash")
    print("\nresult: " + ("matches" if result.matches else "differs"))


def cmd_reconcile(args: argparse.Namespace) -> None:
    snapshot = AppSnapshot.from_json(Path(args.snapshot).read_text())
    with get_engine().connect() as conn:
        result = ReconciliationService(conn).reconcile(snapshot)
    _print_reconciliation(result)
    if not result.matches:
        sys.exit(1)


def cmd_synth_fixtures(args: argparse.Namespace) -> None:
    for path in write_fixtures(Path(args.root)):
        print(f"wrote {path}")


def cmd_synth_tr(args: argparse.Namespace) -> None:
    export = synth_tr.generate(seed=args.seed, months=args.months)
    export.write_csv(Path(args.out))
    print(f"wrote {len(export.rows)} rows to {args.out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="our-pocket", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(required=True)

    account_parser = sub.add_parser("account", help="manage accounts").add_subparsers(required=True)
    add = account_parser.add_parser("add", help="create an account")
    add.add_argument("name")
    add.add_argument(
        "--institution", required=True, choices=AccountService.supported_institutions()
    )
    whose = add.add_mutually_exclusive_group(required=True)
    whose.add_argument("--owner", help="person owning the account")
    whose.add_argument("--joint", action="store_true", help="shared by the household")
    add.add_argument("--household", default=DEFAULT_HOUSEHOLD)
    add.add_argument(
        "--export-name",
        help="the account's name inside the export (N26: the `Account Name` column)",
    )
    add.set_defaults(func=cmd_account_add)

    imp = sub.add_parser("import", help="stage a file as a new import batch")
    imp.add_argument("file")
    imp.add_argument("--account", required=True, help="account to import into")
    imp.add_argument("--max-flags", type=int, default=20)
    imp.set_defaults(func=cmd_import)

    batch = sub.add_parser("batch", help="review, commit or reject a batch").add_subparsers(
        required=True
    )
    show = batch.add_parser("show")
    show.add_argument("batch_id", type=int)
    show.add_argument("--max-flags", type=int, default=50)
    show.set_defaults(func=cmd_batch_show)
    commit = batch.add_parser("commit")
    commit.add_argument("batch_id", type=int)
    commit.add_argument("--accept-warnings", action="store_true")
    commit.set_defaults(func=cmd_batch_commit)
    reject = batch.add_parser("reject")
    reject.add_argument("batch_id", type=int)
    reject.add_argument("--reason", required=True)
    reject.set_defaults(func=cmd_batch_reject)

    led = sub.add_parser("ledger", help="summarise committed entries")
    led.add_argument("--account")
    led.set_defaults(func=cmd_ledger)

    rec = sub.add_parser("reconcile", help="compare the ledger with numbers from the app")
    rec.add_argument("snapshot", help="JSON: account, as_of, cash, optional positions")
    rec.set_defaults(func=cmd_reconcile)

    synth = sub.add_parser("synth", help="generate synthetic exports").add_subparsers(required=True)
    fixtures = synth.add_parser(
        "fixtures", help=f"regenerate the {len(FIXTURES)} committed synthetic file(s)"
    )
    fixtures.add_argument("--root", default=".")
    fixtures.set_defaults(func=cmd_synth_fixtures)
    tr = synth.add_parser("trade-republic", help="write one export with any seed")
    tr.add_argument("--out", required=True)
    tr.add_argument("--seed", type=int, default=42)
    tr.add_argument("--months", type=int, default=14)
    tr.set_defaults(func=cmd_synth_tr)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except IntegrityError as exc:
        print(f"error: {exc.orig}", file=sys.stderr)
        sys.exit(1)
    except (BatchError, AccountNotFound, ExtractionError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
