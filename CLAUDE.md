# Working on our-pocket

## Docs
- Lean: say it once, in one place, and link to it. No restating code, no filler, no marketing.
- README is the gist only: what it is, how to run it, where to read more. No lists that mirror
  the code (tests, modules, flags); they drift. Link to the file instead.
- Every decision goes in `docs/decisions.md` as D-NNN: Chose / Over / Why, a few lines.
- When code changes, update the doc that describes it in the same change, or delete the line.

## Names
- Full words for parameters and variables: `account`, `raw_file`, `instrument`, not `acct`,
  `raw`, `inst`. Short names only for tiny local scopes (`i`, `r` in a comprehension).
- Functions and methods are verbs that say what they do (`reject_at_intake`, `ensure_not_loaded`).
- Files and modules are named after the one thing they hold (`batch_store.py`, `trade_republic.py`).

## Privacy
- Real names, amounts, counts and export file names stay in `data/raw/` and `docs/private/`,
  scratch notes included. Committed files use invented ones.

## Imports
- An external id must not include values the source lets users edit (N26 space names). Decide
  its formula before real rows are committed: changing it later breaks deduplication.
- Migrations name constraints explicitly and pass names through `op.f()`, so the naming
  convention isn't applied twice.

## Before committing
- `make check` passes.
- Commit messages and PR descriptions follow [docs/checks.md](docs/checks.md#commit-messages-and-pull-requests).
- A feature branch gets an independent review before merge.
