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
    assert expense.amount == pytest.approx(99.0)
    # No usable merchant in body/subject → placeholder, never OTP boilerplate.
    assert "Do not share OTP" not in expense.description
    assert "confirm that" not in expense.description.lower()
    assert len(expense.description) <= expense_ingest._DESC_MAX
    if expense.note:
        assert len(expense.note) <= expense_ingest._NOTE_MAX


def test_parse_hsbc_purchase_at_merchant():
    expense = expense_ingest.parse_alert_email(
        subject="Please confirm that your Credit card no ending with 7672",
        body=(
            "You have used your HSBC Credit Card ending with 7672 for a purchase "
            "transaction of Rs.1,250.00 at APOLLO PHARMACY IND on 01-08-2026."
        ),
        received_at=datetime(2026, 8, 1, 10, 0),
    )
    assert expense is not None
    assert expense.amount == pytest.approx(1250.0)
    assert "APOLLO" in expense.description.upper()
    assert "confirm that" not in expense.description.lower()
    assert expense.category == "Health"


def test_parse_rejects_visa_marketing_as_description():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert: INR 10.00 spent",
        body=(
            "Rs. 10.00 spent at a world of Visa Infinite benefits Everyday cashback "
            "using your card on 01-08-2026."
        ),
        received_at=datetime(2026, 8, 1, 11, 0),
    )
    assert expense is not None
    assert "visa infinite" not in expense.description.lower()
    assert "everyday cashback" not in expense.description.lower()


def test_parse_hdfc_upi_prefers_body_merchant_over_vpa_subject():
    expense = expense_ingest.parse_alert_email(
        subject="UPI txn uber1.rzp@hdfcbank Date",
        body=(
            "HDFC BANK --> Dear Customer, You have done a UPI txn. "
            "Rs. 220.00 paid to UBER INDIA on 01-08-2026 via UPI."
        ),
        received_at=datetime(2026, 8, 1, 12, 0),
    )
    assert expense is not None
    assert "UBER" in expense.description.upper()
    assert "confirm that" not in expense.description.lower()


def test_parse_hdfc_payment_was_made_alert():
    expense = expense_ingest.parse_alert_email(
        subject="A payment was made using your Credit Card",
        body=(
            "HDFC BANK --> Dear Customer, Greetings from HDFC Bank. We would like "
            "to inform you that Rs. 335.00 has been debited from your HDFC Bank "
            "Credit Card ending 6527 towards SWIGGY PVT LTD FOOD2 on 01 Aug, 2026 "
            "at 20:36:36."
        ),
        received_at=datetime(2026, 8, 1, 20, 36),
    )
    assert expense is not None
    assert expense.amount == pytest.approx(335.0)
    assert expense.spent_on == date(2026, 8, 1)
    assert "SWIGGY" in expense.description.upper()


def test_parse_sbi_phonepe_spent_at_merchant():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert from PhonePe SBI card SELECT BLACK",
        body=(
            "SBI Having trouble viewing this e-mail? Dear Cardholder, This is to "
            "inform you that, Rs.938.70 spent on your SBI Credit Card ending with "
            "3418 at BharatConnectUtiliti on 01-08-26 via UPI (Ref No. 476684159067)."
        ),
        received_at=datetime(2026, 8, 1, 12, 0),
    )
    assert expense is not None
    assert expense.amount == pytest.approx(938.7)
    assert "BHARATCONNECTUTILITI" in expense.description.upper()
    assert "inform you that" not in expense.description.lower()


def test_parse_upi_payment_alert():
    expense = expense_ingest.parse_alert_email(
        subject="UPI payment of Rs.2550.00",
        body="You paid Rs.2550.00 to MERCHANT STORE via UPI on 25-07-2026.",
        received_at=datetime(2026, 7, 25, 14, 0),
    )
    assert expense is not None
    assert expense.amount == pytest.approx(2550.0)
    assert expense.spent_on == date(2026, 7, 25)
    assert "MERCHANT" in expense.description.upper()


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


def test_parse_alert_skips_imps_and_neft_transfers():
    assert (
        expense_ingest.parse_alert_email(
            subject="IMPS Transaction Alert: INR 5000.00 debited",
            body=(
                "Rs. 5000.00 has been debited from your Account XXXX1234 via IMPS "
                "towards JOHN DOE on 01-08-2026."
            ),
            received_at=datetime(2026, 8, 1, 12, 0),
        )
        is None
    )
    assert (
        expense_ingest.parse_alert_email(
            subject="NEFT Alert from your bank",
            body=(
                "Dear Customer, Rs. 12,500.00 has been debited from A/c XX5678 "
                "towards RENT PAYMENT via NEFT on 01-08-2026."
            ),
            received_at=datetime(2026, 8, 1, 12, 0),
        )
        is None
    )
    # Generic debit subject; transfer type only in the body.
    assert (
        expense_ingest.parse_alert_email(
            subject="Transaction Alert: INR 2000.00 debited",
            body=(
                "Rs. 2000.00 has been debited from your savings account via IMPS "
                "to BENEFICIARY NAME on 01-08-2026. Ref No. 123456."
            ),
            received_at=datetime(2026, 8, 1, 12, 0),
        )
        is None
    )


def test_alert_query_requires_transaction_subjects_and_skips_loans():
    query = expense_ingest.build_alert_query(7)
    assert "newer_than:7d" in query
    assert 'subject:"transaction alert"' in query
    assert 'subject:"debited"' in query
    assert 'subject:"upi"' in query
    assert 'subject:"upi payment"' in query
    assert 'subject:"upi alert"' in query
    assert 'subject:"payment was made using your credit card"' in query
    assert '-subject:"loan"' in query
    assert '-subject:"pre-approved"' in query
    assert '-subject:"imps"' in query
    assert '-subject:"neft"' in query
    assert "from:(" in query
    assert "hdfcbank.bank.in" in query or "hdfcbank.com" in query
    assert "sbicard.com" in query
    assert "debited" in query
    assert "Rs." in query or '"Rs."' in query
    # Must not match bank mail without a spend cue.
    assert " OR " in query
    assert "-has:attachment" in query


def test_parse_unknown_subject_with_body_spend_cues():
    """Novel subject still parses when the body has bank-agnostic spend wording."""
    expense = expense_ingest.parse_alert_email(
        subject="Important update on your account",
        body=(
            "Dear Customer, Rs. 499.00 has been debited from your Credit Card "
            "ending 9999 towards ZOMATO ONLINE on 01-08-2026. "
            "Also see our EMI of Rs. 2,500.00 if you convert this purchase."
        ),
        received_at=datetime(2026, 8, 1, 9, 0),
    )
    assert expense is not None
    assert expense.amount == pytest.approx(499.0)
    assert "ZOMATO" in expense.description.upper()


def test_refresh_expense_from_gmail_updates_bad_description(conn, monkeypatch):
    from carddues import gmail
    from carddues.models import CATEGORY_USER

    expense_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=1250.0,
            description="confirm that your Credit card no ending with 7672",
            category=None,
            category_source=None,
            source=SOURCE_GMAIL,
            source_ref="msg-refresh-1",
        ),
    )
    message = gmail.Message(
        id="msg-refresh-1",
        sender="alerts@hsbc.co.in",
        subject="Please confirm that your Credit card no ending with 7672",
        internal_date=int(datetime(2026, 8, 1, 10, 0).timestamp() * 1000),
        body=(
            "You have used your HSBC Credit Card ending with 7672 for a purchase "
            "transaction of Rs.1,250.00 at APOLLO PHARMACY IND on 01-08-2026."
        ),
    )
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    monkeypatch.setattr(gmail, "service", lambda **_: object())
    monkeypatch.setattr(gmail, "get_message", lambda *_a, **_k: message)

    expense = db.get_expense(conn, expense_id)
    assert expense_ingest.refresh_expense_from_gmail(conn, expense) is True
    updated = db.get_expense(conn, expense_id)
    assert "APOLLO" in updated.description.upper()
    assert updated.category == "Health"

    # Manual category is preserved on refresh.
    db.update_expense(
        conn,
        expense_id,
        spent_on=updated.spent_on,
        amount=updated.amount,
        description=updated.description,
        category="Travel",
        category_source=CATEGORY_USER,
        note=updated.note,
    )
    expense = db.get_expense(conn, expense_id)
    expense_ingest.refresh_expense_from_gmail(conn, expense)
    preserved = db.get_expense(conn, expense_id)
    assert preserved.category == "Travel"
    assert preserved.category_source == CATEGORY_USER
    assert "APOLLO" in preserved.description.upper()


def test_expenses_tab_shows_refresh_affordance(client, monkeypatch):
    from carddues import gmail

    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    page = client.get("/?tab=expenses").get_data(as_text=True)
    assert "Refresh descriptions from Gmail" in page
    assert 'name="days"' in page
    assert 'value="7"' in page
    assert "Fetch expense alerts" in page or "Fetch from Gmail" in page
    assert "Look back" in page


def test_parse_lookback_days_defaults_and_clamps():
    from carddues.web.app import _parse_lookback_days, _parse_lookback_months

    assert _parse_lookback_days(None) == 7
    assert _parse_lookback_days("") == 7
    assert _parse_lookback_days("14") == 14
    assert _parse_lookback_days("0") == 1
    assert _parse_lookback_days("9999") == 400
    assert _parse_lookback_days("nope") == 7
    assert _parse_lookback_months(None) == 1
    assert _parse_lookback_months("3") == 3
    assert _parse_lookback_months("0") == 1
    assert _parse_lookback_months("99") == 24


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


def test_show_email_loads_gmail_message(client, conn, monkeypatch):
    from carddues import gmail

    expense_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 7, 28),
            amount=433.0,
            description="SWIGGY",
            source=SOURCE_GMAIL,
            source_ref="msg-show-1",
        ),
    )
    message = gmail.Message(
        id="msg-show-1",
        sender="alerts@hdfcbank.net",
        subject="Transaction Alert: Rs.433 spent",
        internal_date=int(datetime(2026, 7, 28, 10, 0).timestamp() * 1000),
        body="Rs. 433.00 spent at SWIGGY BANGALORE using your card.",
    )
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    monkeypatch.setattr(gmail, "service", lambda **_: object())
    monkeypatch.setattr(gmail, "get_message", lambda *_a, **_k: message)

    page = client.get(f"/expenses/{expense_id}/email").get_data(as_text=True)
    assert "Transaction Alert: Rs.433 spent" in page
    assert "SWIGGY BANGALORE" in page
    assert "Back to expenses" in page


def test_show_email_requires_gmail_source(client, conn):
    expense_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 7, 1),
            amount=10.0,
            description="Manual",
            source=SOURCE_MANUAL,
        ),
    )
    response = client.get(f"/expenses/{expense_id}/email", follow_redirects=True)
    assert "not imported from Gmail" in response.get_data(as_text=True)
