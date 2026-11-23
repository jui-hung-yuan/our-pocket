"""Database schema (SQLAlchemy Core). Alembic migrations are generated from this.

Data layers, oldest to most trusted (see PROJECT_PLAN.md, "Data layers"):

    L0  source file      the export on disk; identified by sha256, never modified
    L1  raw.raw_row      every CSV row exactly as read (strings), one per data line;
                         UPDATE, DELETE and TRUNCATE are refused by a trigger
    L2  staging.*        import_batch, the parser's typed staged_row, and import_flag rows
    L3  core.*           accounts and the committed, deduplicated ledger_entry reports read

Status-like columns are TEXT with CHECK constraints instead of Postgres ENUMs, so
adding a value is a one-line migration (see docs/decisions.md).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeEngine

# Postgres schemas, one per trust level. Only `raw` is enforced immutable (migration 0002);
# import_batch lives in staging because review updates its status.
RAW = "raw"
STAGING = "staging"
CORE = "core"
SCHEMAS = (RAW, STAGING, CORE)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

BATCH_STATUSES = ("staged", "committed", "rejected")
FLAG_SEVERITIES = ("error", "warning", "info")

# Normalised entry types shared by every institution. The source's own type is kept
# alongside in `source_type`, so nothing is lost by the mapping.
ENTRY_TYPES = (
    "buy",
    "sell",
    "dividend",
    "interest",
    "deposit",
    "withdrawal",
    "tax_adjustment",
    "tax_base",
    "bonus",
    "card_payment",
    "card_refund",
    "transfer_in",
    "transfer_out",
    "space_transfer_in",
    "space_transfer_out",
    "direct_debit",
    "fee",
    "tax",
)

QTY = Numeric(28, 10)
MONEY = Numeric(20, 6)
RATE = Numeric(20, 10)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _id() -> Column[int]:
    return Column("id", BigInteger, Identity(), primary_key=True)


def _created_at() -> Column[datetime]:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


# --- Who owns what -------------------------------------------------------------------
# household_id / owner_person_id exist from day one so row-level security (V3) needs
# no data migration, even though V1 has a single user.

# Used when no household is named; V1 has one.
DEFAULT_HOUSEHOLD = "household"

household = Table(
    "household",
    metadata,
    _id(),
    Column("name", Text, nullable=False, unique=True),
    _created_at(),
    schema=CORE,
)

person = Table(
    "person",
    metadata,
    _id(),
    Column("household_id", BigInteger, ForeignKey("core.household.id"), nullable=False),
    Column("name", Text, nullable=False),
    _created_at(),
    UniqueConstraint("household_id", "name"),
    schema=CORE,
)

account = Table(
    "account",
    metadata,
    _id(),
    Column("household_id", BigInteger, ForeignKey("core.household.id"), nullable=False),
    # NULL owner means a joint account, visible to the whole household.
    Column("owner_person_id", BigInteger, ForeignKey("core.person.id")),
    Column("institution", Text, nullable=False),
    Column("name", Text, nullable=False, unique=True),
    Column("currency", Text, nullable=False, server_default="EUR"),
    # The account's name inside the export, for formats that hold several accounts per
    # file (N26 spaces, in the `Account Name` column), checked on import. NULL when the
    # export has none. Not unique: two members' spaces share default names ("Main Account").
    Column("export_account_name", Text),
    _created_at(),
    schema=CORE,
)

# --- Import pipeline (L0-L2) ---------------------------------------------------------

import_batch = Table(
    "import_batch",
    metadata,
    _id(),
    Column("account_id", BigInteger, ForeignKey("core.account.id"), nullable=False),
    Column("file_name", Text, nullable=False),
    Column("file_sha256", Text, nullable=False),
    Column("file_size", BigInteger, nullable=False),
    Column("encoding", Text, nullable=False),
    Column("delimiter", Text, nullable=False),
    Column("header_fingerprint", Text, nullable=False),
    # NULL when the header matched no known schema (batch is rejected at intake).
    Column("schema_id", Text),
    Column("schema_version", Integer),
    Column("parser_version", Text),
    Column("status", Text, nullable=False),
    Column("row_count", Integer, nullable=False, server_default="0"),
    Column("warnings_accepted", Boolean, nullable=False, server_default=text("false")),
    Column("decision_note", Text),
    _created_at(),
    Column("decided_at", DateTime(timezone=True)),
    CheckConstraint(_in("status", BATCH_STATUSES), name="status"),
    schema=STAGING,
)
# The same file may be imported again only after its earlier batch was rejected.
Index(
    "uq_import_batch_active_file",
    import_batch.c.account_id,
    import_batch.c.file_sha256,
    unique=True,
    postgresql_where=import_batch.c.status != "rejected",
)

raw_row = Table(
    "raw_row",
    metadata,
    _id(),
    Column("batch_id", BigInteger, ForeignKey("staging.import_batch.id"), nullable=False),
    # Line number in the file (header is line 1), so a flag points at the exact line.
    Column("line_number", Integer, nullable=False),
    Column("data", JSONB, nullable=False),
    UniqueConstraint("batch_id", "line_number"),
    schema=RAW,
)


def _entry_columns(*, staged: bool) -> list[Column[Any]]:
    """Typed transaction fields shared by staged_row (all nullable) and ledger_entry."""

    def col(
        name: str, type_: TypeEngine[Any] | type[TypeEngine[Any]], *, required: bool = False
    ) -> Column[Any]:
        return Column(name, type_, nullable=staged or not required)

    return [
        col("external_id", Text, required=True),
        col("booked_at", DateTime(timezone=True), required=True),
        col("booking_date", Date, required=True),
        col("entry_type", Text, required=True),
        col("source_type", Text, required=True),
        col("isin", Text),
        col("instrument_name", Text),
        col("asset_class", Text),
        col("quantity", QTY),
        col("price", QTY),
        col("amount", MONEY, required=True),
        col("fee", MONEY, required=True),
        col("tax", MONEY, required=True),
        # Net cash change of the account. NULL while the cash meaning of a source type
        # is unverified (flagged `cash_semantics_unverified` at import).
        col("cash_effect", MONEY),
        col("currency", Text, required=True),
        col("original_amount", MONEY),
        col("original_currency", Text),
        col("fx_rate", RATE),
        col("description", Text),
        col("counterparty_name", Text),
        col("counterparty_iban", Text),
    ]


staged_row = Table(
    "staged_row",
    metadata,
    _id(),
    Column("batch_id", BigInteger, ForeignKey("staging.import_batch.id"), nullable=False),
    Column("raw_row_id", BigInteger, ForeignKey("raw.raw_row.id"), nullable=False, unique=True),
    Column("line_number", Integer, nullable=False),
    *_entry_columns(staged=True),
    # Set at staging when the external_id is already in the ledger; such rows are
    # skipped on commit, which makes re-importing an overlapping export safe.
    # Holds a ledger_entry.id; no FK, because ledger_entry already references
    # staged_row and a cycle would complicate migrations.
    Column("duplicate_of", BigInteger),
    schema=STAGING,
)

import_flag = Table(
    "import_flag",
    metadata,
    _id(),
    Column(
        "batch_id", BigInteger, ForeignKey("staging.import_batch.id"), nullable=False, index=True
    ),
    # NULL for batch-level flags (unknown schema, wrong institution, ...).
    Column("staged_row_id", BigInteger, ForeignKey("staging.staged_row.id")),
    Column("line_number", Integer),
    Column("code", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("field", Text),
    Column("message", Text, nullable=False),
    CheckConstraint(_in("severity", FLAG_SEVERITIES), name="severity"),
    schema=STAGING,
)

# --- Ledger (L3) ---------------------------------------------------------------------

ledger_entry = Table(
    "ledger_entry",
    metadata,
    _id(),
    Column("household_id", BigInteger, ForeignKey("core.household.id"), nullable=False),
    Column("account_id", BigInteger, ForeignKey("core.account.id"), nullable=False),
    Column("batch_id", BigInteger, ForeignKey("staging.import_batch.id"), nullable=False),
    Column(
        "staged_row_id",
        BigInteger,
        ForeignKey("staging.staged_row.id"),
        nullable=False,
        unique=True,
    ),
    *_entry_columns(staged=False),
    _created_at(),
    UniqueConstraint("account_id", "external_id"),
    CheckConstraint(_in("entry_type", ENTRY_TYPES), name="entry_type"),
    schema=CORE,
)
