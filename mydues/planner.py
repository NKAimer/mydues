"""Monthly cashflow planner: salary funds the next calendar month."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from math import ceil

from . import db

DEFAULT_RECURRING_LABELS = (
    ("Rent", 0.0, 1),
    ("Electricity", 0.0, 5),
    ("Wifi", 0.0, 7),
    ("Mobile", 0.0, 10),
)


def next_month_key(day: date) -> str:
    """Plan month funded by a credit on ``day`` (typically month-end → next month)."""
    if day.month == 12:
        return f"{day.year + 1}-01"
    return f"{day.year}-{day.month + 1:02d}"


def prior_month_end(plan_month: str) -> date:
    """Last calendar day of the month before the plan month (usual salary credit day)."""
    year, month = (int(x) for x in plan_month.split("-", 1))
    if month == 1:
        return date(year - 1, 12, 31)
    last = calendar.monthrange(year, month - 1)[1]
    return date(year, month - 1, last)


def parse_month_key(month_key: str) -> date:
    year, month = (int(x) for x in month_key.split("-", 1))
    return date(year, month, 1)


def shift_month_key(month_key: str, delta: int) -> str:
    start = parse_month_key(month_key)
    month = start.month - 1 + delta
    year = start.year + month // 12
    month = month % 12 + 1
    return f"{year}-{month:02d}"


@dataclass
class PlannerView:
    plan_month: str
    plan_month_label: str
    salary: float
    credited_on: date | None
    recurring_total: float
    planned_total: float
    outflows: float
    savings: float
    actual_spend: float
    actual_vs_planned: float
    recurrings: list[dict] = field(default_factory=list)
    planned: list[dict] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    salary_note: str | None = None
    has_income: bool = False


def build_planner(conn, plan_month: str) -> PlannerView:
    income = db.get_monthly_income(conn, plan_month)
    salary = float(income["amount"]) if income else 0.0
    credited_on = db._as_date(income["credited_on"]) if income and income["credited_on"] else None
    note = income["note"] if income else None
    has_income = income is not None

    recurrings = db.list_recurring_expenses(conn, active_only=False)
    paid_ids = db.paid_recurring_ids(conn, plan_month)
    recurring_rows: list[dict] = []
    recurring_total = 0.0
    for row in recurrings:
        amount = float(row["amount"])
        active = bool(row["active"])
        if active and amount > 0:
            recurring_total += amount
        recurring_rows.append(
            {
                "id": row["id"],
                "label": row["label"],
                "amount": amount,
                "day_of_month": row["day_of_month"],
                "active": active,
                "paid": row["id"] in paid_ids,
            }
        )

    planned_rows = [
        {
            "id": row["id"],
            "label": row["label"],
            "amount": float(row["amount"]),
        }
        for row in db.list_planned_expenses(conn, plan_month)
    ]
    planned_total = sum(r["amount"] for r in planned_rows)

    outflows = recurring_total + planned_total
    savings = round(salary - outflows, 2)

    start = parse_month_key(plan_month)
    end = parse_month_key(shift_month_key(plan_month, 1))
    actual_spend = round(db.expense_total(conn, start=start, end=end), 2)
    actual_vs_planned = round(actual_spend - outflows, 2)

    goals: list[dict] = []
    for goal in db.list_savings_goals(conn):
        target = float(goal["target_amount"])
        months = ceil(target / savings) if savings > 0 else None
        goals.append(
            {
                "id": goal["id"],
                "label": goal["label"],
                "target_amount": target,
                "months_at_pace": months,
                "pct": min(100.0, round(100.0 * max(savings, 0) / target, 1)) if target else 0.0,
            }
        )

    timeline = sorted(
        [
            r
            for r in recurring_rows
            if r["active"] and r["day_of_month"]
        ],
        key=lambda r: (int(r["day_of_month"]), r["label"].lower()),
    )

    label = parse_month_key(plan_month).strftime("%B %Y")
    return PlannerView(
        plan_month=plan_month,
        plan_month_label=label,
        salary=salary,
        credited_on=credited_on,
        recurring_total=round(recurring_total, 2),
        planned_total=round(planned_total, 2),
        outflows=round(outflows, 2),
        savings=savings,
        actual_spend=actual_spend,
        actual_vs_planned=actual_vs_planned,
        recurrings=recurring_rows,
        planned=planned_rows,
        goals=goals,
        timeline=timeline,
        salary_note=note,
        has_income=has_income,
    )


def seed_default_recurrings(conn) -> int:
    """Insert Rent/Electricity/Wifi/Mobile if those labels are missing. Returns added count."""
    existing = {row["label"].strip().lower() for row in db.list_recurring_expenses(conn)}
    added = 0
    for label, amount, day in DEFAULT_RECURRING_LABELS:
        if label.lower() in existing:
            continue
        db.add_recurring_expense(conn, label=label, amount=amount, day_of_month=day)
        added += 1
    return added
