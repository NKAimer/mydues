"""Expenses ledger — separate from credit-card dues."""

from datetime import date, datetime

import pytest

from carddues import db, expense_ingest
from carddues.models import SOURCE_GMAIL, SOURCE_MANUAL, Expense
from carddues.web.app import create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_add_and_list_expenses_for_a_month(conn):
    assert (
        db.add_expense(
            conn,
            Expense(
                spent_on=date(2026, 8, 1),
                amount=250.0,
                description="Coffee",
                category="Food",
                source=SOURCE_MANUAL,
            ),
        )
        is not None
    )
    db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 15),
            amount=1200.0,
            description="Groceries",
            source=SOURCE_MANUAL,
        ),
    )
    db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 7, 30),
            amount=99.0,
            description="July only",
            source=SOURCE_MANUAL,
        ),
    )

    august = db.list_expenses(conn, start=date(2026, 8, 1), end=date(2026, 9, 1))
    assert len(august) == 2
    assert db.expense_total(conn, start=date(2026, 8, 1), end=date(2026, 9, 1)) == pytest.approx(
        1450.0
    )
    assert "2026-08" in db.expense_months(conn)


def test_gmail_source_ref_dedupes(conn):
    first = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=100.0,
            description="UPI Swiggy",
            source=SOURCE_GMAIL,
            source_ref="msg-1",
        ),
    )
    second = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=100.0,
            description="UPI Swiggy again",
            source=SOURCE_GMAIL,
            source_ref="msg-1",
        ),
    )
    assert first is not None
    assert second is None
    assert len(db.list_expenses(conn, start=date(2026, 8, 1), end=date(2026, 9, 1))) == 1


def test_parse_alert_email_extracts_spend():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert: INR 420.00 spent",
        body="Rs. 420.00 spent at SWIGGY BANGALORE on 01-08-2026 using your card.",
        received_at=datetime(2026, 8, 1, 12, 0),
    )
    assert expense is not None
    assert expense.amount == pytest.approx(420.0)
    assert expense.spent_on == date(2026, 8, 1)
    assert "SWIGGY" in expense.description.upper()
    assert expense.source == SOURCE_GMAIL
    if expense.note:
        assert len(expense.note) <= expense_ingest._NOTE_MAX
        assert "Do not share OTP" not in (expense.note or "")


def test_parse_alert_falls_back_to_cleaned_subject():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert: INR 99.00 spent",
        body="Rs. 99.00 was debited. Do not share OTP with anyone. Click here for help.",
        received_at=datetime(2026, 8, 2, 9, 0),
    )
    assert expense is not None
    assert "INR 99" in expense.description or "99.00" in expense.description
    assert "Do not share OTP" not in expense.description
    assert len(expense.description) <= expense_ingest._DESC_MAX
    if expense.note:
        assert len(expense.note) <= expense_ingest._NOTE_MAX


def test_parse_alert_skips_statement_subjects():
    assert (
        expense_ingest.parse_alert_email(
            subject="Your credit card statement is ready",
            body="Rs. 5000.00 total due",
        )
        is None
    )


def test_parse_alert_skips_loan_offers():
    assert (
        expense_ingest.parse_alert_email(
            subject="Pre-approved personal loan offer for you",
            body="Get a loan of Rs. 5,00,000. Apply now. Limited period offer.",
        )
        is None
    )
    assert (
        expense_ingest.parse_alert_email(
            subject="Transaction Alert: INR 100.00 spent",
            body="Rs. 100.00 spent at CAFE. Also enjoy our personal loan offer.",
        )
        is None
    )


def test_alert_query_requires_transaction_subjects_and_skips_loans():
    query = expense_ingest.build_alert_query(90)
    assert 'subject:"transaction alert"' in query
    assert 'subject:"debited"' in query
    assert '-subject:"loan"' in query
    assert '-subject:"pre-approved"' in query
    assert "from:" not in query
    assert " OR subject:" in query or 'subject:"' in query
    # Must not fall back to matching any mail from bank domains alone.
    assert "from:hdfcbank" not in query


def test_expenses_tab_and_manual_add(client, conn):
    page = client.get("/?tab=expenses").get_data(as_text=True)
    assert "Add expense" in page
    assert 'href="/?tab=cards"' in page or "tab=cards" in page

    response = client.post(
        "/expenses",
        data={
            "spent_on": "01/08/2026",
            "amount": "350.50",
            "description": "Metro card",
            "category": "Transit",
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Metro card" in body
    assert "350.50" in body or "350.5" in body
    rows = db.list_expenses(conn, start=date(2026, 8, 1), end=date(2026, 9, 1))
    assert len(rows) == 1
    assert rows[0].description == "Metro card"


def test_edit_and_delete_expense(client, conn):
    expense_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 5),
            amount=100.0,
            description="Old name",
            category="Misc",
            source=SOURCE_MANUAL,
        ),
    )
    assert expense_id is not None

    page = client.post(
        f"/expenses/{expense_id}/edit",
        data={
            "spent_on": "06/08/2026",
            "amount": "175.25",
            "description": "New name",
            "category": "Food",
            "note": "Paid via UPI",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "New name" in page
    assert "Paid via UPI" in page
    assert "175.25" in page or "175.2" in page

    updated = db.get_expense(conn, expense_id)
    assert updated is not None
    assert updated.description == "New name"
    assert updated.amount == pytest.approx(175.25)
    assert updated.spent_on == date(2026, 8, 6)
    assert updated.category == "Food"
    assert updated.note == "Paid via UPI"

    client.post(f"/expenses/{expense_id}/delete", follow_redirects=True)
    assert db.get_expense(conn, expense_id) is None
