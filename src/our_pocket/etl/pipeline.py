"""ImportPipeline: Extract → identify format → Transform → Load (stage).

    bytes ─Extractor─▶ RawFile ─FormatRegistry─▶ Transformer ─▶ ParsedRows ─BatchStore─▶ batch

A file is one batch in one account. Committing is deliberately not part of `run`: a human
reviews the flags first.
"""

from pathlib import Path

from sqlalchemy import Connection

from our_pocket.etl.extract import CsvExtractor, Extractor, RawFile
from our_pocket.etl.load import AccountRequired, BatchStore, UnknownFormat
from our_pocket.etl.models import Account, Flag, ParsedRow
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

    def run_path(self, path: Path, account: Account | None = None) -> int:
        return self.run(path.read_bytes(), path.name, account)

    def run(self, content: bytes, file_name: str, account: Account | None = None) -> int:
        """Stage one file into `account`. Returns the batch id; never touches the ledger."""
        raw_file = self.extractor.extract(content, file_name)  # E
        transformer = self.registry.match(raw_file.signature)
        if transformer is None:
            explanation = self.registry.explain_mismatch(raw_file.signature)
            if account is None:
                raise UnknownFormat(explanation)
            self.store.ensure_not_loaded(account, raw_file)
            flag = Flag("unknown_schema", "error", explanation)
            return self.store.reject_at_intake(account, raw_file, flag)
        if account is None:
            raise AccountRequired(f"{transformer.schema_id} files need an account to import into")
        return self._stage_into(account, raw_file, transformer)

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
        if transformer.account_column is not None:
            self._flag_other_accounts(account, raw_file, transformer.account_column, parsed)
        return self.store.stage(account, raw_file, transformer, parsed)  # L

    def _flag_other_accounts(
        self, account: Account, raw_file: RawFile, account_column: str, parsed: list[ParsedRow]
    ) -> None:
        """Rows naming another space: a deleted space's rows turn up in Main's export and
        belong there (warning). Rows of a space that is an account itself would count twice
        (error); a whole file of them means the wrong --account."""
        other_accounts = self.store.other_export_names(account)
        for raw, row in zip(raw_file.rows, parsed, strict=True):
            name = raw.data.get(account_column, "").strip()
            if name == account.export_account_name:
                continue
            if name in other_accounts:
                row.flag(
                    "other_account",
                    "error",
                    f"row is from {name!r}, which is account {other_accounts[name]!r}; "
                    "import it there",
                    account_column,
                )
            else:
                row.flag(
                    "unknown_space",
                    "warning",
                    f"row is from {name!r}, not {account.export_account_name!r}; accept only "
                    f"if that space is gone and its rows belong to {account.name!r}",
                    account_column,
                )
