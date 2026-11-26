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
migrations; `make migrations` fails if `db/schema.py` and the migrations disagree.

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
`tests/repo/test_architecture.py`.

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
trigger refuses UPDATE, DELETE and TRUNCATE on `raw.raw_row` (tested in `tests/db/test_raw_layer.py`).
`alembic_version` stays in `public`.

**Over:** everything in `public`, with L1 immutability resting on convention in `BatchStore`.

**Why:** D-012's promise ("a parser bug is fixed by re-parsing from L1") only holds if nothing can
edit L1, and a trigger holds for ad-hoc SQL and future code too, not just today's code. Schemas also
make the trust level visible in every query and give V3 a natural grant boundary (agent tools get
`USAGE` on `core` only). `import_batch` sits in `staging`, not `raw`, because review updates its
`status`; its file-identity columns are written once, but splitting the table wasn't worth it yet.
An `analytics` schema for report views comes when the first view does.

### D-018: One account per N26 space, routed by `Account Name`
*2026-11-22*

**Chose:** each N26 space is its own account, matched by `account.export_account_name`, which is
unique per household (two households can both have a "Main Account"). An N26 file is imported
without `--account` (with `--household` when not the default); its rows are grouped by
`Account Name` and each group is staged as its own batch. If any space has no account, nothing
is staged and the error lists the `account add` command per space. Spaces already committed from
the same file are skipped on re-import, so a rejected space can be imported again. Migration 0003
adds the column and the bank entry types (`card_payment`, `card_refund`, `transfer_in`,
`transfer_out`, `direct_debit`, `fee`, `tax`).

**Over:** one account per N26 login, with the space kept on each row.

**Why:** ownership is per space (a shared space is joint, `owner_person_id = NULL`), and most
transfers are between spaces, which P2 can only match if they're movements between accounts.
Real exports are mostly one file per space but can include rows of another space (Q4).
*Superseded by D-022: imports name their account, and `Account Name` is only checked.*

### D-019: N26 external id is a content hash plus an occurrence number
*2026-11-22*

**Chose:** `external_id = "n26:" + sha256(every value except Account Name)[:32] + ":" + n`,
where `n` numbers identical rows within a space in file order.

**Over:** a hash alone (identical rows are real: two equal card payments on one day), hashing
`Account Name` too, or no deduplication for N26.

**Why:** N26 exports no transaction id. The ledger is unique per account already, and a space can
be renamed in the app, which would otherwise give every overlapping row a new id. N26 exports
whole days, so a group of identical rows is never split between overlapping exports, and their
numbers match. **Limit:** if N26 edits a past row (a cleaned-up merchant name), a re-export gives
it a new id and it's imported twice.
*Partner name on space moves and numbering per space superseded by D-023.*

### D-020: N26 fees and taxes go in the `fee` and `tax` columns
*2026-11-22*

**Chose:** Fee rows carry their money in `fee` and Tax rows in `tax`, with `amount = 0`. Dates
have no time, so `booked_at` is the booking date at 00:00 Europe/Berlin.

**Over:** the money in `amount`, typed `fee` or `tax`.

**Why:** `cash_effect = amount + fee + tax` then holds across institutions, and tax totals cover
N26 too.

### D-021: Flag moves between N26 spaces; don't pair transfer legs
*2026-11-23*

**Chose:** an N26 Credit or Debit Transfer without a partner IBAN is typed `space_transfer_in` or
`space_transfer_out` (parser v2, migration 0004). Every other transfer stays `transfer_in` or
`transfer_out`. Nothing links the two legs of a move.

**Over:** matching transfer pairs across accounts by amount and date (the original P2), with
precision/recall targets.

**Why:** balances and net worth are right without any link, because the two legs cancel. What
breaks without help is one-sided totals (spending, income), and a flag is enough to keep space
moves out of those. In the real exports the rule has no exceptions: every transfer without an
IBAN names a space, and none with an IBAN does. Pairing would also fail often: most moves
involving the shared space go to or from the other member's spaces, which aren't imported yet.
Transfers to other institutions (N26 → Trade Republic) stay ordinary outflows and inflows.

### D-022: N26 imports name their account; `Account Name` is only checked
*2026-11-23*

**Chose:** an N26 file is imported with `--account`, like Trade Republic: one file, one account,
one batch. A row whose `Account Name` is another account's export name is an `other_account`
error; any other foreign space name is an `unknown_space` warning, and accepting it keeps the
row in this account. Export names are no longer unique (migration 0004).

**Over:** routing rows to accounts by `Account Name` (D-018), which then needed `--owner` and
per-owner uniqueness once a second member's spaces are imported: both members' exports call
their main space by the same default name.

**Why:** exports are one file per space. The rows of another space that do turn up come from a
deleted space and belong to Main anyway; transfers between it and Main have both legs in Main
and cancel. Rows of a space that has its own account would count twice, so they block commit;
that also catches a file imported into the wrong account.

### D-023: N26 ids leave out the partner name on space moves; numbered per file
*2026-11-23*

**Chose:** on a `space_transfer_*` row the hash skips `Partner Name` as well as `Account Name`.
Identical rows are numbered within the file instead of within the space.

**Over:** hashing `Partner Name` on every row (D-019).

**Why:** on a space move the partner name is a space name, which the user can rename; if N26
shows the new name on old rows, an overlapping re-import would add them again. Numbering per
file follows from D-022: a file goes into one account, so a deleted space's row identical to a
Main row needs its own number. **Limit:** two moves on one day with the same amount and reference
are told apart only by their order in the file.

### D-024: `account add` needs `--owner` or `--joint`
*2026-11-23*

**Chose:** the CLI requires exactly one of them. **Over:** a missing `--owner` meaning joint.
**Why:** a forgotten flag silently made a personal account joint, visible to the whole household.

### D-025: Eval suites are tests, not a separate harness
*2026-11-24*

**Chose:** the §6 suites as pytest files (`tests/eval/`) in `make check`. Each one
asserts its target directly: a planted defect must be flagged on its row and nowhere else, a
drifted header refused with the change named. Defects no row check can see are listed as blind
spots, asserted to pass unflagged, each naming what catches it instead (mostly reconciliation).

**Over:** a harness with its own runner and a score report.

**Why:** V1 has no model, so every target is exact (0 unflagged, 100% detected) and a failed
assertion is the report. A harness earns its place in V2, where an LLM's accuracy is a rate
to track, not a pass/fail.

### D-026: Reconcile against a hand-typed app snapshot
*2026-11-24*

**Chose:** `our-pocket reconcile SNAPSHOT.json` compares the ledger up to `as_of` with cash
and positions typed from the app. A value matches when it differs by less than one unit of the
last digit typed. Exit code 1 on any difference. Snapshots live with the exports in `data/raw/`.

**Over:** exact equality (apps round or cut off share counts), and parsing a position statement.

**Why:** the app's own numbers are the only ground truth that doesn't come from my reading of
the format. Typing a few numbers is cheaper than a PDF parser that would need its own checks.

### D-027: A parser fix means reset and import again; no re-parse command yet
*2026-11-24*

**Chose:** after a parser fix, `make reset-db` and import the files in `data/raw/` again.
**Over:** a command re-parsing stored batches from `raw.raw_row` (D-012's promise).
**Why:** at this scale a rebuild is fast and loses nothing. That changes once something lives
only in the database. Manual corrections and LLM categories (V2) will be kept outside it (a
file keyed by external id, a cache keyed by input), so a rebuild keeps them; if that turns out
not to be possible, re-parse gets built then.

### D-028: Trade Republic booking date may be a few days before its timestamp
*2026-11-24*

**Chose:** warn when `date` is after the Berlin date of `datetime`, or more than 4 days
before it (parser 3). **Over:** requiring them to be equal, as the generator assumed.
**Why:** real exports book some rows, tax refunds most often, 1–2 days before their timestamp,
so an equality check would flag good rows. The window still catches a date moved by a week,
which was a blind spot. The generator now books tax refunds a day early, as real exports do.

### D-029: Tests mirror `src/`; the eval suites and repo checks sit apart
*2026-11-25*

**Chose:** tests of code in `src/our_pocket/<path>/` live in `tests/<path>/`, named after the
module they test where there is one (`etl/transform/test_n26.py`).
`tests/eval/` holds the suites that ask one question of every parser (D-025), and `tests/repo/`
the rules about the codebase (layers, real-data guard). pytest runs with `--import-mode=importlib`,
so two folders can each have a `test_n26.py`.
**Over:** one flat folder, or folders split by whether a test needs the database (the `db`
marker does that).
**Why:** a failing test points at the module to open, and a new module's tests have one obvious place.

### D-030: N26 `Authorization` is typed by sign, its cash meaning unverified (N26 parser 3)
*2026-11-26*

**Chose:** an `Authorization` row becomes `card_payment` if negative and `card_refund` if
positive, with `cash_effect` left NULL and a `cash_semantics_unverified` warning, as for
`STOCKPERK` (D-007). **Over:** refusing the type (the batch could never be committed), or
counting it as cash.
**Why:** a real Main export holds one, from 2023: a positive card authorization that never became
a `Presentment`, with no other row from that partner. Whether N26 counted it in the balance
can't be read from the file. Reconciling the account against the app's balance settles it:
`reconcile` reports whether counting it closes a gap (Q1).

### D-031: Positions in the chart are valued at cost, by average cost
*2026-11-25*

**Chose:** what the shares still held cost: a buy adds the cash it took, fees included; a sale
removes the average cost of the shares sold, per account and ISIN. A sale larger than the
holding stops the chart (`MissingBuys`) instead of guessing.
**Over:** the last trade price seen in the export, and FIFO.
**Why:** cost is exact and checkable against the export; a last trade price can be months old
for a stock bought once. FIFO is what German tax uses, but it only changes which cost a sale
removes, not the picture, and the tax view isn't V1. Market value needs daily prices (V2).

### D-032: One local Streamlit app for balances and review; no stored aggregates, FastAPI later
*2026-11-25*

**Chose:** `make app`, a Streamlit app with two pages that call the services directly: Balances
(total, cash and positions at cost over time for all accounts, one institution or one account,
and the accounts side by side) and Review (a staged batch's flags, then commit or reject under
the CLI's rules). It serves on localhost only. History is computed from the ledger on each
request and cached by Streamlit until the next commit (`last_entry_id` is the cache key).
**Over:** a static HTML chart from the CLI (no way to switch views); FastAPI (an API, still
needing a frontend: it earns its place when another program reads the data, like the V3 agent);
a stored layer of daily balances (it would go stale after every import and repeat the logic).
**Why:** one app covers V1 items 5 and 6. Computing history takes milliseconds at this size;
revisit stored aggregates when it doesn't, or when a tool other than this code reads the database.

### D-033: Conventional Commits and one pull request template
*2026-11-26*

**Chose:** commit headers as `type(scope): description`, checked by a local commit-msg hook;
a body for why and a footer for `Refs: D-NNN`; one PR template; "Rebase and merge".
**Over:** free-form messages, as in most earlier commits, and squash merges.
**Why:** the history is read back to study and review changes: a type and scope make `git log`
filterable (`git log --grep '^fix(n26)'`), and rebase merging keeps each commit on `main`. A
local hook needs no dependency; the earlier commits stay as they are.
