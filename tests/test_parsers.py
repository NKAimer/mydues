from datetime import date

import pytest

from carddues.parsers import parse_statement
from carddues.text import normalize

from . import fixtures


def parse(raw: str, **kwargs):
    return parse_statement(normalize(raw), **kwargs)


def test_hdfc_table_layout():
    result = parse(fixtures.HDFC_TABLE)
    assert result is not None
    assert result.issuer == "hdfc"
    assert result.total_due == pytest.approx(45231.50)
    assert result.min_due == pytest.approx(2270.00)
    assert result.due_date == date(2026, 7, 25)
    assert result.statement_date == date(2026, 7, 5)
    assert result.last4 == "8765"


def test_hdfc_extra_column_does_not_break_limits():
    result = parse(fixtures.HDFC_TABLE)
    assert result.credit_limit == pytest.approx(500000.00)
    assert result.available_credit == pytest.approx(454768.50)


def test_icici_inline_layout():
    result = parse(fixtures.ICICI_INLINE)
    assert result.issuer == "icici"
    assert result.total_due == pytest.approx(112480.35)
    assert result.min_due == pytest.approx(5624.00)
    assert result.due_date == date(2026, 8, 5)
    assert result.statement_date == date(2026, 7, 18)
    assert result.credit_limit == pytest.approx(800000.00)
    assert result.available_credit == pytest.approx(687519.65)


def test_sbicard_table_with_currency_prefix():
    result = parse(fixtures.SBICARD_TABLE)
    assert result.issuer == "sbicard"
    assert result.total_due == pytest.approx(23908.00)
    assert result.min_due == pytest.approx(1200.00)
    assert result.due_date == date(2026, 8, 2)
    assert result.statement_date == date(2026, 7, 12)


def test_axis_value_on_following_line():
    result = parse(fixtures.AXIS_INLINE)
    assert result.issuer == "axis"
    assert result.total_due == pytest.approx(67540.20)
    assert result.min_due == pytest.approx(3377.00)
    assert result.due_date == date(2026, 8, 3)


def test_amex_new_balance_wording():
    result = parse(fixtures.AMEX_INLINE)
    assert result.issuer == "amex"
    assert result.min_due == pytest.approx(9211.00)
    assert result.due_date == date(2026, 8, 8)
    assert result.statement_date == date(2026, 7, 20)


def test_credit_balance_is_negative():
    result = parse(fixtures.CREDIT_BALANCE)
    assert result.issuer == "kotak"
    assert result.total_due == pytest.approx(-3410.75)


def test_unknown_issuer_falls_back_to_generic():
    result = parse(fixtures.UNKNOWN_ISSUER)
    assert result is not None
    assert result.parser == "generic"
    assert result.total_due == pytest.approx(8750.00)
    assert result.due_date == date(2026, 7, 29)


def test_non_statement_returns_nothing():
    assert parse(fixtures.NOT_A_STATEMENT) is None


def test_issuer_hint_is_used():
    result = parse(fixtures.UNKNOWN_ISSUER, issuer_hint="hdfc")
    assert result.total_due == pytest.approx(8750.00)


def test_confidence_reflects_completeness():
    full = parse(fixtures.ICICI_INLINE)
    assert full.confidence == 1.0
