"""Category budgets — CRUD and month spend vs limit."""

from datetime import date

import pytest

from mydues import db
from mydues.models import SOURCE_MANUAL, Expense
from mydues.web.app import create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def _add(conn, *, spent_on, amount, description, category=None):
    return db.add_expense(
        conn,
        Expense(
            spent_on=spent_on,
            amount=amount,
            description=description,
            category=category,
            source=SOURCE_MANUAL,
        ),
    )


def test_upsert_update_delete_budget(conn):
    assert db.upsert_category_budget(conn, "Food & dining", 5000) is True
    rows = db.list_category_budgets(conn)
    assert len(rows) == 1
    assert rows[0]["category"] == "Food & dining"
    assert float(rows[0]["amount_limit"]) == pytest.approx(5000)

    assert db.upsert_category_budget(conn, "food & dining", 6000) is True
    rows = db.list_category_budgets(conn)
    assert len(rows) == 1
    assert float(rows[0]["amount_limit"]) == pytest.approx(6000)

    assert db.delete_category_budget(conn, "Food & dining") is True
    assert db.list_category_budgets(conn) == []


def test_budget_rejects_blank_and_non_positive(conn):
    assert db.upsert_category_budget(conn, "", 100) is False
    assert db.upsert_category_budget(conn, "  ", 100) is False
    assert db.upsert_category_budget(conn, "Food", 0) is False
    assert db.upsert_category_budget(conn, "Food", -10) is False
    assert db.list_category_budgets(conn) == []


def test_budget_status_spent_remaining_and_over(conn):
    db.upsert_category_budget(conn, "Food & dining", 1000)
    db.upsert_category_budget(conn, "Travel", 2000)
    _add(
        conn,
        spent_on=date(2026, 8, 2),
        amount=400,
        description="Lunch",
        category="Food & dining",
    )
    _add(
        conn,
        spent_on=date(2026, 8, 3),
        amount=700,
        description="Dinner",
        category="Food & dining",
    )

    status = {
        row["category"]: row
        for row in db.budget_status_for_month(
            conn, start=date(2026, 8, 1), end=date(2026, 9, 1)
        )
    }
    food = status["Food & dining"]
    assert food["spent"] == pytest.approx(1100)
    assert food["remaining"] == pytest.approx(-100)
    assert food["over"] is True
    travel = status["Travel"]
    assert travel["spent"] == pytest.approx(0)
    assert travel["remaining"] == pytest.approx(2000)
    assert travel["over"] is False


def test_web_budget_appears_and_marks_over(client, conn):
    _add(
        conn,
        spent_on=date(2026, 8, 5),
        amount=2500,
        description="Swiggy",
        category="Food & dining",
    )
    page = client.post(
        "/category-budgets",
        data={"category": "Food & dining", "amount_limit": "1000", "month": "2026-08"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Budgets" in page
    assert "Food & dining" in page
    assert "Over" in page
    assert 'class="budget-row over"' in page or "budget-fill over" in page


def test_month_totals_unchanged_without_budgets(conn):
    _add(conn, spent_on=date(2026, 8, 1), amount=100, description="A", category="Food")
    _add(conn, spent_on=date(2026, 8, 2), amount=200, description="B", category="Travel")
    assert db.expense_total(conn, start=date(2026, 8, 1), end=date(2026, 9, 1)) == pytest.approx(
        300
    )
    assert db.budget_status_for_month(conn, start=date(2026, 8, 1), end=date(2026, 9, 1)) == []
