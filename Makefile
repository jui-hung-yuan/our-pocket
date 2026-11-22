.PHONY: check lint typecheck guard boundaries migrations test fmt hooks db migrate synthetic demo

# Where `make migrations` and pytest run. Both drop and recreate tables here,
# so this must never point at the database holding your real data.
TEST_DATABASE_URL ?= postgresql+psycopg://pocket:pocket@localhost:5432/pocket_test
export TEST_DATABASE_URL

## --- the one command: everything CI runs on every push ------------------------------
check: lint typecheck guard boundaries migrations test  ## run all checks
	@echo "all checks passed"

lint:  ## ruff lint + formatting
	uv run ruff check .
	uv run ruff format --check .

typecheck:  ## mypy --strict on src/ and migrations/
	uv run mypy

guard:  ## no real CSV/PDF exports among the files git would commit
	uv run python scripts/guard_real_data.py --all

boundaries:  ## layer rules: interfaces → services → etl → db; extract/transform pure
	uv run pytest -q -m boundaries

migrations:  ## migrations apply, match db/schema.py, and downgrade cleanly
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic check
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic downgrade base
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head

test:  ## pytest (DB tests skip if Postgres is down, unless REQUIRE_DB=1)
	uv run pytest -q -m "not boundaries"

## --- development --------------------------------------------------------------------
fmt:
	uv run ruff format .
	uv run ruff check --fix .

hooks:  ## install the pre-commit guard into .git/hooks
	uv run pre-commit install

db:  ## start Postgres (app db `pocket` + `pocket_test`)
	docker compose up -d --wait

migrate:  ## apply migrations to DATABASE_URL (the app database)
	uv run alembic upgrade head

synthetic:  ## regenerate every committed file under data/synthetic/
	uv run our-pocket synth fixtures

demo: migrate  ## end-to-end on synthetic data
	-uv run our-pocket account add "TR Demo" --institution trade_republic --owner Alex
	uv run our-pocket import data/synthetic/trade_republic/transactions_seed42.csv --account "TR Demo"
	@echo 'review the flags above, then: uv run our-pocket batch commit <id> --accept-warnings'
