# our-pocket

A household ledger built from German bank and broker CSV exports. No number reaches the ledger
unvalidated, and every number traces back to its file and line.

Every bank exports its own CSV, and a quiet change to one breaks whatever was built on it.
our-pocket reads Trade Republic and N26 exports into one Postgres ledger: an unknown format is
refused, an odd row is flagged for review, and the result is checked against the apps' own
balances. A local app shows cash and positions over time and the review of each import.

Status: V1 done. Next: categorisation and daily prices (V2).

## Results

On synthetic data, in every `make check`
([PROJECT_PLAN §6](PROJECT_PLAN.md#6-evaluation-plan)):

- **Field accuracy:** every parsed field equals what the generator wrote, on five seeds.
- **Planted errors:** over 50 broken rows (wrong sign, bad check digit, sums that don't add
  up…), each flagged, and nothing else flagged.
- **Format changes:** a renamed, added, dropped or moved column is refused, with the change named.

On my own exports: cash and positions match the apps to the cent. Getting there found three
rows I had read wrong, while every test passed, because the generator shared my reading. That
is why real data gets reconciled.
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
make hooks       # git hooks: real-data guard, commit-message check
make migrate
make demo        # account "TR Demo" + synthetic Trade Republic import
uv run our-pocket batch show 1
uv run our-pocket batch commit 1 --accept-warnings
uv run our-pocket ledger --account "TR Demo"
make app        # http://localhost:8501: balances over time, review and commit staged batches
```

Real exports go in git-ignored `data/raw/`:

```bash
uv run our-pocket account add "TR" --institution trade_republic --owner <name>
uv run our-pocket import data/raw/trade_republic/<export>.csv --account TR

# N26: one account per space; --export-name is the CSV's `Account Name`, checked on import
uv run our-pocket account add "N26 Main" --institution n26 --owner <name> --export-name "<space>"
uv run our-pocket import data/raw/n26/<export>.csv --account "N26 Main"
uv run our-pocket account add "N26 Shared" --institution n26 --joint --export-name "<space>"
```

## Checks

`make check` runs lint, mypy, the real-data guard, layer boundaries, a migration round trip and
the tests. CI runs the same command. Details: [docs/checks.md](docs/checks.md).

## Layout

```text
src/our_pocket/
  etl/          extract/ → registry → transform/ → load/, wired by ImportPipeline
  services/     AccountService, LedgerService: the API interfaces call
  interfaces/   cli.py, web/ (the Streamlit app)
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
