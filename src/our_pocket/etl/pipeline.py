"""ImportPipeline: Extract → identify format → Transform → Load (stage).

    bytes ─Extractor─▶ RawFile ─FormatRegistry─▶ Transformer ─▶ ParsedRows ─BatchStore─▶ batch(es)

A file is one batch, except for formats whose rows name their account (N26 spaces):
those become one batch per account.

Committing is deliberately not part of `run`: a human reviews the flags first.
"""

from pathlib import Path

from sqlalchemy import Connection

from our_pocket.db.schema import DEFAULT_HOUSEHOLD
from our_pocket.etl.extract import CsvExtractor, Extractor, RawFile
from our_pocket.etl.load import (
    AccountNotAllowed,
    AccountRequired,
    BatchStore,
    DuplicateFile,
    UnknownExportAccount,
    UnknownFormat,
)
from our_pocket.etl.models import Account, Flag, RawRow
from our_pocket.etl.registry import FormatRegistry, default_registry
from our_pocket.etl.transform import Transformer


class ImportPipeline:
    def __init__(
        self,
        conn: Connection,
        *,
        extractor: Extractor | None = None,
        registry: FormatRegistry | None = None,
    ) -> None:
        self.extractor = extractor or CsvExtractor()
        self.registry = registry or default_registry()
        self.store = BatchStore(conn)

    def run_path(
        self,
        path: Path,
        account: Account | None = None,
        household_name: str = DEFAULT_HOUSEHOLD,
    ) -> list[int]:
        return self.run(path.read_bytes(), path.name, account, household_name)

    def run(
        self,
        content: bytes,
        file_name: str,
        account: Account | None = None,
        household_name: str = DEFAULT_HOUSEHOLD,
    ) -> list[int]:
        """Stage one file. Returns the batch ids (one per account); never touches the ledger.

        Formats with one account per file (Trade Republic) need `account`. Formats whose
        rows name their account (N26 spaces) must not get one: each row goes to the
        account of `household_name` whose export_account_name matches, one batch per account.
        """
        raw_file = self.extractor.extract(content, file_name)  # E
        transformer = self.registry.match(raw_file.signature)
        if transformer is None:
            explanation = self.registry.explain_mismatch(raw_file.signature)
            if account is None:
                raise UnknownFormat(explanation)
            self.store.ensure_not_loaded(account, raw_file)
            flag = Flag("unknown_schema", "error", explanation)
            return [self.store.reject_at_intake(account, raw_file, flag)]
        if transformer.account_column is not None:
            if account is not None:
                raise AccountNotAllowed(
                    f"{transformer.schema_id} names each row's account in "
                    f"{transformer.account_column!r}; import it without an account"
                )
            return self._stage_by_row_account(
                raw_file, transformer, transformer.account_column, household_name
            )
        if account is None:
            raise AccountRequired(f"{transformer.schema_id} files need an account to import into")
        return [self._stage_into(account, raw_file, transformer)]

    def _stage_into(self, account: Account, raw_file: RawFile, transformer: Transformer) -> int:
        self.store.ensure_not_loaded(account, raw_file)
        if transformer.institution != account.institution:
            flag = Flag(
                "institution_mismatch",
                "error",
                f"file is {transformer.institution}, "
                f"account {account.name!r} is {account.institution}",
            )
            return self.store.reject_at_intake(account, raw_file, flag)
        parsed = transformer.transform(raw_file.rows)  # T
        return self.store.stage(account, raw_file, transformer, parsed)  # L

    def _stage_by_row_account(
        self,
        raw_file: RawFile,
        transformer: Transformer,
        account_column: str,
        household_name: str,
    ) -> list[int]:
        rows_by_name: dict[str, list[RawRow]] = {}
        for raw in raw_file.rows:
            rows_by_name.setdefault(raw.data.get(account_column, "").strip(), []).append(raw)
        accounts = self.store.accounts_by_export_name(transformer.institution, household_name)
        unmatched = [name for name in rows_by_name if name not in accounts]
        if unmatched:
            raise UnknownExportAccount(transformer.institution, unmatched, household_name)
        # Every check passes before anything is written: a file is staged whole or not at all.
        # Spaces already committed from this file are skipped, so a rejected one can come back.
        pending: dict[str, list[RawRow]] = {}
        for name, rows in rows_by_name.items():
            loaded = self.store.find_loaded(accounts[name], raw_file)
            if loaded is None:
                pending[name] = rows
            elif loaded.status != "committed":
                raise DuplicateFile(
                    f"this exact file is already batch {loaded.id} ({loaded.status}) "
                    f"for {accounts[name].name!r}; reject it first to re-import"
                )
        if not pending:
            raise DuplicateFile("every space of this exact file is already committed")

        # Transform the whole file once: external ids number identical rows file-wide.
        parsed_by_line = {p.line_number: p for p in transformer.transform(raw_file.rows)}  # T
        return [
            self.store.stage(  # L
                accounts[name],
                raw_file,
                transformer,
                [parsed_by_line[raw.line_number] for raw in rows],
                rows=rows,
            )
            for name, rows in pending.items()
        ]
