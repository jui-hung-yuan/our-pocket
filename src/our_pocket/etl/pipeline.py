"""ImportPipeline: Extract → identify format → Transform → Load (stage).

    bytes ─Extractor─▶ RawFile ─FormatRegistry─▶ Transformer ─▶ ParsedRows ─BatchStore─▶ batch

Committing is deliberately not part of `run`: a human reviews the flags first.
"""

from pathlib import Path

from sqlalchemy import Connection

from our_pocket.etl.extract import CsvExtractor, Extractor
from our_pocket.etl.load import BatchStore
from our_pocket.etl.models import Account, Flag
from our_pocket.etl.registry import FormatRegistry, default_registry


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

    def run_path(self, path: Path, account: Account) -> int:
        return self.run(path.read_bytes(), path.name, account)

    def run(self, content: bytes, file_name: str, account: Account) -> int:
        """Stage one file into `account`. Returns the batch id; never touches the ledger."""
        raw_file = self.extractor.extract(content, file_name)  # E
        self.store.ensure_not_loaded(account, raw_file)

        transformer = self.registry.match(raw_file.signature)
        if transformer is None:
            flag = Flag(
                "unknown_schema", "error", self.registry.explain_mismatch(raw_file.signature)
            )
            return self.store.reject_at_intake(account, raw_file, flag)
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
