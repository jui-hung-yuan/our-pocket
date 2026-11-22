"""Load: write import batches to the database, then commit or reject them.

Loading has two phases. `stage` writes the raw rows (L1), typed rows (L2) and flags,
and never touches the ledger. `commit` promotes a reviewed batch's rows into
ledger_entry (L3). Every ledger entry keeps a link to its staged row, raw row and file.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Row, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from our_pocket.db.schema import (
    account,
    import_batch,
    import_flag,
    ledger_entry,
    raw_row,
    staged_row,
)
from our_pocket.etl.extract import RawFile
from our_pocket.etl.models import ENTRY_FIELDS, Account, Flag, ParsedRow
from our_pocket.etl.transform import Transformer


class BatchError(Exception):
    pass


class DuplicateFile(BatchError):
    pass


class InvalidBatchState(BatchError):
    pass


class CommitBlocked(BatchError):
    pass


@dataclass
class BatchReport:
    batch_id: int
    status: str
    account: str
    file_name: str
    schema_id: str | None
    schema_version: int | None
    parser_version: str | None
    row_count: int
    duplicate_count: int
    flag_counts: Counter[str] = field(default_factory=Counter)
    flags: list[dict[str, Any]] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.flag_counts["error"]

    @property
    def warnings(self) -> int:
        return self.flag_counts["warning"]


@dataclass
class CommitResult:
    batch_id: int
    inserted: int
    skipped_duplicates: int


class BatchStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    # --- staging ------------------------------------------------------------------

    def ensure_not_loaded(self, account: Account, raw_file: RawFile) -> None:
        """The same bytes may be staged again only after the earlier batch was rejected."""
        existing = self.conn.execute(
            select(import_batch.c.id).where(
                import_batch.c.account_id == account.id,
                import_batch.c.file_sha256 == raw_file.signature.file_sha256,
                import_batch.c.status != "rejected",
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise DuplicateFile(
                f"this exact file is already batch {existing}; reject it first to re-import"
            )

    def reject_at_intake(self, account: Account, raw_file: RawFile, flag: Flag) -> int:
        """Record a file that was refused before transformation (audit trail)."""
        batch_id = self._insert_batch(
            account,
            raw_file,
            status="rejected",
            decision_note=f"rejected at intake: {flag.code}",
            decided_at=func.now(),
        )
        self.conn.execute(insert(import_flag).values(self._flag_values(batch_id, flag)))
        return batch_id

    def stage(
        self, account: Account, raw_file: RawFile, transformer: Transformer, parsed: list[ParsedRow]
    ) -> int:
        batch_id = self._insert_batch(
            account,
            raw_file,
            schema_id=transformer.schema_id,
            schema_version=transformer.schema_version,
            parser_version=transformer.parser_version,
            status="staged",
            row_count=len(raw_file.rows),
        )
        if not raw_file.rows:
            return batch_id

        raw_ids = (
            self.conn.execute(
                insert(raw_row).returning(raw_row.c.id, sort_by_parameter_order=True),
                [
                    {"batch_id": batch_id, "line_number": r.line_number, "data": r.data}
                    for r in raw_file.rows
                ],
            )
            .scalars()
            .all()
        )

        in_ledger = self._existing_ledger_ids(account, [p.external_id for p in parsed])
        staged_ids = (
            self.conn.execute(
                insert(staged_row).returning(staged_row.c.id, sort_by_parameter_order=True),
                [
                    {
                        "batch_id": batch_id,
                        "raw_row_id": raw_id,
                        "line_number": p.line_number,
                        "duplicate_of": in_ledger.get(p.external_id or ""),
                        **{f: getattr(p, f) for f in ENTRY_FIELDS},
                    }
                    for raw_id, p in zip(raw_ids, parsed, strict=True)
                ],
            )
            .scalars()
            .all()
        )

        flags = []
        for staged_id, p in zip(staged_ids, parsed, strict=True):
            row_flags = list(p.flags)
            if p.external_id in in_ledger:
                row_flags.append(
                    Flag(
                        "already_in_ledger",
                        "info",
                        f"already ledger entry {in_ledger[p.external_id]}; skipped on commit",
                        "transaction_id",
                    )
                )
            flags += [
                self._flag_values(batch_id, f, staged_row_id=staged_id, line_number=p.line_number)
                for f in row_flags
            ]
        if flags:
            self.conn.execute(insert(import_flag), flags)
        return batch_id

    # --- decisions ----------------------------------------------------------------

    def commit(self, batch_id: int, *, accept_warnings: bool = False) -> CommitResult:
        batch = self._locked_batch(batch_id)
        if batch.status != "staged":
            raise InvalidBatchState(f"batch {batch_id} is {batch.status}, not staged")

        rows = self.conn.execute(
            select(import_flag.c.severity, func.count())
            .where(import_flag.c.batch_id == batch_id)
            .group_by(import_flag.c.severity)
        ).all()
        severities: Counter[str] = Counter({severity: n for severity, n in rows})
        if severities["error"]:
            raise CommitBlocked(
                f"batch {batch_id} has {severities['error']} error flag(s); fix the file and "
                "re-import, or reject the batch"
            )
        if severities["warning"] and not accept_warnings:
            raise CommitBlocked(
                f"batch {batch_id} has {severities['warning']} warning(s); review them and "
                "commit with accept_warnings to proceed"
            )

        household_id = self.conn.execute(
            select(account.c.household_id).where(account.c.id == batch.account_id)
        ).scalar_one()
        source = select(
            literal(household_id).label("household_id"),
            literal(batch.account_id).label("account_id"),
            staged_row.c.batch_id,
            staged_row.c.id.label("staged_row_id"),
            *(staged_row.c[f] for f in ENTRY_FIELDS),
        ).where(staged_row.c.batch_id == batch_id, staged_row.c.duplicate_of.is_(None))

        # ON CONFLICT covers rows committed by another batch after this one was staged.
        inserted = (
            self.conn.execute(
                pg_insert(ledger_entry)
                .from_select(
                    ["household_id", "account_id", "batch_id", "staged_row_id", *ENTRY_FIELDS],
                    source,
                )
                .on_conflict_do_nothing(index_elements=["account_id", "external_id"])
                .returning(ledger_entry.c.id)
            )
            .scalars()
            .all()
        )

        self.conn.execute(
            update(import_batch)
            .where(import_batch.c.id == batch_id)
            .values(status="committed", warnings_accepted=accept_warnings, decided_at=func.now())
        )
        return CommitResult(batch_id, len(inserted), batch.row_count - len(inserted))

    def reject(self, batch_id: int, reason: str) -> None:
        batch = self._locked_batch(batch_id)
        if batch.status != "staged":
            raise InvalidBatchState(f"batch {batch_id} is {batch.status}, not staged")
        self.conn.execute(
            update(import_batch)
            .where(import_batch.c.id == batch_id)
            .values(status="rejected", decision_note=reason, decided_at=func.now())
        )

    # --- reading ------------------------------------------------------------------

    def report(self, batch_id: int) -> BatchReport:
        row = self.conn.execute(
            select(import_batch, account.c.name.label("account_name"))
            .join(account, account.c.id == import_batch.c.account_id)
            .where(import_batch.c.id == batch_id)
        ).one_or_none()
        if row is None:
            raise BatchError(f"no batch {batch_id}")
        flags = [
            dict(f._mapping)
            for f in self.conn.execute(
                select(
                    import_flag.c.severity,
                    import_flag.c.code,
                    import_flag.c.line_number,
                    import_flag.c.field,
                    import_flag.c.message,
                )
                .where(import_flag.c.batch_id == batch_id)
                .order_by(import_flag.c.line_number.nulls_first(), import_flag.c.id)
            )
        ]
        duplicates = self.conn.execute(
            select(func.count()).where(
                staged_row.c.batch_id == batch_id, staged_row.c.duplicate_of.is_not(None)
            )
        ).scalar_one()
        return BatchReport(
            batch_id=batch_id,
            status=row.status,
            account=row.account_name,
            file_name=row.file_name,
            schema_id=row.schema_id,
            schema_version=row.schema_version,
            parser_version=row.parser_version,
            row_count=row.row_count,
            duplicate_count=duplicates,
            flag_counts=Counter(f["severity"] for f in flags),
            flags=flags,
        )

    # --- helpers ------------------------------------------------------------------

    def _insert_batch(self, account: Account, raw_file: RawFile, **values: Any) -> int:
        signature = raw_file.signature
        batch_id: int = self.conn.execute(
            insert(import_batch)
            .values(
                account_id=account.id,
                file_name=raw_file.name,
                file_sha256=signature.file_sha256,
                file_size=signature.file_size,
                encoding=signature.encoding,
                delimiter=signature.delimiter,
                header_fingerprint=signature.header_fingerprint,
                **values,
            )
            .returning(import_batch.c.id)
        ).scalar_one()
        return batch_id

    def _existing_ledger_ids(
        self, account: Account, external_ids: list[str | None]
    ) -> dict[str, int]:
        ids = [i for i in external_ids if i]
        return dict(
            self.conn.execute(
                select(ledger_entry.c.external_id, ledger_entry.c.id).where(
                    ledger_entry.c.account_id == account.id, ledger_entry.c.external_id.in_(ids)
                )
            ).all()
        )

    def _locked_batch(self, batch_id: int) -> Row[Any]:
        batch = self.conn.execute(
            select(import_batch).where(import_batch.c.id == batch_id).with_for_update()
        ).one_or_none()
        if batch is None:
            raise BatchError(f"no batch {batch_id}")
        return batch

    @staticmethod
    def _flag_values(
        batch_id: int,
        flag: Flag,
        *,
        staged_row_id: int | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "staged_row_id": staged_row_id,
            "line_number": line_number,
            "code": flag.code,
            "severity": flag.severity,
            "field": flag.field,
            "message": flag.message,
        }
