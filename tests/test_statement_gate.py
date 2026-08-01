"""Non-card bank PDFs must not become dues; real card bills must still parse."""

from datetime import date, datetime

import pytest

from carddues import db, gmail, ingest
from carddues.models import SOURCE_STATEMENT, Card, ParsedStatement, StatementRecord
from carddues.parsers import parse_statement
from carddues.statement_gate import is_credit_card_mail, looks_like_credit_card_statement
from carddues.text import normalize

from . import fixtures


def parse(raw: str, **kwargs):
    return parse_statement(normalize(raw), **kwargs)


def test_demat_text_is_not_a_credit_card_statement():
    assert not looks_like_credit_card_statement(fixtures.ICICI_DEMAT_ESTATEMENT)


def test_savings_estatement_is_not_a_credit_card_statement():
    assert not looks_like_credit_card_statement(fixtures.ICICI_SAVINGS_ESTATEMENT)
    assert parse(fixtures.ICICI_SAVINGS_ESTATEMENT, issuer_hint="icici") is None


def test_demat_pdf_is_not_parsed_as_a_card_bill():
    assert parse(fixtures.ICICI_DEMAT_ESTATEMENT, issuer_hint="icici") is None


def test_real_issuer_fixtures_still_look_like_card_bills():
    for raw in (
        fixtures.HDFC_TABLE,
        fixtures.ICICI_INLINE,
        fixtures.SBICARD_CASHBACK_HEADER,
        fixtures.AXIS_INLINE,
        fixtures.AXIS_PAYMENT_SUMMARY,
        fixtures.AMEX_INLINE,
        fixtures.HSBC_LIVE_JULY,
        fixtures.HDFC_SWIGGY_ALTERNATE,
    ):
        assert looks_like_credit_card_statement(raw)
        assert parse(raw) is not None


def test_demat_mail_subject_is_skipped():
    assert not is_credit_card_mail(
        subject="Transaction e-Statement for ICICI Bank Demat Account IN303028 XXXXXX95"
    )
    assert is_credit_card_mail(
        subject="Amazon Pay ICICI Bank Credit Card Statement for the period June 29, 2026"
    )


def test_savings_mail_subject_is_skipped():
    assert not is_credit_card_mail(
        subject="ICICI Bank Statement from June 01, 2026 to June 30, 2026 for XXXXXXXX5705"
    )
    assert is_credit_card_mail(
        subject="ICICI Bank Credit Card Statement for the period June 26 2026 to July 25 2026"
    )


def test_hsbc_terms_pdf_is_rejected():
    assert not looks_like_credit_card_statement(fixtures.HSBC_MITC)
    assert not is_credit_card_mail(filename="Most Important Terms and Conditions.pdf")
    assert not is_credit_card_mail(filename="Most_Important_Terms___Conditions.pdf")
    assert not is_credit_card_mail(filename="Key Fact Statement.pdf")
    assert is_credit_card_mail(filename="20260722.pdf")


def test_sbi_bill_with_mitc_footnote_still_counts_as_a_statement():
    """SBI appends MITC links; that must not veto the bill on page one."""
    assert looks_like_credit_card_statement(fixtures.SBICARD_PARTIAL_TAIL)
    assert parse(fixtures.SBICARD_PARTIAL_TAIL, issuer_hint="sbicard") is not None


def test_yes_bank_sender_domain_is_registered():
    from carddues import issuers

    assert "yes.bank.in" in issuers.get("yesbank").senders
    assert issuers.detect_from_sender("<estatement@yes.bank.in>") == "yesbank"


def test_gmail_query_excludes_demat_subjects():
    query = gmail.build_query(30)
    assert "-subject:\"demat\"" in query
    assert "credit card statement" in query
    assert "newer_than:30d" in query


def test_gmail_query_supports_month_lookback():
    query = gmail.build_query(lookback_months=1)
    assert "newer_than:1m" in query
    assert "newer_than:1d" not in query


def test_store_does_not_auto_create_card_without_due_fields(conn):
    weak = ParsedStatement(
        total_due=100.0,
        last4="1189",
        issuer="hdfc",
        parser="icici",
    )
    status, detail, card_id = ingest.store(conn, weak, source_ref="msg-demat")

    assert status == ingest.STATUS_UNPARSED
    assert card_id is None
    assert db.list_cards(conn) == []
    assert "which card" in detail


def test_init_purges_phantom_hdfc_1189(conn):
    card = Card(issuer="hdfc", label="HDFC Bank ••1189", last4="1189")
    card.id = db.add_card(conn, card)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=100.0,
            source=SOURCE_STATEMENT,
            source_ref="msg-demat",
            as_of=datetime(2026, 8, 1, 1, 18),
            statement_date=date(2026, 5, 1),
            parser="icici",
        ),
    )

    db.init(conn)

    assert db.find_card(conn, issuer="hdfc", last4="1189") is None


def test_init_retargets_hdfc_2476_and_purges_icici_0001(conn):
    swiggy = Card(issuer="hdfc", label="HDFC Bank ••6527", last4="6527")
    swiggy.id = db.add_card(conn, swiggy)
    bogus = Card(issuer="hdfc", label="HDFC Bank ••2476", last4="2476")
    bogus.id = db.add_card(conn, bogus)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=bogus.id,
            total_due=4551.0,
            source=SOURCE_STATEMENT,
            source_ref="msg-swiggy",
            as_of=datetime(2026, 8, 1, 1, 13),
            statement_date=date(2026, 2, 20),
            parser="hdfc",
        ),
    )
    savings = Card(issuer="icici", label="ICICI Bank ••0001", last4="0001")
    savings.id = db.add_card(conn, savings)

    db.init(conn)

    assert db.find_card(conn, issuer="hdfc", last4="2476") is None
    assert db.find_card(conn, issuer="hdfc", last4="6527") is not None
    assert db.find_card(conn, issuer="icici", last4="0001") is None
    rows = conn.execute(
        "SELECT card_id FROM statements WHERE source_ref = ?", ("msg-swiggy",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["card_id"] == swiggy.id


def test_hdfc_swiggy_prefers_card_no_over_alternate_account():
    from carddues.text import find_card_last4

    raw = fixtures.HDFC_SWIGGY_ALTERNATE
    assert find_card_last4(raw) == "6527"
    result = parse(raw, issuer_hint="hdfc")
    assert result is not None
    assert result.last4 == "6527"
    assert result.total_due == pytest.approx(4551.0)
