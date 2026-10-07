"""IBAN engine: validity, generation, country coverage, routing checksums."""
import pytest

from zenithpay.iban import (
    IBAN_FORMATS,
    SUPPORTED_COUNTRIES,
    format_iban,
    generate_account_number,
    generate_iban,
    generate_routing_number,
    is_supported,
    supports_iban,
    supported_countries,
    validate_iban,
)

VALID_IBANS = [
    "DE89370400440532013000",
    "GB82WEST12345698765432",
    "FR1420041010050500013M02606",
    "NL91ABNA0417164300",
    "AT611904300234573201",
    "CH9300762011623852957",
    "IT60X0542811101000000123456",
    "ES9121000418450200051332",
]


def test_validate_known_good_ibans():
    for iban in VALID_IBANS:
        assert validate_iban(iban), iban


def test_validate_accepts_spaced_and_lowercase():
    assert validate_iban("de89 3704 0044 0532 0130 00")


def test_validate_rejects_tampered_check_digits():
    assert not validate_iban("DE00370400440532013000")
    assert not validate_iban("GB83WEST12345698765432")  # 82 -> 83


def test_validate_rejects_garbage():
    assert not validate_iban("")
    assert not validate_iban("hello")
    assert not validate_iban(None)
    assert not validate_iban(12345)
    assert not validate_iban("DE8937040044053201300")  # too short


def test_format_iban_groups_of_four():
    assert format_iban("de89370400440532013000") == "DE89 3704 0044 0532 0130 00"


def test_generate_valid_for_every_iban_country():
    for country, (length, _structure) in IBAN_FORMATS.items():
        iban = generate_iban(country)
        assert len(iban) == length, country
        assert iban.startswith(country)
        assert validate_iban(iban), iban


def test_generated_ibans_are_unique():
    seen = {generate_iban("DE") for _ in range(50)}
    assert len(seen) == 50


def test_nigeria_excluded_and_unsupported():
    assert "NG" not in SUPPORTED_COUNTRIES
    assert not is_supported("NG")
    assert not supports_iban("NG")
    with pytest.raises(ValueError):
        generate_iban("NG")


def test_non_iban_country_cannot_generate():
    with pytest.raises(ValueError):
        generate_iban("US")


def test_supported_countries_payload():
    countries = supported_countries()
    assert len(countries) >= 240
    by_code = {c["code"]: c for c in countries}
    assert by_code["DE"]["iban"] is True
    assert by_code["US"]["iban"] is False
    assert by_code["AF"]["currency"] == "AFN"
    assert by_code["ZW"]
    assert "NG" not in by_code
    for c in countries:
        assert len(c["currency"]) == 3 and c["currency"].isalpha()
        assert c["name"]
    # every IBAN-registry country we claim must be openable
    for country in IBAN_FORMATS:
        assert country in by_code


def test_us_routing_number_has_valid_aba_checksum():
    for _ in range(20):
        routing = generate_routing_number("US")
        assert len(routing) == 9 and routing.isdigit()
        d = [int(ch) for ch in routing]
        checksum = (
            3 * (d[0] + d[3] + d[6])
            + 7 * (d[1] + d[4] + d[7])
            + (d[2] + d[5] + d[8])
        )
        assert checksum % 10 == 0


def test_account_number_lengths():
    assert len(generate_account_number("US")) == 12
    assert len(generate_account_number("GB")) == 8
    assert len(generate_account_number("DE")) == 10
    for country in ("US", "GB", "DE", "KE", "JP"):
        assert generate_account_number(country).isdigit()


def test_routing_lengths_for_common_countries():
    assert len(generate_routing_number("GB")) == 6
    assert len(generate_routing_number("DE")) == 8
    assert len(generate_routing_number("IN")) == 11
    assert generate_routing_number("IN").startswith("ZNPY0")
