"""Check-digit validation: cheap, deterministic proof that an identifier wasn't mangled."""

import re

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_IBAN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


def _letters_to_digits(s: str) -> str:
    return "".join(str(int(ch, 36)) for ch in s)


def isin_check_digit(body: str) -> str:
    """Check digit for the first 11 characters of an ISIN (Luhn over the digit string)."""
    digits = _letters_to_digits(body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
        total += n // 10 + n % 10
    return str((10 - total % 10) % 10)


def is_valid_isin(isin: str) -> bool:
    return bool(_ISIN.match(isin)) and isin_check_digit(isin[:11]) == isin[11]


def iban_check_digits(country: str, bban: str) -> str:
    remainder = int(_letters_to_digits(bban + country) + "00") % 97
    return f"{98 - remainder:02d}"


def is_valid_iban(iban: str) -> bool:
    iban = iban.replace(" ", "")
    if not _IBAN.match(iban):
        return False
    return int(_letters_to_digits(iban[4:] + iban[:4])) % 97 == 1
