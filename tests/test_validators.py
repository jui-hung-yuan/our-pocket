import pytest

from our_pocket.etl.transform.validators import is_valid_iban, is_valid_isin


@pytest.mark.parametrize("isin", ["US0378331005", "GB0002634946", "AU0000XVGZA3"])
def test_valid_isins(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize(
    "isin",
    [
        "US0378331006",  # wrong check digit
        "US037833100",  # too short
        "us0378331005",  # lowercase
        "GB000263494X",  # check digit must be numeric
    ],
)
def test_invalid_isins(isin):
    assert not is_valid_isin(isin)


def test_iban_check():
    assert is_valid_iban("DE89370400440532013000")
    assert is_valid_iban("DE89 3704 0044 0532 0130 00")
    assert not is_valid_iban("DE88370400440532013000")
    assert not is_valid_iban("not an iban")
