from datetime import date

import pytest

from carddues.parsers import parse_statement
from carddues.text import normalize, parse_statement_date, undouble_glyphs

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


def test_sbicard_cashback_skips_stmt_number_for_min_due():
    result = parse(fixtures.SBICARD_CASHBACK_STMT_NO)

    assert result is not None
    assert result.total_due == pytest.approx(301.00)
    assert result.min_due == pytest.approx(200.00)
    assert result.min_due != pytest.approx(25122229867.0)


def test_sbicard_cashback_prefers_header_statement_date_over_period():
    result = parse(fixtures.SBICARD_CASHBACK_HEADER)

    assert result is not None
    assert result.statement_date == date(2025, 12, 24)
    assert result.due_date == date(2026, 1, 13)
    assert result.min_due == pytest.approx(200.00)
    assert result.statement_date != date(2025, 11, 28)
    assert result.statement_date != date(2025, 11, 25)


def test_statement_period_uses_the_range_end_not_later_transactions():
    window = "25 Nov 25 to 24 Dec 25\n28 Nov 25 PAYMENT RECEIVED 8,462.00 C"

    assert parse_statement_date(window, label="Statement Period") == date(2025, 12, 24)


def test_hdfc_compact_mask_is_preferred_over_alternate_account():
    result = parse(fixtures.HDFC_TATA_NEU_COMPACT)

    assert result is not None
    assert result.last4 == "2750"
    assert result.last4 != "0722"


def test_hdfc_tata_neu_live_layout_never_registers_0722():
    """Alternate Account …0722758 must not invent card ••0722."""
    from carddues.text import find_card_last4

    raw = fixtures.HDFC_TATA_NEU_LIVE_LAYOUT
    assert find_card_last4(raw) == "2750"
    result = parse(raw, issuer_hint="hdfc")

    assert result is not None
    assert result.last4 == "2750"
    assert result.total_due == pytest.approx(4373.0)
    assert "0722" not in (result.last4 or "")


def test_yesbank_reads_amounts_from_the_line_below_labels():
    """Points Earned : 0 and Available Cash Limit Cr must not become dues."""
    result = parse(fixtures.YESBANK_KLICK, issuer_hint="yesbank")

    assert result is not None
    assert result.issuer == "yesbank"
    assert result.last4 == "2653"
    assert result.total_due == pytest.approx(12255.0)
    assert result.min_due == pytest.approx(245.10)
    assert result.due_date == date(2026, 8, 1)
    assert result.statement_date == date(2026, 7, 12)
    assert result.total_due != 0
    assert result.min_due != pytest.approx(-10262.0)


def test_axis_value_on_following_line():
    result = parse(fixtures.AXIS_INLINE)
    assert result.issuer == "axis"
    assert result.total_due == pytest.approx(67540.20)
    assert result.min_due == pytest.approx(3377.00)
    assert result.due_date == date(2026, 8, 3)


def test_axis_payment_summary_table_not_formula_or_tnc():
    """Flipkart/Airtel/Ace Axis PDFs: header totals beat =Total Payment Due and T&Cs."""
    from carddues.text import find_card_last4

    raw = fixtures.AXIS_PAYMENT_SUMMARY
    assert find_card_last4(raw) == "0406"
    result = parse(raw, issuer_hint="axis")

    assert result is not None
    assert result.issuer == "axis"
    assert result.last4 == "0406"
    assert result.total_due == pytest.approx(554.00)
    assert result.min_due == pytest.approx(100.00)
    assert result.due_date == date(2025, 11, 4)
    assert result.statement_date == date(2025, 10, 15)
    assert result.matched_labels.get("statement_date") == "Statement Generation Date"
    # Previous balance / T&C sample figures must not win.
    assert result.total_due != pytest.approx(48.00)
    assert result.total_due != pytest.approx(8813.65)
    assert result.min_due != pytest.approx(3.75)
    assert result.min_due != pytest.approx(1953.65)


def test_axis_ace_payment_summary():
    result = parse(fixtures.AXIS_ACE_PAYMENT_SUMMARY, issuer_hint="axis")

    assert result is not None
    assert result.last4 == "0478"
    assert result.total_due == pytest.approx(16009.00)
    assert result.min_due == pytest.approx(321.00)
    assert result.due_date == date(2025, 11, 4)
    assert result.statement_date == date(2025, 10, 15)
    assert result.total_due != pytest.approx(52.00)


def test_axis_credit_balance_is_negative():
    result = parse(fixtures.AXIS_CREDIT_BALANCE, issuer_hint="axis")

    assert result is not None
    assert result.last4 == "0083"
    assert result.total_due == pytest.approx(-154.00)
    assert result.min_due == pytest.approx(0.00)
    assert result.due_date == date(2026, 6, 1)
    assert result.statement_date == date(2026, 5, 12)


def test_amex_new_balance_wording():
    result = parse(fixtures.AMEX_INLINE)
    assert result.issuer == "amex"
    assert result.min_due == pytest.approx(9211.00)
    assert result.due_date == date(2026, 8, 8)
    assert result.statement_date == date(2026, 7, 20)


def test_hsbc_inline_layout():
    result = parse(fixtures.HSBC_INLINE)

    assert result is not None
    assert result.issuer == "hsbc"
    assert result.total_due == pytest.approx(18450.00)
    assert result.min_due == pytest.approx(920.00)
    assert result.due_date == date(2026, 8, 11)
    assert result.statement_date == date(2026, 7, 22)
    assert result.last4 == "4200"


def test_hsbc_live_prefers_total_payment_due_over_net_outstanding():
    from carddues.text import find_card_last4

    raw = fixtures.HSBC_LIVE_JULY
    assert find_card_last4(raw) == "7672"
    result = parse(raw, issuer_hint="hsbc")

    assert result is not None
    assert result.issuer == "hsbc"
    assert result.last4 == "7672"
    assert result.total_due == pytest.approx(20252.96)
    assert result.min_due == pytest.approx(202.53)
    assert result.statement_date == date(2026, 7, 22)
    assert result.due_date == date(2026, 8, 6)
    assert result.total_due != pytest.approx(17367.28)


def test_hsbc_live_extracts_ddmmm_transactions():
    from carddues.parsers.transactions import extract_transactions
    from carddues.text import normalize

    rows = extract_transactions(normalize(fixtures.HSBC_LIVE_JULY))
    descriptions = [txn.description for txn in rows]
    assert "BBPS PMT BBPSDP016181185431LHnWAo" in descriptions
    assert "MW KPN FF 3072 WHITEFIELD BANGALORE" in descriptions
    assert "Zepto Marketplace Priv Bangalore IN" in descriptions
    assert not any("OUTSTANDING" in d.upper() for d in descriptions)
    assert not any("OPENING BALANCE" in d.upper() for d in descriptions)
    payment = next(txn for txn in rows if "BBPS" in txn.description)
    assert payment.is_credit
    assert payment.amount == pytest.approx(17367.28)
    assert payment.txn_date == date(2026, 6, 30)


def test_sbi_partial_tail_is_exposed_for_card_matching():
    from carddues.text import find_card_last4, find_card_tail

    raw = fixtures.SBICARD_PARTIAL_TAIL
    assert find_card_last4(raw) is None
    assert find_card_tail(raw) == "18"
    result = parse(raw, issuer_hint="sbicard")

    assert result is not None
    assert result.last4 is None
    assert result.card_tail == "18"
    assert result.total_due == pytest.approx(4512.00)


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


def test_doubled_header_glyphs_are_collapsed():
    assert undouble_glyphs("PPAAYYMMEENNTT DDUUEE DDAATTEE") == "PAYMENT DUE DATE"
    assert undouble_glyphs("SSTTAATTEEMMEENNTT DDAATTEE") == "STATEMENT DATE"
    assert undouble_glyphs("PAYMENT DUE DATE") == "PAYMENT DUE DATE"
    assert undouble_glyphs("4321 XXXX XXXX 8765") == "4321 XXXX XXXX 8765"


def test_icici_amazon_pay_reads_the_header_due_date_not_the_example():
    """A years-old illustration date must not replace August 15, 2026."""
    result = parse(fixtures.ICICI_AMAZON_DOUBLED_HEADERS, issuer_hint="icici")

    assert result is not None
    assert result.statement_date == date(2026, 7, 28)
    assert result.due_date == date(2026, 8, 15)
    assert result.total_due == pytest.approx(7317.0)
    assert result.due_date != date(2023, 10, 26)


def test_an_example_due_date_before_the_statement_is_rejected():
    """Without a usable header, a T&Cs sample from 2023 must not be kept."""
    text = """
    ICICI Bank Credit Card Statement
    Statement Date : 28-07-2026
    Total Amount due : Rs. 1,000.00
    4 Payment due date - Oct 26, 2023
    """
    result = parse(text, issuer_hint="icici")

    assert result is not None
    assert result.statement_date == date(2026, 7, 28)
    assert result.due_date is None


def test_a_statement_period_uses_the_closing_date():
    """June 29 to July 28 must not store June 29 as the statement date."""
    result = parse(fixtures.ICICI_PERIOD_ONLY, issuer_hint="icici")

    assert result is not None
    assert result.statement_date == date(2026, 7, 28)
    assert result.due_date == date(2026, 8, 15)
