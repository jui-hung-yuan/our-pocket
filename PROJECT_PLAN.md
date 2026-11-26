# our-pocket: project plan

*Started 2026-11-19. Decisions and their reasons are in [docs/decisions.md](docs/decisions.md).*

> **Scope change: CSV only.** V1 reads institutions' own **CSV exports** only. Trade Republic
> now offers a transaction CSV export, so the PDF and LLM-extraction path planned
> originally moves out of V1. See [D-002](docs/decisions.md#d-002).

## 1. Problems

The spine is P1 + P2: one trustworthy ledger. Everything later (categorisation, subscriptions, Q&A)
is only as good as the ledger underneath.

| # | Problem | User | Input | Output | Success check |
|---|---|---|---|---|---|
| **P1** | **Trustworthy ingestion of institution exports** | A household with accounts at German banks and brokers | Trade Republic transaction CSV, N26 CSV, each in whatever format version the institution ships that month | Typed, validated ledger rows (date, type, ISIN, quantity, price, amount, fee, tax, currency, FX), each traceable to its file and line | 100% field accuracy on synthetic ground truth; **0 unflagged errors** on planted perturbations; every unknown format refused at intake, never guessed; positions match the broker's own position list |
| **P2** | **Ledger consolidation** | Same household | Ledger rows from all accounts | One ledger where moves between N26 spaces are flagged and overlapping exports don't double-count | Every planted space move flagged, nothing else; re-importing any export changes nothing; net worth never double-counts |

P3 (categorisation), P4 (subscription detection) and P5 (household Q&A agent) come in V2 and V3.

## 2. V1: Trustworthy ledger

**In scope**

1. Trade Republic transaction CSV → ledger ✅
2. N26 CSV → ledger, one account per space ✅
3. Flag moves between N26 spaces (P2); legs aren't paired ([D-021](docs/decisions.md#d-021-flag-moves-between-n26-spaces-dont-pair-transfer-legs)) ✅
4. Eval suites on synthetic data ✅; real-data reconciliation ✅, first run on own exports matched (§6)
5. One chart: cash + positions over time (at cost or at reported valuations; daily prices are V2)
6. A review step for flagged batches (CLI now; maybe one Streamlit page)
7. README with the eval results and the 60-second pitch

**Won't do in V1**

- PDFs of any kind, and so no LLM extraction (see D-002)
- Scalable Capital and comdirect
- Categorisation, subscription detection
- Multiple users or row-level security (the `household_id` and `owner_person_id` columns exist already, see D-011)
- MCP server, agent, Q&A
- Provider fallback, drift detection, cost dashboards
- Daily market prices
- AWS or any deployment; Airflow/Dagster-style orchestration
- Any UI beyond one chart and the review step
- Partial batch commits (see D-006)

## 3. Data layers

```text
L0  source file     data/raw/…  (git-ignored)    sha256, size, encoding, delimiter, header fingerprint
         │                                         → import_batch
L1  raw_row         every data line as {column: string} (JSONB), plus its line number
         │  parser (versioned)
L2  staged_row      typed fields, entry_type, cash_effect, duplicate_of
    import_flag     error / warning / info, pointing at batch, line and field
         │  commit (only if no errors and warnings were accepted)
L3  ledger_entry    unique (account_id, external_id); links to staged_row → raw_row → batch → file
```

- **L0 and L1 are lossless and immutable.** A parser bug is fixed by re-parsing, not by re-exporting.
  Each layer has its own Postgres schema (`raw`, `staging`, `core`), and a trigger refuses any
  change to `raw.raw_row` (D-017).
- **L2 is disposable.** It is the parser's opinion, stamped with schema and parser version.
- **L3 is the only layer reports read.** It never holds a row that hasn't passed review, and every
  entry can be traced back to its file and line (tested in `tests/etl/test_pipeline.py`).

## 4. Schema versioning

Two kinds of schema change, handled separately:

**Institution file formats** (the export changes under you):

- On intake, each file gets a **header fingerprint**: sha256 of the normalised column names *in order*
  plus the delimiter. BOM, case and surrounding whitespace don't count.
- `etl/registry.py` maps fingerprints to a `Transformer` subclass that declares its institution,
  schema version and exact columns. Trade Republic's current export is
  `trade_republic.transactions_csv` **v1** (`TradeRepublicV1`). Code layout: [docs/architecture.md](docs/architecture.md).
- **Unknown fingerprint → batch rejected at intake** with an `unknown_schema` flag that explains the
  difference from the closest known schema (missing/extra columns, delimiter, order). Nothing is parsed.
- A new format means **a new registry entry with version + 1**, never an edit to an old one. It can
  reuse the old parser if only the order changed.
- **Parser version** is separate from schema version and is stored on every batch, so we can find all
  ledger rows produced by a parser that later turned out to be wrong.
- Value-level drift that the header can't show (a new `type`, a new `account_type`, a decimal comma)
  is caught by row flags (`unknown_type`, `unexpected_account_type`, `unparseable_value`).

**Our own database**: SQLAlchemy Core tables in `db/schema.py`, Alembic migrations in `migrations/`.
`make migrations` fails if the schema and the migrations drift apart.

## 5. Import-batch flow

```text
import FILE --account A
  ├─ unreadable (encoding, ragged rows)        → error, no batch
  ├─ same sha256 already staged/committed     → refused (DuplicateFile)
  ├─ unknown fingerprint                      → batch REJECTED at intake  [unknown_schema]
  ├─ schema institution ≠ account institution → batch REJECTED at intake  [institution_mismatch]
  └─ known schema → raw rows + parsed rows + flags → batch STAGED
                                                         │
             batch show N    (flags grouped by code, with line numbers)
                                                         │
          ┌──────────────────────────────┬──────────────────────────────────┐
   any error flag                 only warnings                        clean
   commit refused;              commit --accept-warnings              commit
   reject, fix, re-import                │                               │
                                         └──────────► COMMITTED ◄────────┘
                                   rows already in ledger are skipped (idempotent)
```

| Severity | Meaning | Effect |
|---|---|---|
| `error` | The row can't be trusted as read | Blocks the whole batch |
| `warning` | Plausible but unverified | Commit needs `--accept-warnings`, recorded on the batch |
| `info` | Audit trail (e.g. `already_in_ledger`) | None |

Trade Republic v1 flag catalogue: `missing_field`, `unparseable_value`, `unknown_type`,
`unexpected_sign`, `trade_arithmetic` (|amount| = |shares| × price ± 0.01), `fx_incomplete`,
`fx_mismatch` (original × fx_rate = amount ± 0.01), `invalid_isin` (check digit), `invalid_iban`
(mod 97), `invalid_currency`, `unexpected_account_type`, `duplicate_in_file`,
`tax_row_has_amount`, `cash_semantics_unverified` (`STOCKPERK` only), `already_in_ledger`.

**Idempotency:** the same file twice is refused; an overlapping export (e.g. Jan–Jun, then Jan–Dec) only
adds new `transaction_id`s. Both are tested.

## 6. Evaluation plan

CSV-only V1 has no LLM, so the evaluation is about **the pipeline's honesty**: does it read every
field correctly, and does it flag *every* row it can't trust? The guide's 20–50 case idea becomes:

| Suite | Cases | Ground truth | Metric | Target |
|---|---|---|---|---|
| **Field accuracy** | Synthetic exports, 5 seeds × 14 months, both institutions; together every Trade Republic type the generator writes | Generator's own bookkeeping (holdings, cash, balances per space), not the parser's output | Field-level accuracy; positions and cash = generator's | 100% |
| **Perturbation (row level)** | ~50 planted defects: wrong sign, broken arithmetic, bad ISIN/IBAN, decimal comma, missing FX field, unknown type, duplicate id, … Every row flag the parsers can raise has one (tested) | Each defect's expected flag | **Unflagged errors**; false flags on the other rows of the file | 0 unflagged; 0 false flags |
| **Blind spots** | Defects no row check can see (a deposit amount changed; shares and amount scaled together) | Asserted to pass unflagged, each naming what catches it instead | — | listed, not hidden |
| **Schema drift (file level)** | Renamed, added, dropped and reordered columns; `;` and tab delimiters | Refused at intake, explanation names the change | Detection rate; explanation correct | 100% |
| **Not drift** | BOM, header case and spaces, CRLF, other quoting, cp1252 | Read exactly like the clean file | Parsed rows equal | 100% |
| **Idempotency** | Re-import, overlapping exports, out-of-order imports, the same rows re-saved as other bytes | Ledger row count, positions, cash | Ledger unchanged on re-import | exact |
| **Space transfers (P2)** | Moves between N26 spaces, incl. one-legged moves from a member's space that isn't imported; external transfers and MoneyBeam | The moves the generator made | Rows typed `space_transfer_*` vs planted | exact |
| **Real-data reconciliation** (private) | Own exports | The cash balance and position list shown in the app, typed into a snapshot file | Position diff per ISIN; cash diff | 0 (results stay local) |

The synthetic suites are pytest files in `tests/eval/`, so they run in `make check`
([D-025](docs/decisions.md#d-025-eval-suites-are-tests-not-a-separate-harness)). Reconciliation:

```bash
# data/raw/trade_republic/app_<date>.json, typed from the app on the day of the export:
# {"account": "TR", "as_of": "2026-11-24", "cash": "1234.56", "positions": {"<ISIN>": "12.3456"}}
uv run our-pocket reconcile data/raw/trade_republic/app_<date>.json
```

It counts entries booked up to `as_of`, and says whether counting `STOCKPERK` amounts as cash
would close a cash gap, which settles Q1 ([D-026](docs/decisions.md#d-026-reconcile-against-a-hand-typed-app-snapshot)).

- Synthetic generator rules: invented ISINs and IBANs with valid check digits, the real export's quoting
  and number formats, mixed timestamp precisions, late trades booked on the next local day.
- **Circularity caveat:** the generator encodes my reading of the format. Real-data reconciliation is
  the check on that reading. It already caught one mistake: the generator put tax-only rows' money
  in `amount` (D-016). `STOCKPERK` is where my reading is still unproven.
- **CI:** `make check` on every push: lint, mypy, real-data guard, layer boundaries, migration
  round trip, and all suites on synthetic data (`REQUIRE_DB=1`, so DB tests can't silently skip).
  Real-data runs and their results stay local. See [docs/checks.md](docs/checks.md).

## 7. Version ladder

| Version | Scope | Payoff |
|---|---|---|
| **V1: Trustworthy ledger** | §2 | Cash + positions chart from real data, reconciled to the broker |
| **V2: Categorise and value** | Categorisation (classical baseline vs LLM on low-confidence rows, cost per 1,000 rows); daily prices; comdirect CSV; PDF/LLM extraction reconsidered with the CSV parser as ground truth | Benchmark table: when GenAI beats rules |
| **V3: Household + agent** | Row-level security; read-only MCP/SQL tools; Q&A eval; subscriptions | "What did we spend on subscriptions in Q3?" |

**V3 prerequisites and shape:**

- Answers are only right if spending leaves out `space_transfer_*` rows (P2, V1) and rows are
  categorised (V2); otherwise moves between N26 spaces count as spending.
- Each entry point (slash command, MCP server, Slack bot) is a thin `interfaces/` adapter over
  `LedgerService`; question-shaped methods added there (balance on a date, entries by account and
  period) become the agent's tools.
- Agent SQL access, if any, runs as a read-only Postgres role limited to the `core` schema.
- Entry point order: local MCP server + Claude Code slash command first, Slack later (Q7).

## 8. Open questions

| # | Question | Status |
|---|---|---|
| Q1 | Cash meaning of `EARNINGS`, `PRE_DETERMINED_TAX_BASE`, `TAX_OPTIMIZATION`, `STOCKPERK` (and N26 `Authorization`): is `amount` cash, or only `tax`? Settle by reconciling against the app's cash balance | answered for the three tax types: `amount` is 0, `tax` is cash ([D-016](docs/decisions.md#d-016-tax-only-rows-move-cash-through-tax-parser-v2)); `STOCKPERK` open; also N26 `Authorization` ([D-030](docs/decisions.md#d-030-n26-authorization-is-typed-by-sign-its-cash-meaning-unverified-n26-parser-3)) |
| Q2 | Is `TRANSFER_INBOUND` vs `CUSTOMER_INBOUND` meaningful for transfer matching (P2)? | moot: legs aren't paired ([D-021](docs/decisions.md#d-021-flag-moves-between-n26-spaces-dont-pair-transfer-legs)) |
| Q3 | Do Trade Republic exports overlap between months, or is each a clean partition? (The pipeline handles both) | open |
| Q4 | N26 export: one file per account/space, all with the same header? | answered: same header; mostly one file per space, but a file can include rows of a deleted space; files are imported per space and `Account Name` is checked ([D-022](docs/decisions.md#d-022-n26-imports-name-their-account-account-name-is-only-checked)) |
| Q5 | Timebox for V1 | open |
| Q6 | Consent from each household member before their accounts are imported | open |
| Q7 | Slack access sends ledger data to Slack and the LLM provider and needs hosting: worth it over a local MCP server + slash command? Needs Q6 settled first | open |
