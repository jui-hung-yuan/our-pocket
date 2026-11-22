"""CSV extraction: decode, detect the delimiter, fingerprint the header, read rows."""

import csv
import hashlib
import io

from our_pocket.etl.extract.base import ExtractionError, Extractor, FileSignature, RawFile
from our_pocket.etl.models import RawRow


def normalise_column(name: str) -> str:
    return name.strip().lower()


def header_fingerprint(columns: tuple[str, ...], delimiter: str) -> str:
    """Stable id of a header: column names (normalised, in order) plus the delimiter.

    Order is deliberately part of the fingerprint: a reordered export is a format
    change we want to notice and register, not silently absorb.
    """
    payload = delimiter + "\x1f" + "\x1f".join(normalise_column(c) for c in columns)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class CsvExtractor(Extractor):
    encodings: tuple[str, ...] = ("utf-8-sig", "cp1252")
    delimiters: tuple[str, ...] = (",", ";", "\t")

    def extract(self, content: bytes, name: str) -> RawFile:
        text, encoding = self._decode(content)
        if not text.strip():
            raise ExtractionError("file is empty")
        delimiter = self._detect_delimiter(text.splitlines()[0])
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        header = tuple(normalise_column(c) for c in next(reader))
        if len(set(header)) != len(header):
            raise ExtractionError("header has duplicate column names")

        rows: list[RawRow] = []
        for values in reader:
            if not any(v.strip() for v in values):
                continue
            if len(values) != len(header):
                raise ExtractionError(
                    f"line {reader.line_num}: {len(values)} values for {len(header)} columns"
                )
            rows.append(RawRow(reader.line_num, dict(zip(header, values, strict=True))))

        signature = FileSignature(
            file_sha256=hashlib.sha256(content).hexdigest(),
            file_size=len(content),
            encoding=encoding,
            delimiter=delimiter,
            columns=header,
            header_fingerprint=header_fingerprint(header, delimiter),
        )
        return RawFile(name, signature, rows)

    def _decode(self, content: bytes) -> tuple[str, str]:
        for encoding in self.encodings:
            try:
                return content.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        raise ExtractionError(f"not decodable as any of {', '.join(self.encodings)}")

    def _detect_delimiter(self, header_line: str) -> str:
        counts = {d: header_line.count(d) for d in self.delimiters}
        best = max(counts, key=lambda d: counts[d])
        if counts[best] == 0:
            raise ExtractionError("header has no recognised delimiter")
        return best
