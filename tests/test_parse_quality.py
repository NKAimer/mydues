"""Parse quality contract and audit helpers."""

from datetime import date, datetime

import pytest

from mydues import audit, db, dues, ingest, parse_quality
from mydues.models import SOURCE_STATEMENT, Card, ParsedStatement, StatementRecord


def test_assess_flags_missing_due_and_empty_txns():
    weak = ParsedStatement(total_due=12000.0, last4="2653", issuer="yesbank")
    report = parse_quality.assess(weak)
    assert not report.hard_ok
    assert report.transactions_incomplete


def test_reparse_refuses_txn_collapse(conn):
    card = Card(issuer="hdfc", label="HDFC", last4="8765")
    card.id = db.add_card(conn, card)
    previous = StatementRecord(
        card_id=card.id,
        total_due=12000.0,
        min_due=500.0,
        due_date=date(2026, 8, 1),
        statement_date=date(2026, 7, 12),
        source=SOURCE_STATEMENT,
        source_ref="msg-1",
        as_of=datetime(2026, 7, 13),
    )
    previous.id = db.save_statement(conn, previous)
    worse = ParsedStatement(
        total_due=12000.0,
        min_due=500.0,
        due_date=date(2026, 8, 1),
        statement_date=date(2026, 7, 12),
        last4="8765",
        issuer="hdfc",
        transactions=[],
    )
    status, detail, _ = ingest.store(
        conn,
        worse,
        source_ref="msg-1",
        previous=previous,
        previous_txn_count=22,
    )
    assert status == ingest.STATUS_ERROR
    assert "collapsed" in detail


def test_reparse_allows_fixing_phone_number_total():
    """8250825 (ERGO Toll Free) → real bill must not trip the collapse guard."""
    from mydues.models import Transaction

    previous = StatementRecord(
        card_id=1,
        total_due=8250825.0,
        min_due=800.0,
        due_date=date(2026, 5, 21),
        statement_date=date(2026, 5, 1),
        source=SOURCE_STATEMENT,
        source_ref="msg-phone",
        as_of=datetime(2026, 5, 2),
    )
    fixed = ParsedStatement(
        total_due=172729.0,
        min_due=8640.0,
        due_date=date(2026, 5, 21),
        statement_date=date(2026, 5, 1),
        last4="2750",
        issuer="hdfc",
        transactions=[
            Transaction(description=f"t{i}", amount=10.0, txn_date=date(2026, 4, 3))
            for i in range(8)
        ],
    )
    assert parse_quality.reparse_regression(
        previous, previous_txn_count=8, new=fixed
    ) is None


def test_reparse_refuses_real_bill_to_stub_total():
    previous = StatementRecord(
        card_id=1,
        total_due=12000.0,
        min_due=500.0,
        due_date=date(2026, 8, 1),
        statement_date=date(2026, 7, 12),
        source=SOURCE_STATEMENT,
        as_of=datetime(2026, 7, 13),
    )
    stub = ParsedStatement(
        total_due=200.0,
        min_due=6.0,
        due_date=date(2026, 8, 1),
        statement_date=date(2026, 7, 12),
        last4="8765",
        issuer="hdfc",
        transactions=[],
    )
    reason = parse_quality.reparse_regression(
        previous, previous_txn_count=2, new=stub
    )
    assert reason is not None
    assert "collapsed" in reason


def test_undated_junk_does_not_outrank_a_real_cycle(conn):
    card = Card(issuer="yesbank", label="YES Bank ••2653", last4="2653")
    card.id = db.add_card(conn, card)
    real = StatementRecord(
        card_id=card.id,
        total_due=12255.0,
        min_due=245.1,
        due_date=date(2026, 8, 1),
        statement_date=date(2026, 7, 12),
        source=SOURCE_STATEMENT,
        source_ref="msg-real",
        as_of=datetime(2026, 7, 13),
    )
    junk = StatementRecord(
        card_id=card.id,
        total_due=200.0,
        min_due=6.0,
        source=SOURCE_STATEMENT,
        source_ref="msg-agreement",
        as_of=datetime(2026, 8, 1, 12, 0, 0),
    )
    db.save_statement(conn, real)
    db.save_statement(conn, junk)
    view = dues.view_for_card(conn, card, today=date(2026, 8, 1))
    assert view.record is not None
    assert view.record.total_due == pytest.approx(12255.0)
    assert view.statement_date == date(2026, 7, 12)


def test_audit_reports_missing_transactions(conn):
    card = Card(issuer="yesbank", label="YES Bank ••2653", last4="2653")
    card.id = db.add_card(conn, card)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=12255.0,
            min_due=245.1,
            due_date=date(2026, 8, 1),
            statement_date=date(2026, 7, 12),
            source=SOURCE_STATEMENT,
            as_of=datetime(2026, 7, 13),
        ),
    )
    findings = audit.audit_cards(conn)
    kinds = {f.kind for f in findings if f.card_id == card.id}
    assert "transactions_missing" in kinds
