"""Month-wise billed card totals (10th–9th statement-date window)."""

from datetime import date, datetime

import pytest

from mydues import db
from mydues.models import (
    KIND_CREDIT,
    KIND_DEBIT,
    SOURCE_STATEMENT,
    Card,
    StatementRecord,
    Transaction,
)
from mydues.web.app import _card_billing_window, create_app


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


def _save_due(conn, card: Card, *, total_due: float, statement_date: date) -> int:
    return db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=total_due,
            as_of=datetime.now(),
            source=SOURCE_STATEMENT,
            statement_date=statement_date,
        ),
    )


def test_card_spend_uses_statement_total_due_by_billing_window(conn):
    """HSBC-style: bill amount is statement total_due, not calendar txn_date debits."""
    hsbc = _seed_card(conn, last4="7672", label="HSBC Live+")
    other = _seed_card(conn, last4="1111", label="Other")

    july_id = _save_due(conn, hsbc, total_due=20252.96, statement_date=date(2026, 7, 22))
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
    _save_due(conn, other, total_due=1000.0, statement_date=date(2026, 7, 15))
    _save_due(conn, hsbc, total_due=17367.28, statement_date=date(2026, 6, 22))

    july_start, july_end = _card_billing_window(date(2026, 7, 1))
    assert july_start == date(2026, 7, 10)
    assert july_end == date(2026, 8, 10)
    assert db.card_spend_total(conn, start=july_start, end=july_end) == pytest.approx(
        20252.96 + 1000.0
    )
    by_card = db.card_spend_by_card(conn, start=july_start, end=july_end)
    assert by_card[0]["last4"] == "7672"
    assert by_card[0]["total"] == pytest.approx(20252.96)
    assert by_card[1]["total"] == pytest.approx(1000.0)

    june_start, june_end = _card_billing_window(date(2026, 6, 1))
    assert db.card_spend_total(conn, start=june_start, end=june_end) == pytest.approx(
        17367.28
    )
    assert "2026-07" in db.card_spend_months(conn)


def test_billing_window_boundaries(conn):
    """9th maps to previous month; 10th starts the labeled month."""
    card = _seed_card(conn, last4="9999", label="Boundary")
    _save_due(conn, card, total_due=9.0, statement_date=date(2026, 7, 9))
    _save_due(conn, card, total_due=10.0, statement_date=date(2026, 7, 10))
    _save_due(conn, card, total_due=5.0, statement_date=date(2026, 8, 5))
    _save_due(conn, card, total_due=9.0, statement_date=date(2026, 8, 9))
    _save_due(conn, card, total_due=10.0, statement_date=date(2026, 8, 10))

    june = _card_billing_window(date(2026, 6, 1))
    july = _card_billing_window(date(2026, 7, 1))
    august = _card_billing_window(date(2026, 8, 1))

    assert db.card_spend_total(conn, start=june[0], end=june[1]) == pytest.approx(9.0)
    assert db.card_spend_total(conn, start=july[0], end=july[1]) == pytest.approx(
        10.0 + 5.0 + 9.0
    )
    assert db.card_spend_total(conn, start=august[0], end=august[1]) == pytest.approx(
        10.0
    )
    months = db.card_spend_months(conn)
    assert "2026-06" in months
    assert "2026-07" in months
    assert "2026-08" in months
    # 5 Aug (and 9 Aug) belong to July's window, not a bare calendar August key
    # from strftime — Aug 10 alone creates 2026-08.


def test_cards_tab_shows_billed_month_total(client, conn):
    card = _seed_card(conn, last4="7672", label="HSBC Live+")
    _save_due(conn, card, total_due=20252.96, statement_date=date(2026, 7, 22))
    page = client.get("/?tab=cards&month=2026-07").get_data(as_text=True)
    assert "Billed in July 2026" in page
    assert "20,252.96" in page or "20252.96" in page
    assert "HSBC Live+" in page
    assert "10th of this month" in page
