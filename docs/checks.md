# Checks

One command runs everything: **`make check`**. GitHub Actions runs the same command on every pull
request and on every push to `main` (`.github/workflows/ci.yml`), against a real Postgres 17.
A pre-commit hook adds a local guard so real financial data is stopped before it reaches git.

```bash
make db        # once: Postgres in Docker (databases pocket + pocket_test)
make hooks     # once per clone: install the pre-commit guard and commit-message check
make check     # before pushing
```

## What `make check` runs

Fast checks come first, so failures show up quickly.

| Step | Command | Fails when | Needs DB |
|---|---|---|---|
| `lint` | `ruff check` + `ruff format --check` | Lint errors or unformatted code | no |
| `typecheck` | `mypy` (strict, on `src/` and `migrations/`) | Any type error | no |
| `guard` | `scripts/guard_real_data.py --all` | Any file git would commit looks like a real export (see below) | no |
| `boundaries` | `pytest -m boundaries` (`tests/repo/test_architecture.py`) | A layer imports a layer above it, or `etl/extract` / `etl/transform` touch the database (rules in [architecture.md](architecture.md)) | no |
| `migrations` | `alembic upgrade head` → `alembic check` → `downgrade base` → `upgrade head` | Migrations don't apply, `db/schema.py` changed without a new migration, or a downgrade is broken | yes |
| `test` | `pytest -m "not boundaries"` | Any test fails | yes, for `db`-marked tests |

**Databases:** `migrations` and the DB tests use `TEST_DATABASE_URL` (default: the `pocket_test`
database from `make db`) and drop tables there. They never touch `DATABASE_URL`, which may hold
real data.

**Skipping vs failing:** locally, DB tests *skip* when Postgres is down so you can still run the
rest. CI sets `REQUIRE_DB=1`, which turns that skip into a failure, so CI can't pass without them.
`migrations` always needs the database.

Each step also runs on its own: `make lint`, `make typecheck`, `make guard`, `make boundaries`,
`make migrations`, `make test`.

## Real-data guard

`scripts/guard_real_data.py` runs in two places:

- **pre-commit hook** (after `make hooks`): checks the files being committed;
- **`make check` / CI**: checks every file git would commit (`git ls-files --cached --others
  --exclude-standard`), so `git commit --no-verify` doesn't get real data past CI.

A file is **blocked** when:

| Rule | Example |
|---|---|
| Statement-like extension (`.csv .tsv .pdf .xls .xlsx`) outside `data/synthetic/` | `tests/fixtures/export.csv` |
| Anything under `data/` except `data/synthetic/` and `data/README.md` | `data/raw/n26/….csv` (also git-ignored) |
| Content looks like an export, whatever the name: PDF magic bytes, or a first line matching a registered institution's header fingerprint | a Trade Republic export renamed to `notes.txt` |
| A CSV under `data/synthetic/` that isn't byte-identical to its generator's output | a real export copied into `data/synthetic/` |

The last rule works because every committed synthetic file is listed in
`our_pocket.synthetic.FIXTURES` together with the function that generates it. To add or refresh
synthetic data: add the entry there, then run `make synthetic`.

These are **defence in depth** next to `.gitignore` (which ignores `data/` except
`data/synthetic/`), not a replacement for it. The rules are tested in `tests/repo/test_guard.py`,
including a test that the repository is clean.

**If the guard blocks a commit:** move the file to `data/raw/` (ignored), or regenerate synthetic
data with `make synthetic`. Don't bypass it. If a rule is wrong, change the rule and its test.

## Commit messages and pull requests

The commit-msg hook (`scripts/check_commit_message.py`, after `make hooks`) refuses a header
that isn't `type(scope): description` ([D-033](decisions.md#d-033-conventional-commits-and-one-pull-request-template)):

```text
fix(n26): type Authorization by sign, cash meaning unverified

Why the change was needed, and what it means for data already imported.

Refs: D-030
Co-Authored-By: ...
```

- **type:** one of the `TYPES` in the script; `!` after the scope marks a breaking change.
- **scope:** the area touched, lower-case: `n26`, `trade-republic`, `cli`, `web`, `ledger`,
  `ci`. Leave it out only when a change has no single area.
- **body:** why, wrapped at 72. **Footer:** `Refs: D-NNN`, `BREAKING CHANGE: ...`, co-authors.

A pull request starts from `.github/pull_request_template.md`. Merge with "Rebase and merge" so
each commit stays in `main`'s history.

## Type checking scope

`mypy --strict` covers `src/` and `migrations/`. Tests aren't type-checked: pytest fixtures are
untyped by nature, and running the tests already exercises that code.

## Adding a check

Add a Makefile target, put it in the `check:` prerequisite list (fast checks first), add a row to
the table above. CI picks it up automatically, since it only runs `make check`.
