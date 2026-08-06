"""Charge / EMI / interest / fee heuristics."""

from datetime import date, datetime

import pytest

from mydues import charges, db
from mydues.categories import resolve_category
from mydues.charges import (
    CHARGE_ANNUAL_FEE,
    CHARGE_EMI,
    CHARGE_FEE,
    CHARGE_INTEREST,
    CHARGE_LATE_FEE,
    detect_charge_kind,
)
from mydues.models import (
    CATEGORY_GUESS,
    CATEGORY_USER,
    KIND_DEBIT,
    SOURCE_MANUAL,
    SOURCE_STATEMENT,
    Card,
    Expense,
    StatementRecord,
    Transaction,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Annual fee FY26-27", CHARGE_ANNUAL_FEE),
        ("Joining fee", CHARGE_ANNUAL_FEE),
        ("Membership fee charged", CHARGE_ANNUAL_FEE),
        ("Finance charge", CHARGE_INTEREST),
        ("Revolving interest", CHARGE_INTEREST),
        ("Late payment fee", CHARGE_LATE_FEE),
        ("Late fee Aug", CHARGE_LATE_FEE),
        ("Easy EMI installment", CHARGE_EMI),
        ("LOAN INSTALLMENT", CHARGE_EMI),
        ("EMI 05/08", CHARGE_EMI),
        ("Service fee", CHARGE_FEE),
        ("Processing fee", CHARGE_FEE),
        ("SWIGGY BANGALORE", None),
        ("Coffee day", None),
        ("Premium store", None),
        ("CHEMIST", None),
    ],
)
def test_detect_charge_kind_table(text, expected):
    assert detect_charge_kind(text) == expected


def test_emi_maps_to_emi_category_on_guess_path(conn):
    category, source = resolve_category("Easy EMI installment", conn=conn)
    assert category == "EMI"
    assert source == CATEGORY_GUESS


def test_user_category_not_overwritten_by_charge_reapply(conn):
    eid = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=500,
            description="Annual fee",
            category="Shopping",
            category_source=CATEGORY_USER,
            source=SOURCE_MANUAL,
        ),
    )
    assert eid is not None
    stats = __import__("mydues.categories", fromlist=["apply_category_rules"]).apply_category_rules(
        conn
    )
    expense = db.get_expense(conn, eid)
    assert expense.category == "Shopping"
    assert expense.category_source == CATEGORY_USER
    assert expense.charge_kind is None
    assert stats["expenses_updated"] == 0


def test_backfill_classifies_null_kinds_once(conn):
    card_id = db.add_card(conn, Card(issuer="hdfc", label="HDFC", last4="1234"))
    sid = db.save_statement(
        conn,
        StatementRecord(
            card_id=card_id,
            total_due=1000,
            statement_date=date(2026, 8, 1),
            source=SOURCE_STATEMENT,
            as_of=datetime.now(),
        ),
    )
    db.save_transactions(
        conn,
        card_id,
        sid,
        [
            Transaction(description="SWIGGY", amount=200, kind=KIND_DEBIT),
            Transaction(description="Annual membership fee", amount=500, kind=KIND_DEBIT),
        ],
    )
    # Clear kinds to simulate legacy rows, then re-init backfill.
    conn.execute("UPDATE transactions SET charge_kind = NULL")
    conn.commit()
    db._backfill_charge_kinds(conn)
    kinds = {
        row["description"]: row["charge_kind"]
        for row in conn.execute("SELECT description, charge_kind FROM transactions")
    }
    assert kinds["SWIGGY"] is None
    assert kinds["Annual membership fee"] == CHARGE_ANNUAL_FEE
    # Already-set kinds left alone.
    conn.execute(
        "UPDATE transactions SET charge_kind = ? WHERE description = ?",
        (CHARGE_FEE, "Annual membership fee"),
    )
    conn.commit()
    db._backfill_charge_kinds(conn)
    row = conn.execute(
        "SELECT charge_kind FROM transactions WHERE description = ?",
        ("Annual membership fee",),
    ).fetchone()
    assert row["charge_kind"] == CHARGE_FEE


def test_expenses_are_not_charge_flagged(conn):
    eid = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 2),
            amount=500,
            description="Annual fee",
            source=SOURCE_MANUAL,
        ),
    )
    expense = db.get_expense(conn, eid)
    assert expense is not None
    assert expense.charge_kind is None
    # Legacy flag is cleared on backfill.
    conn.execute(
        "UPDATE expenses SET charge_kind = ? WHERE id = ?",
        (CHARGE_ANNUAL_FEE, eid),
    )
    conn.commit()
    db._backfill_charge_kinds(conn)
    expense = db.get_expense(conn, eid)
    assert expense.charge_kind is None


def test_charge_label_helper():
    assert charges.charge_label(CHARGE_ANNUAL_FEE) == "Annual fee"
    assert charges.charge_label(None) is None
