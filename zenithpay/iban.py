"""Multi-country IBAN engine (ISO 13616 / SWIFT registry).

- IBAN_FORMATS:      country -> (total length incl. country + check digits, BBAN structure)
- generate_iban():   builds a valid IBAN with mod-97 check digits
- validate_iban():   recomputes the check digits
- format_iban():     groups of four for display

Structure letters: n = digit, a = uppercase letter, c = alphanumeric.

Nigeria (NG) is intentionally excluded from SUPPORTED_COUNTRIES (project decision).
"""
from __future__ import annotations

import re
import secrets

import pycountry
from babel.numbers import get_territory_currencies

# --------------------------------------------------------------------------- #
# IBAN registry: country -> (total length, BBAN structure)
# --------------------------------------------------------------------------- #
IBAN_FORMATS: dict[str, tuple[int, str]] = {
    "AD": (24, "4n4n12n"),
    "AE": (23, "3n16n"),
    "AL": (28, "8n16n"),
    "AT": (20, "5n11n"),
    "AZ": (28, "4a20n"),
    "BA": (20, "3n3n8n2n"),
    "BE": (16, "3n7n2n"),
    "BG": (22, "4a4n2n8n"),
    "BH": (22, "4a14n"),
    "BR": (29, "8n5n10n1a1n"),
    "BY": (28, "4a4n16n"),
    "CH": (21, "5n12n"),
    "CR": (22, "4n14n"),
    "CY": (28, "3n5n16n"),
    "CZ": (24, "4n6n10n"),
    "DE": (22, "8n10n"),
    "DK": (18, "4n9n1n"),
    "DO": (28, "4a20n"),
    "EE": (20, "2n2n11n1n"),
    "EG": (29, "4n4n17n"),
    "ES": (24, "4n4n1n1n10n"),
    "FI": (18, "6n7n1n"),
    "FO": (18, "4n9n1n"),
    "FR": (27, "5n5n11c2n"),  # French account numbers may contain letters
    "GB": (22, "4a6n8n"),
    "GE": (22, "2a16n"),
    "GI": (23, "4a15n"),
    "GL": (18, "4n9n1n"),
    "GR": (27, "3n4n16n"),
    "GT": (28, "4a20n"),
    "HR": (21, "7n10n"),
    "HU": (28, "3n4n1n15n1n"),
    "IE": (22, "4a6n8n"),
    "IL": (23, "3n3n13n"),
    "IQ": (23, "4a3n12n"),
    "IR": (26, "22n"),
    "IS": (26, "4n2n6n10n"),
    "IT": (27, "1a5n5n12n"),
    "JO": (30, "4a4n18n"),
    "KW": (30, "4a22n"),
    "KZ": (20, "3n13n"),
    "LB": (28, "4n20n"),
    "LC": (32, "4a24n"),
    "LI": (21, "5n12n"),
    "LT": (20, "5n11n"),
    "LU": (20, "3n13n"),
    "LV": (21, "4a13n"),
    "MC": (27, "5n5n11n2n"),
    "MD": (24, "2a18n"),
    "ME": (22, "3n13n2n"),
    "MK": (19, "3n10n2n"),
    "MR": (27, "5n5n11n2n"),
    "MT": (31, "4a5n18n"),
    "MU": (30, "4a19n3n"),
    "NL": (18, "4a10n"),
    "NO": (15, "4n6n1n"),
    "PK": (24, "4a16n"),
    "PL": (28, "24n"),
    "PS": (29, "4a21n"),
    "PT": (25, "4n4n11n2n"),
    "QA": (29, "4a21n"),
    "RO": (24, "4a16n"),
    "RS": (22, "3n13n2n"),
    "SA": (24, "2n18n"),
    "SC": (31, "4a2n2n16n3n"),
    "SE": (24, "3n16n1n"),
    "SI": (19, "5n8n2n"),
    "SK": (24, "4n6n10n"),
    "SM": (27, "1a5n5n12n"),
    "ST": (25, "8n11n2n"),
    "SV": (28, "4a20n"),
    "TL": (23, "3n14n2n"),
    "TN": (24, "2n3n13n2n"),
    "TR": (26, "5n1n16n"),
    "UA": (29, "6n19n"),
    "VA": (22, "3n15n"),
    "VG": (24, "4a16n"),
    "XK": (20, "4n10n2n"),
}

# --------------------------------------------------------------------------- #
# Countries we accept for account opening: code -> (name, default currency)
# --------------------------------------------------------------------------- #
SUPPORTED_COUNTRIES: dict[str, tuple[str, str]] = {
    # IBAN registry countries
    "AD": ("Andorra", "EUR"),
    "AE": ("United Arab Emirates", "AED"),
    "AL": ("Albania", "ALL"),
    "AT": ("Austria", "EUR"),
    "AZ": ("Azerbaijan", "AZN"),
    "BA": ("Bosnia and Herzegovina", "BAM"),
    "BE": ("Belgium", "EUR"),
    "BG": ("Bulgaria", "BGN"),
    "BH": ("Bahrain", "BHD"),
    "BR": ("Brazil", "BRL"),
    "BY": ("Belarus", "BYN"),
    "CH": ("Switzerland", "CHF"),
    "CR": ("Costa Rica", "CRC"),
    "CY": ("Cyprus", "EUR"),
    "CZ": ("Czechia", "CZK"),
    "DE": ("Germany", "EUR"),
    "DK": ("Denmark", "DKK"),
    "DO": ("Dominican Republic", "DOP"),
    "EE": ("Estonia", "EUR"),
    "EG": ("Egypt", "EGP"),
    "ES": ("Spain", "EUR"),
    "FI": ("Finland", "EUR"),
    "FO": ("Faroe Islands", "DKK"),
    "FR": ("France", "EUR"),
    "GB": ("United Kingdom", "GBP"),
    "GE": ("Georgia", "GEL"),
    "GI": ("Gibraltar", "GIP"),
    "GL": ("Greenland", "DKK"),
    "GR": ("Greece", "EUR"),
    "GT": ("Guatemala", "GTQ"),
    "HR": ("Croatia", "EUR"),
    "HU": ("Hungary", "HUF"),
    "IE": ("Ireland", "EUR"),
    "IL": ("Israel", "ILS"),
    "IQ": ("Iraq", "IQD"),
    "IR": ("Iran", "IRR"),
    "IS": ("Iceland", "ISK"),
    "IT": ("Italy", "EUR"),
    "JO": ("Jordan", "JOD"),
    "KW": ("Kuwait", "KWD"),
    "KZ": ("Kazakhstan", "KZT"),
    "LB": ("Lebanon", "LBP"),
    "LC": ("Saint Lucia", "XCD"),
    "LI": ("Liechtenstein", "CHF"),
    "LT": ("Lithuania", "EUR"),
    "LU": ("Luxembourg", "EUR"),
    "LV": ("Latvia", "EUR"),
    "MC": ("Monaco", "EUR"),
    "MD": ("Moldova", "MDL"),
    "ME": ("Montenegro", "EUR"),
    "MK": ("North Macedonia", "MKD"),
    "MR": ("Mauritania", "MRU"),
    "MT": ("Malta", "EUR"),
    "MU": ("Mauritius", "MUR"),
    "NL": ("Netherlands", "EUR"),
    "NO": ("Norway", "NOK"),
    "PK": ("Pakistan", "PKR"),
    "PL": ("Poland", "PLN"),
    "PS": ("Palestine", "ILS"),
    "PT": ("Portugal", "EUR"),
    "QA": ("Qatar", "QAR"),
    "RO": ("Romania", "RON"),
    "RS": ("Serbia", "RSD"),
    "SA": ("Saudi Arabia", "SAR"),
    "SC": ("Seychelles", "SCR"),
    "SE": ("Sweden", "SEK"),
    "SI": ("Slovenia", "EUR"),
    "SK": ("Slovakia", "EUR"),
    "SM": ("San Marino", "EUR"),
    "ST": ("Sao Tome and Principe", "STN"),
    "SV": ("El Salvador", "USD"),
    "TL": ("Timor-Leste", "USD"),
    "TN": ("Tunisia", "TND"),
    "TR": ("Turkiye", "TRY"),
    "UA": ("Ukraine", "UAH"),
    "VA": ("Vatican City", "EUR"),
    "VG": ("British Virgin Islands", "USD"),
    "XK": ("Kosovo", "EUR"),
    # Non-IBAN countries (routing + account number only, no IBAN)
    "AR": ("Argentina", "ARS"),
    "AU": ("Australia", "AUD"),
    "BD": ("Bangladesh", "BDT"),
    "CA": ("Canada", "CAD"),
    "CL": ("Chile", "CLP"),
    "CN": ("China", "CNY"),
    "CO": ("Colombia", "COP"),
    "ET": ("Ethiopia", "ETB"),
    "GH": ("Ghana", "GHS"),
    "HK": ("Hong Kong", "HKD"),
    "ID": ("Indonesia", "IDR"),
    "IN": ("India", "INR"),
    "JP": ("Japan", "JPY"),
    "KE": ("Kenya", "KES"),
    "KH": ("Cambodia", "KHR"),
    "KR": ("South Korea", "KRW"),
    "LK": ("Sri Lanka", "LKR"),
    "MX": ("Mexico", "MXN"),
    "MY": ("Malaysia", "MYR"),
    "NP": ("Nepal", "NPR"),
    "NZ": ("New Zealand", "NZD"),
    "PE": ("Peru", "PEN"),
    "PH": ("Philippines", "PHP"),
    "RU": ("Russia", "RUB"),
    "RW": ("Rwanda", "RWF"),
    "SG": ("Singapore", "SGD"),
    "TZ": ("Tanzania", "TZS"),
    "TH": ("Thailand", "THB"),
    "TW": ("Taiwan", "TWD"),
    "UG": ("Uganda", "UGX"),
    "US": ("United States", "USD"),
    "VN": ("Vietnam", "VND"),
    "ZA": ("South Africa", "ZAR"),
    "ZM": ("Zambia", "ZMW"),
    # Nigeria (NG) intentionally excluded.
}

# Include every assigned ISO 3166-1 country code, preserving the curated
# labels/currencies above where available.
for _country in pycountry.countries:
    if _country.alpha_2 == "NG":
        continue
    _currencies = get_territory_currencies(
        _country.alpha_2, tender=True, non_tender=False
    )
    _currency = next(
        (code for code in _currencies if len(code) == 3 and code.isalpha()),
        "USD",
    )
    SUPPORTED_COUNTRIES.setdefault(
        _country.alpha_2,
        (getattr(_country, "common_name", _country.name), _currency),
    )

# Branded filler for lettered BBAN segments (GB/MT/... bank codes read "ZNPY").
_BRAND_LETTERS = "ZNPY"

_ROUTING_LENGTHS = {
    "CA": 8, "GB": 6, "DE": 8, "AU": 6, "NZ": 6, "ZA": 6,
    "JP": 7, "CN": 12, "SG": 7, "KR": 3, "HK": 3, "TW": 6,
}

_ACCOUNT_NUMBER_LENGTHS = {"US": 12, "GB": 8}


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #
def is_supported(country: str) -> bool:
    return (country or "").upper() in SUPPORTED_COUNTRIES


def supports_iban(country: str) -> bool:
    return (country or "").upper() in IBAN_FORMATS


def default_currency(country: str) -> str:
    entry = SUPPORTED_COUNTRIES.get((country or "").upper())
    return entry[1] if entry else "USD"


def supported_countries() -> list[dict]:
    """Country list for account creation."""
    return [
        {"code": code, "name": name, "currency": currency, "iban": supports_iban(code)}
        for code, (name, currency) in sorted(SUPPORTED_COUNTRIES.items())
    ]


def format_iban(iban: str) -> str:
    """Group an IBAN into blocks of four: DE89 3704 ..."""
    compact = _compact(iban)
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


def validate_iban(iban: str) -> bool:
    """True when structure, country format and mod-97 check digits all hold."""
    if not isinstance(iban, str):
        return False
    compact = _compact(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", compact):
        return False
    country, check, bban = compact[:2], compact[2:4], compact[4:]
    if country in IBAN_FORMATS:
        length, structure = IBAN_FORMATS[country]
        if len(compact) != length:
            return False
        if not re.fullmatch(_structure_regex(structure), bban):
            return False
    return check == _check_digits(country, bban)


def generate_iban(country: str) -> str:
    """Generate a structurally valid IBAN for an IBAN-registry country."""
    country = (country or "").upper()
    if country not in IBAN_FORMATS:
        raise ValueError(f"country {country!r} does not use IBAN")
    length, structure = IBAN_FORMATS[country]
    bban = _build_bban(structure)
    if len(bban) != length - 4:  # pragma: no cover - guards format typos
        raise ValueError(f"bad IBAN structure for {country}: {structure}")
    return f"{country}{_check_digits(country, bban)}{bban}"


def generate_account_number(country: str) -> str:
    """Internal account number (displayed alongside IBAN / routing)."""
    country = (country or "").upper()
    length = _ACCOUNT_NUMBER_LENGTHS.get(country, 10)
    return _random_digits(length)


def generate_routing_number(country: str) -> str:
    """Country-appropriate routing identifier.

    US gets a real ABA routing number with a valid checksum; IN gets an
    IFSC-shaped code; everything else gets a plain digit string.
    """
    country = (country or "").upper()
    if country == "US":
        return _aba_routing_number()
    if country == "IN":
        return f"ZNPY0{_random_alnum(6)}"
    return _random_digits(_ROUTING_LENGTHS.get(country, 9))


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _compact(iban: str) -> str:
    return re.sub(r"[\s-]", "", iban).upper()


def _parse_structure(structure: str) -> list[tuple[int, str]]:
    return [(int(n), kind) for n, kind in re.findall(r"(\d+)([nac])", structure)]


def _structure_regex(structure: str) -> str:
    parts = {"n": r"\d", "a": r"[A-Z]", "c": r"[A-Z0-9]"}
    return "".join(f"{parts[kind]}{{{n}}}" for n, kind in _parse_structure(structure))


def _build_bban(structure: str) -> str:
    bban = ""
    for n, kind in _parse_structure(structure):
        if kind == "n":
            first = secrets.choice("123456789") if not bban else secrets.choice("0123456789")
            bban += first + "".join(secrets.choice("0123456789") for _ in range(n - 1))
        elif kind == "a":
            bban += _random_letters(n)
        else:
            bban += _random_alnum(n)
    return bban


def _random_letters(n: int) -> str:
    return "".join(_BRAND_LETTERS[i % len(_BRAND_LETTERS)] for i in range(n))


def _random_alnum(n: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _random_digits(n: int) -> str:
    first = secrets.choice("123456789") if n else ""
    return first + "".join(secrets.choice("0123456789") for _ in range(n - 1))


def _check_digits(country: str, bban: str) -> str:
    """ISO 7064 MOD 97-10: 98 - mod97(rearranged)."""
    rearranged = f"{bban}{country}00"
    numeric = "".join(
        ch if ch.isdigit() else str(ord(ch) - 55) for ch in rearranged
    )
    return f"{98 - int(numeric) % 97:02d}"


def _aba_routing_number() -> str:
    """9-digit US ABA routing number with a valid checksum digit."""
    digits = [secrets.choice("0123456789") for _ in range(8)]
    total = (
        3 * (int(digits[0]) + int(digits[3]) + int(digits[6]))
        + 7 * (int(digits[1]) + int(digits[4]) + int(digits[7]))
        + (int(digits[2]) + int(digits[5]))
    )
    check = (10 - total % 10) % 10
    return "".join(digits) + str(check)
