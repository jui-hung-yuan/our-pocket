# Decision log

Short entries: what I chose, over what, and why. Newest at the bottom. A decision is changed
by adding an entry that supersedes it, not by editing the old one.

---

### D-001: PostgreSQL, not BigQuery
*2026-11-19*

The data is small and transactional (thousands of rows a year), and row-level security is how the personal vs household views will be enforced in V3,
inside the database rather than in prompts. BigQuery is a GCP-only warehouse built for scale this
project doesn't have.

### D-002: V1 is CSV only
*2026-11-19*

**Chose:** institutions' own CSV exports (Trade Republic transaction CSV, N26 CSV). **Over:** Trade
Republic and Scalable PDFs with LLM extraction, the original plan.

**Why:** Trade Republic now exports a transaction CSV, so the main broker no longer needs PDF
extraction to get a correct ledger. Building the ledger on deterministic parsers first gives a
trustworthy base, and later gives ground truth for any LLM extractor.

**Cost:** V1 loses the "LLM extractor vs Portfolio Performance's parsers" benchmark, which was the
strongest eval story. The V1 eval story becomes "zero unflagged errors and schema
drift caught at intake" (PROJECT_PLAN §6). LLM work moves to V2 (categorisation, and PDFs if they're
still needed), where it can be measured against the CSV-derived ledger.

### D-003: Deterministic parsers for structured exports
*2026-11-19*

A CSV with a known header is a solved problem; an LLM would add cost, latency and a hallucination
risk for nothing. The README will say so: knowing when *not* to use GenAI is the point.

### D-004: SQLAlchemy Core + Alembic, not the ORM
*2026-11-19*

The pipeline moves sets of rows between layers (`INSERT … SELECT … ON CONFLICT`), which Core
expresses directly. No identity map or lazy-loading to reason about. Alembic gives versioned
migrations; `tests/test_migrations.py` fails if `db/schema.py` and the migrations disagree.

### D-005: Identify file formats by header fingerprint; refuse unknown ones
*2026-11-19*

**Chose:** sha256 of the normalised column names (in order) plus the delimiter, looked up in an
explicit registry of versioned schemas. Unknown → batch rejected at intake with a diff against the
closest known schema.

**Over:** "best effort" parsing by column name, which silently keeps working when a column is renamed
and then writes NULLs into the ledger.

Column order is part of the fingerprint on purpose: a reorder is cheap to register (reuse the parser,
bump the version) and it is a signal that the export changed. Case, whitespace and BOM are ignored.

### D-006: Stage → review → commit, with no partial commits
*2026-11-19*

Every file becomes an `import_batch`. Parsing writes only to staging tables. Any `error` flag blocks
the whole batch; warnings need an explicit `--accept-warnings`, which is recorded on the batch.

Partial commits were rejected: committing 299 of 300 rows leaves a hole in the ledger that nothing
points to. A blocked batch is visible and gets fixed.

### D-007: Trade Republic CSV conventions (v1)
*2026-11-19*

Profiled on a real export (only column shapes were looked at):

- 23 columns, comma-delimited, every value quoted, UTF-8 without BOM, rows in ascending time order.
- `amount` is gross. `fee` and `tax` are separate signed columns. BUY: `shares` > 0 and
  `amount = −shares × price`. SELL: `shares` < 0 and `amount = |shares| × price`. Held on every
  trade to within 0.02.
- Foreign dividends: `amount = original_amount × fx_rate` (every row).
- `datetime` is UTC with either 3 or 6 fractional digits. `date` is the local booking date and
  can differ from the UTC date (late trades).
- 12 `type` values, mapped to 9 normalised `entry_type`s; the raw type is kept in `source_type`.

`cash_effect = amount + fee + tax` for most types. For `EARNINGS`, `PRE_DETERMINED_TAX_BASE`
(Vorabpauschale bases), `TAX_OPTIMIZATION` and `STOCKPERK`, `amount` doesn't obviously relate to
`tax`, so `cash_effect` is left NULL with a `cash_semantics_unverified` warning, instead of guessed.
Open question Q1: settle by reconciling against the app's cash balance. *Superseded for the three
tax types by D-016; `STOCKPERK` is still open.*

### D-008: Deduplicate by file hash and by (account, external id)
*2026-11-19*

The same bytes can't be staged twice while an earlier batch is still active. Overlapping exports are
expected (monthly vs yearly downloads), so rows whose `transaction_id` is already in the ledger are
flagged `already_in_ledger` and skipped. A unique constraint plus `ON CONFLICT DO NOTHING` also
covers rows committed by another batch between staging and commit.

### D-009: Synthetic data from a seeded generator with invented identifiers
*2026-11-19*

Instruments, people, ISINs and IBANs are invented, but their check digits are valid, so the
validators are exercised for real. The committed fixture must equal `generate(seed=42)` byte for
byte (tested), so nobody hand-edits it. Ground truth (holdings) comes from the generator's own
bookkeeping, never from parsing its output. Real exports stay in git-ignored `data/raw/`, and CI
fails if anything outside `data/synthetic/` under `data/` is tracked.

### D-010: TEXT + CHECK constraints instead of Postgres ENUMs
*2026-11-19*

Adding a value to a native enum needs special migration handling; changing a CHECK is one line.
New entry types are expected as more institutions are added.

### D-011: `household_id` and `owner_person_id` from day one
*2026-11-19*

V1 has one user, but adding ownership columns to a populated ledger later means a data migration.
`owner_person_id = NULL` means a joint account. Row-level security policies come in V3.

### D-012: Raw rows stored losslessly as JSONB strings
*2026-11-19*

L1 keeps every value exactly as read, keyed by column name, with its line number. A parser bug
is fixed by re-parsing from L1. Every ledger entry links back via
`staged_row → raw_row → import_batch` (tested).

### D-013: Boring tooling
*2026-11-19*

uv, ruff, pytest, argparse, a Makefile, Docker Compose. No orchestration framework. DB tests run against a real Postgres; CI sets `REQUIRE_DB=1` so they can't silently skip.

### D-014: ETL packages and layers; extractors per format, transformers per institution
*2026-11-19*

**Chose:** `etl/` (extract → transform → load, wired by `ImportPipeline`), `services/` (the API),
`interfaces/` (CLI; agent tools later), `db/`. Abstract base classes `Extractor` and
`Transformer`, with one subclass per file format and one per (institution, schema version)
respectively. Diagrams in [architecture.md](architecture.md).

**Over:** flat modules at the package root (`accounts.py`, `batches.py`, `ledger.py`), and one
extractor class per institution.

**Why:** the root layout hid which code touches the database and which is pure. Splitting by ETL
stage makes the data flow visible and lets agent tools (V3) depend on `services/` only, never on the
import machinery. Extractors are per format because Trade Republic and N26 are both CSV: what
differs is the meaning of columns, which is transformation. The layer rules are enforced by
`tests/test_architecture.py`.

### D-015: One `make check`; real-data guard by content, not just by path
*2026-11-20*

CI runs only `make check`, so local and CI checks can't drift apart. The migration check uses
`alembic check` plus a downgrade/upgrade round trip on the test database, replacing the earlier
pytest comparison. mypy runs strict on `src/` and `migrations/`.

The guard runs both as a pre-commit hook and in `make check` over every committable file, so
`--no-verify` can't get real data past CI. It blocks by extension, by location, and by content
(PDF magic bytes; headers matching a registered institution format), and accepts files under
`data/synthetic/` only if they're byte-identical to a registered generator's output. Relying on
`.gitignore` alone was rejected: a single `git add -f`, or a CSV saved outside `data/`, would leak a
household's transactions into a public repo.

### D-016: Tax-only rows move cash through `tax` (parser v2)
*2026-11-21*

Profiling the real export answered Q1 for three of the four types. On every `TAX_OPTIMIZATION`,
`EARNINGS` and `PRE_DETERMINED_TAX_BASE` row, `amount` is 0 and the money is in `tax`:

- `TAX_OPTIMIZATION`: `tax` > 0 on every row. These are refunds of tax withheld earlier, when
  later realised losses (or the allowance) offset gains. They carry no ISIN, so they can't be
  linked to a single sell: the loss pot is account-wide.
- `EARNINGS`, `PRE_DETERMINED_TAX_BASE`: `tax` < 0, booked in January. This is the Vorabpauschale
  tax being debited.

**Chose:** for these types `cash_effect = amount + fee + tax` like every other type (so it equals
`tax`), with a `tax_row_has_amount` warning and NULL `cash_effect` if `amount` is ever non-zero.
`ledger` reports tax per entry type and the net, so refunds aren't lost in the totals. Parser
version 2; schema version unchanged (the file format didn't change, only its reading).

**Over:** keeping them NULL until a cash-balance reconciliation. That dropped every tax refund
from the ledger, so net tax came out wrong.

**Still open:** `STOCKPERK` (amount > 0, no shares, no tax) stays `cash_semantics_unverified`. The
synthetic generator previously put the money in `amount` for these types; it now matches the real
shape, which is the circularity caveat in the eval plan in action.

### D-017: One Postgres schema per layer; `raw` is append-only in the database
*2026-11-22*

**Chose:** `raw` (`raw_row`), `staging` (`import_batch`, `staged_row`, `import_flag`) and `core`
(`household`, `person`, `account`, `ledger_entry`), moved out of `public` by migration 0002. A
trigger refuses UPDATE, DELETE and TRUNCATE on `raw.raw_row` (tested in `tests/test_raw_layer.py`).
`alembic_version` stays in `public`.

**Over:** everything in `public`, with L1 immutability resting on convention in `BatchStore`.

**Why:** D-012's promise ("a parser bug is fixed by re-parsing from L1") only holds if nothing can
edit L1, and a trigger holds for ad-hoc SQL and future code too, not just today's code. Schemas also
make the trust level visible in every query and give V3 a natural grant boundary (agent tools get
`USAGE` on `core` only). `import_batch` sits in `staging`, not `raw`, because review updates its
`status`; its file-identity columns are written once, but splitting the table wasn't worth it yet.
An `analytics` schema for report views comes when the first view does.
