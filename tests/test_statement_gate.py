"""Non-card bank PDFs must not become dues; real card bills must still parse."""

from datetime import date, datetime

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


def test_demat_pdf_is_not_parsed_as_a_card_bill():
    assert parse(fixtures.ICICI_DEMAT_ESTATEMENT, issuer_hint="icici") is None


def test_real_issuer_fixtures_still_look_like_card_bills():
    for raw in (
        fixtures.HDFC_TABLE,
        fixtures.ICICI_INLINE,
        fixtures.SBICARD_CASHBACK_HEADER,
        fixtures.AXIS_INLINE,
        fixtures.AMEX_INLINE,
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


def test_yes_bank_sender_domain_is_registered():
    from carddues import issuers

    assert "yes.bank.in" in issuers.get("yesbank").senders
    assert issuers.detect_from_sender("<estatement@yes.bank.in>") == "yesbank"


def test_gmail_query_excludes_demat_subjects():
    query = gmail.build_query(30)
    assert "-subject:\"demat\"" in query
    assert "credit card statement" in query


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
