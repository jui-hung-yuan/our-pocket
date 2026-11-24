"""Eval: schema drift at file level, for every registered institution.

A real format change (a column renamed, added, dropped or moved; another delimiter) must be
refused at intake, with an explanation that names the change. A difference that isn't a
format change (BOM, header case, line endings, quoting, cp1252) must be read exactly as the
clean file. Targets (PROJECT_PLAN §6): 100% detection, explanation correct.
"""

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from our_pocket.db.schema import raw_row
from our_pocket.etl.extract import CsvExtractor
from our_pocket.etl.load import BatchStore
from our_pocket.etl.models import ParsedRow
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.etl.registry import default_registry
from our_pocket.services import AccountService
from our_pocket.synthetic import n26 as synth_n26
from our_pocket.synthetic import trade_republic as synth_tr
from our_pocket.synthetic.trade_republic import COLUMNS_V1 as TRADE_REPUBLIC_HEADER

REGISTRY = default_registry()


@dataclass(frozen=True)
class Export:
    """A clean export as header + values, so drift can be applied to both."""

    institution: str
    schema_id: str
    header: list[str]
    rows: list[list[str]]
    text_column: str  # a free-text column, for the encoding cases
    quoting: int  # as the institution writes it


def _trade_republic() -> Export:
    rows = synth_tr.generate(seed=42).rows
    header = list(TRADE_REPUBLIC_HEADER)
    return Export(
        "trade_republic",
        "trade_republic.transactions_csv",
        header,
        [[r[c] for c in header] for r in rows],
        "description",
        csv.QUOTE_ALL,
    )


def _n26() -> Export:
    rows = synth_n26.generate(seed=42).space_rows(synth_n26.MAIN)
    header = list(synth_n26.HEADER)
    return Export(
        "n26",
        "n26.transactions_csv",
        header,
        [[r[c] for c in header] for r in rows],
        "Payment Reference",
        csv.QUOTE_MINIMAL,
    )


EXPORTS = {"trade_republic": _trade_republic(), "n26": _n26()}


def write(
    header: list[str],
    rows: list[list[str]],
    *,
    delimiter: str = ",",
    quoting: int = csv.QUOTE_MINIMAL,
    line_end: str = "\n",
    encoding: str = "utf-8",
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, quoting=quoting, lineterminator=line_end)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode(encoding)


# --- drift: refused, and explained --------------------------------------------------------

# name → (build the drifted file, what the explanation must say)
Drift = Callable[[Export], tuple[bytes, list[str]]]


def renamed(export: Export) -> tuple[bytes, list[str]]:
    old = export.header[-1]
    header = [*export.header[:-1], old + " renamed"]
    return write(header, export.rows), [
        f"missing columns: {old.lower()}",
        f"extra columns: {old.lower()} renamed",
    ]


def added(export: Export) -> tuple[bytes, list[str]]:
    rows = [[*r, ""] for r in export.rows]
    return write([*export.header, "New Column"], rows), ["extra columns: new column"]


def dropped(export: Export) -> tuple[bytes, list[str]]:
    gone = export.header[-1]
    return write(export.header[:-1], [r[:-1] for r in export.rows]), [
        f"missing columns: {gone.lower()}"
    ]


def reordered(export: Export) -> tuple[bytes, list[str]]:
    header = [export.header[1], export.header[0], *export.header[2:]]
    rows = [[r[1], r[0], *r[2:]] for r in export.rows]
    return write(header, rows), ["same columns in a different order"]


def semicolons(export: Export) -> tuple[bytes, list[str]]:
    return write(export.header, export.rows, delimiter=";"), ["delimiter ';' instead of ','"]


def tabs(export: Export) -> tuple[bytes, list[str]]:
    return write(export.header, export.rows, delimiter="\t"), ["delimiter '\\t' instead of ','"]


DRIFTS: dict[str, Drift] = {
    "renamed column": renamed,
    "added column": added,
    "dropped column": dropped,
    "reordered columns": reordered,
    "semicolon delimiter": semicolons,
    "tab delimiter": tabs,
}
DRIFT_CASES = [
    pytest.param(institution, drift, id=f"{institution}: {name}")
    for institution in EXPORTS
    for name, drift in DRIFTS.items()
]


@pytest.mark.parametrize("institution, drift", DRIFT_CASES)
def test_drift_is_refused_and_explained(institution, drift):
    export = EXPORTS[institution]
    content, explained = drift(export)
    signature = CsvExtractor().extract(content, "drifted.csv").signature
    assert REGISTRY.match(signature) is None
    explanation = REGISTRY.explain_mismatch(signature)
    assert explanation.startswith(f"closest known schema: {export.schema_id} v1")
    for fragment in explained:
        assert fragment in explanation


@pytest.mark.db
@pytest.mark.parametrize("institution, drift", DRIFT_CASES)
def test_drift_is_rejected_at_intake_with_nothing_parsed(conn, institution, drift):
    export = EXPORTS[institution]
    content, _ = drift(export)
    export_name = synth_n26.MAIN if institution == "n26" else None
    account = AccountService(conn).add(
        "Account", institution, owner="Alex", export_account_name=export_name
    )
    batch_id = ImportPipeline(conn).run(content, "drifted.csv", account)
    report = BatchStore(conn).report(batch_id)
    assert report.status == "rejected"
    assert [f["code"] for f in report.flags] == ["unknown_schema"]
    rows = select(func.count()).select_from(raw_row).where(raw_row.c.batch_id == batch_id)
    assert conn.execute(rows).scalar_one() == 0


# --- no drift: read exactly as the clean file ---------------------------------------------


def _read(content: bytes) -> list[ParsedRow]:
    raw_file = CsvExtractor().extract(content, "export.csv")
    transformer = REGISTRY.match(raw_file.signature)
    assert transformer is not None, REGISTRY.explain_mismatch(raw_file.signature)
    return transformer.transform(raw_file.rows)


def with_bom(export: Export) -> bytes:
    return b"\xef\xbb\xbf" + write(export.header, export.rows, quoting=export.quoting)


def noisy_header(export: Export) -> bytes:
    header = [f" {c.upper()} " for c in export.header]
    return write(header, export.rows, quoting=export.quoting)


def crlf(export: Export) -> bytes:
    return write(export.header, export.rows, quoting=export.quoting, line_end="\r\n")


def other_quoting(export: Export) -> bytes:
    """Trade Republic quotes every value and N26 doesn't; each must survive the other."""
    quoting = csv.QUOTE_MINIMAL if export.quoting == csv.QUOTE_ALL else csv.QUOTE_ALL
    return write(export.header, export.rows, quoting=quoting)


BENIGN: dict[str, Callable[[Export], bytes]] = {
    "BOM": with_bom,
    "header case and spaces": noisy_header,
    "CRLF line endings": crlf,
    "other quoting": other_quoting,
}


@pytest.mark.parametrize(
    "institution, variant",
    [
        pytest.param(institution, variant, id=f"{institution}: {name}")
        for institution in EXPORTS
        for name, variant in BENIGN.items()
    ],
)
def test_benign_variant_reads_like_the_clean_file(institution, variant):
    export = EXPORTS[institution]
    clean = write(export.header, export.rows, quoting=export.quoting)
    assert variant(export) != clean
    assert _read(variant(export)) == _read(clean)


def _encodable(values: list[str], encoding: str) -> bool:
    try:
        "".join(values).encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


@pytest.mark.parametrize("institution", EXPORTS)
def test_cp1252_file_reads_like_utf8(institution):
    """Excel on Windows saves as cp1252. Non-ASCII text must come out the same. (Rows
    cp1252 can't hold, like a space name with an emoji, can't be in such a file.)"""
    export = EXPORTS[institution]
    column = export.header.index(export.text_column)
    rows = [list(r) for r in export.rows if _encodable(r, "cp1252")]
    rows[0][column] = "Café Müller"
    from_cp1252 = _read(write(export.header, rows, encoding="cp1252"))
    assert from_cp1252 == _read(write(export.header, rows))
    assert from_cp1252[0].description == "Café Müller"
