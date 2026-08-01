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


def test_store_matches_sbi_partial_tail_to_registered_card(conn):
    phonepe = Card(issuer="sbicard", label="SBI Phonepe ••3418", last4="3418")
    phonepe.id = db.add_card(conn, phonepe)
    cashback = Card(issuer="sbicard", label="SBI Cashback ••9326", last4="9326")
    cashback.id = db.add_card(conn, cashback)

    statement = ParsedStatement(
        total_due=4512.0,
        min_due=225.0,
        due_date=date(2026, 8, 13),
        statement_date=date(2026, 7, 24),
        issuer="sbicard",
        parser="sbicard",
        card_tail="18",
    )
    status, detail, card_id = ingest.store(conn, statement, source_ref="msg-phonepe")

    assert status == ingest.STATUS_PARSED
    assert card_id == phonepe.id
    assert "Phonepe" in detail or "3418" in detail


def test_store_does_not_guess_when_partial_tail_matches_two_cards(conn):
    db.add_card(conn, Card(issuer="sbicard", label="A ••3418", last4="3418"))
    db.add_card(conn, Card(issuer="sbicard", label="B ••5518", last4="5518"))

    statement = ParsedStatement(
        total_due=100.0,
        issuer="sbicard",
        parser="sbicard",
        card_tail="18",
    )
    status, _, card_id = ingest.store(conn, statement, source_ref="msg-ambig")

    assert status == ingest.STATUS_UNPARSED
    assert card_id is None


def test_reingesting_the_same_statement_does_not_duplicate(conn):
    ingest.store(conn, STATEMENT, source_ref="msg-1")
    ingest.store(conn, STATEMENT, source_ref="msg-1")

    card = db.list_cards(conn)[0]
    assert len(db.statements_for_card(conn, card.id)) == 1


def test_store_replaces_prior_cycle_when_statement_date_changes(conn):
    ingest.store(conn, STATEMENT, source_ref="msg-1")
    corrected = ParsedStatement(
        total_due=45231.50,
        min_due=2270.0,
        due_date=date(2026, 8, 15),
        statement_date=date(2026, 7, 28),
        credit_limit=500000.0,
        last4="8765",
        issuer="hdfc",
        parser="hdfc",
    )
    ingest.store(conn, corrected, source_ref="msg-1")

    card = db.list_cards(conn)[0]
    rows = db.statements_for_card(conn, card.id)
    assert len(rows) == 1
    assert rows[0].statement_date == date(2026, 7, 28)
    assert rows[0].due_date == date(2026, 8, 15)


def test_store_moves_statement_off_the_wrong_card_for_same_attachment(conn):
    """Axis bills once fell onto the only registered card; reparse must migrate."""
    wrong = Card(issuer="axis", label="Axis Bank ••0406", last4="0406")
    wrong.id = db.add_card(conn, wrong)
    right = Card(issuer="axis", label="Axis Bank ••0083", last4="0083")
    right.id = db.add_card(conn, right)

    misfiled = ParsedStatement(
        total_due=217.0,
        min_due=10000.0,
        statement_date=date(2025, 10, 12),
        last4="0406",
        issuer="axis",
        parser="axis",
    )
    ingest.store(conn, misfiled, source_ref="msg-axis")

    corrected = ParsedStatement(
        total_due=3692.91,
        min_due=100.0,
        due_date=date(2025, 11, 1),
        statement_date=date(2025, 10, 12),
        last4="0083",
        issuer="axis",
        parser="axis",
    )
    ingest.store(conn, corrected, source_ref="msg-axis")

    assert db.statements_for_card(conn, wrong.id) == []
    rows = db.statements_for_card(conn, right.id)
    assert len(rows) == 1
    assert rows[0].total_due == pytest.approx(3692.91)
    assert rows[0].min_due == pytest.approx(100.0)


def test_retarget_card_last4_renames_a_misread_card(conn):
    card = Card(issuer="hdfc", label="HDFC Bank ••0722", last4="0722")
    card.id = db.add_card(conn, card)

    assert db.retarget_card_last4(conn, issuer="hdfc", from_last4="0722", to_last4="2750")

    updated = db.get_card(conn, card.id)
    assert updated is not None
    assert updated.last4 == "2750"
    assert "••2750" in updated.label
    assert db.find_card(conn, issuer="hdfc", last4="0722") is None


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
