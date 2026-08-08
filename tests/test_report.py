"""Month-close Report tab — snapshot of planner, budgets, and dues."""

from datetime import date, datetime

import pytest

from mydues import db
from mydues.models import SOURCE_MANUAL, SOURCE_STATEMENT, Card, Expense, StatementRecord
from mydues.web.app import create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def _seed_report(conn):
    db.upsert_monthly_income(
        conn,
        month="2026-08",
        amount=100_000,
        credited_on=date(2026, 7, 31),
    )
    db.add_recurring_expense(conn, label="Rent", amount=30_000, day_of_month=1)
    db.add_savings_goal(conn, label="Emergency", target_amount=90_000)
    db.upsert_category_budget(conn, "Food & dining", 1_000)
    db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 5),
            amount=1_500,
            description="Dinner",
            category="Food & dining",
            source=SOURCE_MANUAL,
        ),
    )
    card = Card(issuer="hdfc", label="HDFC Millennia", last4="4321")
    card.id = db.add_card(conn, card)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=12_000,
            min_due=600,
            due_date=date(2026, 8, 20),
            statement_date=date(2026, 8, 1),
            source=SOURCE_STATEMENT,
            source_ref="report-stmt",
            as_of=datetime.now(),
        ),
    )
    return card


def test_report_tab_renders_snapshot_sections(client, conn):
    _seed_report(conn)
    page = client.get("/?tab=report&month=2026-08").get_data(as_text=True)

    assert "<title>Report — Mydues</title>" in page
    assert 'class="tab active"' in page and "Report" in page
    assert "Month-close snapshot" in page
    assert "Planned outflows" in page
    assert "Actual spend" in page
    assert "Cards to pay" in page
    assert "HDFC Millennia" in page
    assert "Budget overruns" in page
    assert "Food &amp; dining" in page or "Food & dining" in page
    assert "Goals pace" in page
    assert "Emergency" in page
    assert "Open checklist" in page
    assert 'href="/?tab=cards&amp;highlight=pay-checklist"' in page or (
        "highlight=pay-checklist" in page and "tab=cards" in page
    )


def test_report_tab_empty_state_copy(client, conn):
    page = client.get("/?tab=report&month=2020-01").get_data(as_text=True)

    assert "No unpaid cards" in page
    assert "No budgets set for this month" in page
    assert "No savings goals yet" in page
    assert "read-only" in page.lower() or "Edit plan" in page
