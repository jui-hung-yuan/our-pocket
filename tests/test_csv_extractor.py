import pytest

from our_pocket.etl.extract import CsvExtractor, ExtractionError
from our_pocket.etl.registry import default_registry
from our_pocket.etl.transform.trade_republic import COLUMNS_V1

registry = default_registry()


def sniff_bytes(content: bytes):
    return CsvExtractor().extract(content, "test.csv")


def _csv(header: list[str], *rows: list[str], delimiter: str = ",") -> bytes:
    lines = [delimiter.join(f'"{c}"' for c in line) for line in (header, *rows)]
    return ("\n".join(lines) + "\n").encode()


def test_synthetic_export_matches_trade_republic_v1(synthetic):
    sniffed = sniff_bytes(synthetic.to_csv_bytes())
    schema = registry.match(sniffed.signature)
    assert schema is not None
    assert (schema.schema_id, schema.schema_version) == ("trade_republic.transactions_csv", 1)
    assert len(sniffed.rows) == len(synthetic.rows)
    assert sniffed.rows[0].line_number == 2


def test_fingerprint_ignores_bom_case_and_whitespace(synthetic):
    plain = sniff_bytes(synthetic.to_csv_bytes()).signature
    header = [f" {c.upper()} " for c in COLUMNS_V1]
    noisy = sniff_bytes(b"\xef\xbb\xbf" + _csv(header)).signature
    assert noisy.header_fingerprint == plain.header_fingerprint
    assert noisy.file_sha256 != plain.file_sha256


def test_reordered_columns_are_a_new_schema():
    reordered = list(COLUMNS_V1)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    signature = sniff_bytes(_csv(reordered)).signature
    assert registry.match(signature) is None
    assert "different order" in registry.explain_mismatch(signature)


def test_added_and_removed_columns_are_explained():
    changed = [c for c in COLUMNS_V1 if c != "mcc_code"] + ["isin_name"]
    signature = sniff_bytes(_csv(changed)).signature
    assert registry.match(signature) is None
    explanation = registry.explain_mismatch(signature)
    assert "trade_republic.transactions_csv v1" in explanation
    assert "missing columns: mcc_code" in explanation
    assert "extra columns: isin_name" in explanation


def test_semicolon_delimiter_is_detected_and_fingerprinted():
    signature = sniff_bytes(_csv(list(COLUMNS_V1), delimiter=";")).signature
    assert signature.delimiter == ";"
    assert registry.match(signature) is None
    assert "delimiter" in registry.explain_mismatch(signature)


def test_cp1252_fallback():
    signature = sniff_bytes("Buchungstag;Empfänger\n".encode("cp1252")).signature
    assert signature.encoding == "cp1252"
    assert signature.columns == ("buchungstag", "empfänger")


@pytest.mark.parametrize(
    "content, message",
    [
        (b"", "empty"),
        (b'"a","b"\n"1","2","3"\n', "3 values for 2 columns"),
        (b'"a","a"\n', "duplicate column"),
        (b"single_column_no_delimiter\n", "delimiter"),
    ],
)
def test_unreadable_files(content, message):
    with pytest.raises(ExtractionError, match=message):
        sniff_bytes(content)


def test_blank_lines_are_skipped_but_line_numbers_stay_true():
    sniffed = sniff_bytes(b'"a","b"\n"1","2"\n\n"3","4"\n')
    assert [r.line_number for r in sniffed.rows] == [2, 4]
