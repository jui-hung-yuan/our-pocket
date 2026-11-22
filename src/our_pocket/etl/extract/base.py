"""Extract: read a source document into raw rows without interpreting any value.

Getting the file (downloading the export from the bank's app) happens before this,
by hand. Extraction starts from the file's bytes.

One Extractor per file *format* (CSV now; PDF later), not per institution: Trade
Republic and N26 both export CSV and differ only in what the columns mean, which is
the transformer's job.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from our_pocket.etl.models import RawRow


class ExtractionError(Exception):
    """The file can't be read as rows at all (encoding, ragged lines, empty)."""


@dataclass(frozen=True)
class FileSignature:
    """What identifies a file and its format, before any value is trusted."""

    file_sha256: str
    file_size: int
    encoding: str
    delimiter: str
    columns: tuple[str, ...]
    header_fingerprint: str


@dataclass(frozen=True)
class RawFile:
    name: str
    signature: FileSignature
    rows: list[RawRow]


class Extractor(ABC):
    @abstractmethod
    def extract(self, content: bytes, name: str) -> RawFile: ...

    def extract_path(self, path: Path) -> RawFile:
        return self.extract(path.read_bytes(), path.name)
