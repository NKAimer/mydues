from datetime import date

import pytest

from carddues import db, dues, ingest
from carddues.models import Card, ParsedStatement

STATEMENT = ParsedStatement(
    total_due=45231.50,
    min_due=2270.0,
    due_date=date(2026, 7, 25),
    statement_date=date(2026, 7, 5),
    credit_limit=500000.0,
    last4="8765",
    issuer="hdfc",
    parser="hdfc",
)


def test_store_registers_an_unknown_card(conn):
    status, detail, card_id = ingest.store(conn, STATEMENT, source_ref="msg-1")

    assert status == ingest.STATUS_PARSED
    assert card_id is not None
    cards = db.list_cards(conn)
    assert len(cards) == 1
    assert cards[0].last4 == "8765"
    assert "HDFC Bank" in cards[0].label
    assert "45231.50" in detail


def test_store_matches_an_existing_card(conn):
    card = Card(issuer="hdfc", label="My Infinia", last4="8765")
    card.id = db.add_card(conn, card)

    _, _, card_id = ingest.store(conn, STATEMENT, source_ref="msg-1")

    assert card_id == card.id
    assert len(db.list_cards(conn)) == 1
    view = dues.view_for_card(conn, card, today=date(2026, 7, 20))
    assert view.billed_amount == pytest.approx(45231.50)


def test_reingesting_the_same_statement_does_not_duplicate(conn):
    ingest.store(conn, STATEMENT, source_ref="msg-1")
    ingest.store(conn, STATEMENT, source_ref="msg-1")

    card = db.list_cards(conn)[0]
    assert len(db.statements_for_card(conn, card.id)) == 1


def test_statement_without_card_digits_needs_a_single_issuer_match(conn):
    anonymous = ParsedStatement(total_due=1000.0, issuer="hdfc")

    status, detail, card_id = ingest.store(conn, anonymous, source_ref="msg-2")
    assert status == ingest.STATUS_UNPARSED
    assert card_id is None
    assert "which card" in detail

    card = Card(issuer="hdfc", label="Only HDFC", last4="8765")
    card.id = db.add_card(conn, card)
    status, _, card_id = ingest.store(conn, anonymous, source_ref="msg-2")
    assert status == ingest.STATUS_PARSED
    assert card_id == card.id


def test_ingest_log_tracks_what_was_seen(conn):
    db.log_ingest(conn, message_id="m1", filename="a.pdf", status="parsed", detail="ok")
    db.log_ingest(conn, message_id="m2", filename="b.pdf", status="locked", detail="no password")

    assert ("m1", "a.pdf") in db.seen_attachments(conn)
    assert ("m2", "b.pdf") not in db.seen_attachments(conn)
    assert [row["filename"] for row in db.unresolved_ingest(conn)] == ["b.pdf"]
