# our-pocket

A household ledger built from German bank and broker CSV exports. No number reaches the ledger
unvalidated, and every number traces back to its file and line.

Status: V1 in progress. Trade Republic and N26 CSV → ledger work end to end; transfer matching is next.
Plan: [PROJECT_PLAN.md](PROJECT_PLAN.md) · decisions: [docs/decisions.md](docs/decisions.md).

## How an import works

```text
CSV ─▶ fingerprint header ─▶ registry: known schema? ─▶ versioned parser
                │ unknown: rejected at intake, with a diff vs the closest known schema
                ▼
      raw.raw_row (append-only) + staging.staged_row + staging.import_flag
                │ errors block · warnings need --accept-warnings · duplicates skipped
                ▼
      core.ledger_entry   unique per (account, transaction id)
```

Parsing is deterministic code, not an LLM ([D-003](docs/decisions.md#d-003-deterministic-parsers-for-structured-exports)).

## Run it

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
make db          # Postgres 17: databases pocket and pocket_test
make hooks       # pre-commit guard against committing real exports
make migrate
make demo        # account "TR Demo" + synthetic Trade Republic import
uv run our-pocket batch show 1
uv run our-pocket batch commit 1 --accept-warnings
uv run our-pocket ledger --account "TR Demo"
```

Real exports go in git-ignored `data/raw/`:

```bash
uv run our-pocket account add "TR" --institution trade_republic --owner <name>
uv run our-pocket import data/raw/trade_republic/<export>.csv --account TR

# N26: one account per space, named as in the CSV's `Account Name` column
uv run our-pocket account add "N26 Main" --institution n26 --owner <name> --export-name "<space>"
uv run our-pocket import data/raw/n26/<export>.csv
```

## Checks

`make check` runs lint, mypy, the real-data guard, layer boundaries, a migration round trip and
the tests. CI runs the same command. Details: [docs/checks.md](docs/checks.md).

## Layout

```text
src/our_pocket/
  etl/          extract/ → registry → transform/ → load/, wired by ImportPipeline
  services/     AccountService, LedgerService: the API interfaces call
  interfaces/   cli.py
  db/           schema.py: all tables
  synthetic/    seeded generators, one per export format
migrations/     Alembic
```

Module diagrams: [docs/architecture.md](docs/architecture.md).

## Privacy

Only `data/synthetic/` is committed, and CI fails on anything else under `data/`. Synthetic data
uses invented names, ISINs and IBANs.

## License

MIT
