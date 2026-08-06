"""Amount parsing guards for phone numbers and money-shaped figures."""

import pytest

from mydues.text import looks_phone_like_amount, parse_amount, parse_amount_meta


def test_parse_amount_skips_toll_free_bare_integer():
    assert parse_amount("Toll Free: 8250825") is None
    assert parse_amount("Toll Free: +800 08250825") is None


def test_parse_amount_keeps_currency_and_grouped_bills():
    assert parse_amount("C1,72,729.00") == pytest.approx(172729.0)
    assert parse_amount("Rs. 12,255.00") == pytest.approx(12255.0)
    value, shaped = parse_amount_meta("C1,72,729.00")
    assert value == pytest.approx(172729.0)
    assert shaped is True


def test_parse_amount_skips_bare_seven_digit_integer():
    assert parse_amount("8250825") is None
    assert parse_amount_meta("8250825") is None


def test_looks_phone_like_amount():
    assert looks_phone_like_amount(8250825.0)
    assert not looks_phone_like_amount(172729.0)
    assert not looks_phone_like_amount(8250825.50)
