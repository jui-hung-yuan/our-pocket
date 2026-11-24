import pytest

from our_pocket.etl.extract import CsvExtractor, ExtractionError
from our_pocket.etl.registry import default_registry

registry = default_registry()


def sniff_bytes(content: bytes):
    return CsvExtractor().extract(content, "test.csv")


def test_synthetic_export_matches_trade_republic_v1(synthetic):
    sniffed = sniff_bytes(synthetic.to_csv_bytes())
    schema = registry.match(sniffed.signature)
    assert schema is not None
    assert (schema.schema_id, schema.schema_version) == ("trade_republic.transactions_csv", 1)
    assert len(sniffed.rows) == len(synthetic.rows)
    assert sniffed.rows[0].line_number == 2


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
