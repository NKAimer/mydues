"""Month-wise billed card totals (by statement date)."""

from datetime import date, datetime

import pytest

from carddues import db
from carddues.models import (
    KIND_CREDIT,
    KIND_DEBIT,
    SOURCE_STATEMENT,
    Card,
    StatementRecord,
    Transaction,
)
from carddues.web.app import create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def _seed_card(conn, *, last4: str, label: str) -> Card:
    card = Card(issuer="hdfc", label=label, last4=last4)
    card.id = db.add_card(conn, card)
    return card


def test_card_spend_uses_statement_total_due_by_statement_date(conn):
    """HSBC-style: bill amount is statement total_due, not calendar txn_date debits."""
    hsbc = _seed_card(conn, last4="7672", label="HSBC Live+")
    other = _seed_card(conn, last4="1111", label="Other")

    july_id = db.save_statement(
        conn,
        StatementRecord(
            card_id=hsbc.id,
            total_due=20252.96,
            as_of=datetime.now(),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 7, 22),
        ),
    )
    # Purchases on the July bill can have June txn_dates; those must not
    # shrink the July billed total.
    db.save_transactions(
        conn,
        hsbc.id,
        july_id,
        [
            Transaction(
                description="JUNE PURCHASE",
                amount=5000.0,
                txn_date=date(2026, 6, 28),
                kind=KIND_DEBIT,
            ),
            Transaction(
                description="JULY PURCHASE",
                amount=16350.0,
                txn_date=date(2026, 7, 5),
                kind=KIND_DEBIT,
            ),
            Transaction(
                description="PAYMENT",
                amount=18477.0,
                txn_date=date(2026, 7, 10),
                kind=KIND_CREDIT,
            ),
        ],
    )
    db.save_statement(
        conn,
        StatementRecord(
            card_id=other.id,
            total_due=1000.0,
            as_of=datetime.now(),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 7, 15),
        ),
    )
    db.save_statement(
        conn,
        StatementRecord(
            card_id=hsbc.id,
            total_due=17367.28,
            as_of=datetime.now(),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 6, 22),
        ),
    )

    july_start, july_end = date(2026, 7, 1), date(2026, 8, 1)
    assert db.card_spend_total(conn, start=july_start, end=july_end) == pytest.approx(
        20252.96 + 1000.0
    )
    by_card = db.card_spend_by_card(conn, start=july_start, end=july_end)
    assert by_card[0]["last4"] == "7672"
    assert by_card[0]["total"] == pytest.approx(20252.96)
    assert by_card[1]["total"] == pytest.approx(1000.0)
    assert db.card_spend_total(conn, start=date(2026, 6, 1), end=july_start) == pytest.approx(
        17367.28
    )
    assert "2026-07" in db.card_spend_months(conn)


def test_cards_tab_shows_billed_month_total(client, conn):
    card = _seed_card(conn, last4="7672", label="HSBC Live+")
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=20252.96,
            as_of=datetime.now(),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 7, 22),
        ),
    )
    page = client.get("/?tab=cards&month=2026-07").get_data(as_text=True)
    assert "Billed in July 2026" in page
    assert "20,252.96" in page or "20252.96" in page
    assert "HSBC Live+" in page
    assert "statement date" in page.lower()
