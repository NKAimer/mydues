"""Monthly cashflow planner — mapping, savings, paid marks, defaults."""

from datetime import date

import pytest

from mydues import db, planner
from mydues.models import SOURCE_MANUAL, Expense
from mydues.web.app import _parse_amount, create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_next_month_key_and_prior_month_end():
    assert planner.next_month_key(date(2026, 8, 31)) == "2026-09"
    assert planner.next_month_key(date(2026, 12, 31)) == "2027-01"
    assert planner.prior_month_end("2026-09") == date(2026, 8, 31)
    assert planner.prior_month_end("2027-01") == date(2026, 12, 31)


def test_build_planner_savings_and_actual(conn):
    db.upsert_monthly_income(
        conn,
        month="2026-09",
        amount=100_000,
        credited_on=date(2026, 8, 31),
    )
    db.add_recurring_expense(conn, label="Rent", amount=30_000, day_of_month=1)
    db.add_planned_expense(conn, month="2026-09", label="Gift", amount=5_000)
    db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 9, 4),
            amount=2_000,
            description="Coffee",
            source=SOURCE_MANUAL,
        ),
    )

    view = planner.build_planner(conn, "2026-09")
    assert view.salary == pytest.approx(100_000)
    assert view.has_income is True
    assert view.recurring_total == pytest.approx(30_000)
    assert view.planned_total == pytest.approx(5_000)
    assert view.outflows == pytest.approx(35_000)
    assert view.savings == pytest.approx(65_000)
    assert view.actual_spend == pytest.approx(2_000)
    assert view.actual_vs_planned == pytest.approx(2_000 - 35_000)
    assert len(view.timeline) == 1
    assert view.timeline[0]["label"] == "Rent"


def test_seed_defaults_and_idempotent(conn):
    assert planner.seed_default_recurrings(conn) == 4
    assert planner.seed_default_recurrings(conn) == 0
    rows = {r["label"]: r["day_of_month"] for r in db.list_recurring_expenses(conn)}
    assert rows == {"Rent": 1, "Electricity": 5, "Wifi": 7, "Mobile": 10}


def test_recurring_paid_mark(conn):
    rid = db.add_recurring_expense(conn, label="Wifi", amount=800, day_of_month=7)
    assert rid is not None
    assert db.set_recurring_paid(conn, rid, "2026-09", paid=True) is True
    view = planner.build_planner(conn, "2026-09")
    row = next(r for r in view.recurrings if r["id"] == rid)
    assert row["paid"] is True
    assert db.set_recurring_paid(conn, rid, "2026-09", paid=False) is True
    view = planner.build_planner(conn, "2026-09")
    row = next(r for r in view.recurrings if r["id"] == rid)
    assert row["paid"] is False


def test_savings_goal_pace(conn):
    db.upsert_monthly_income(conn, month="2026-09", amount=50_000)
    db.add_recurring_expense(conn, label="Rent", amount=20_000, day_of_month=1)
    db.add_savings_goal(conn, label="Emergency", target_amount=90_000)
    view = planner.build_planner(conn, "2026-09")
    assert view.savings == pytest.approx(30_000)
    assert view.goals[0]["months_at_pace"] == 3


def test_copy_income_web(client, conn):
    db.upsert_monthly_income(
        conn, month="2026-08", amount=99_000, credited_on=date(2026, 7, 31)
    )
    resp = client.post(
        "/planner/income/copy",
        data={"month": "2026-09"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "tab=planner" in resp.headers["Location"]
    assert "month=2026-09" in resp.headers["Location"]
    row = db.get_monthly_income(conn, "2026-09")
    assert row is not None
    assert float(row["amount"]) == pytest.approx(99_000)


def test_zero_amount_recurring_excluded_from_total(conn):
    db.upsert_monthly_income(conn, month="2026-09", amount=10_000)
    db.add_recurring_expense(conn, label="Rent", amount=0, day_of_month=1)
    view = planner.build_planner(conn, "2026-09")
    assert view.recurring_total == 0
    assert view.savings == pytest.approx(10_000)


def test_copy_monthly_income_amount_note_and_credited_on(conn):
    db.upsert_monthly_income(
        conn,
        month="2026-08",
        amount=99_000.5,
        credited_on=date(2026, 7, 15),
        note="with bonus",
    )
    rid = db.add_recurring_expense(conn, label="Rent", amount=30_000, day_of_month=1)
    gid = db.add_savings_goal(conn, label="Emergency", target_amount=200_000)
    assert db.copy_monthly_income(conn, from_month="2026-08", to_month="2026-09")
    row = db.get_monthly_income(conn, "2026-09")
    assert float(row["amount"]) == pytest.approx(99_000.5)
    assert row["note"] == "with bonus"
    assert db._as_date(row["credited_on"]) == date(2026, 8, 31)
    # Prior month's credited_on is not reused; target gets prior_month_end.
    assert db._as_date(row["credited_on"]) != date(2026, 7, 15)
    assert db.list_recurring_expenses(conn)[0]["id"] == rid
    assert db.list_savings_goals(conn)[0]["id"] == gid


def test_copy_monthly_income_empty_prior(conn):
    db.add_recurring_expense(conn, label="Rent", amount=1_000, day_of_month=1)
    db.add_savings_goal(conn, label="EF", target_amount=10_000)
    assert db.copy_monthly_income(conn, from_month="2026-07", to_month="2026-08") is False
    assert db.get_monthly_income(conn, "2026-08") is None
    assert len(db.list_recurring_expenses(conn)) == 1
    assert len(db.list_savings_goals(conn)) == 1


def test_copy_income_web_empty_prior_flashes(client, conn):
    resp = client.post(
        "/planner/income/copy",
        data={"month": "2026-09"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"No salary saved for 2026-08" in resp.data
    assert db.get_monthly_income(conn, "2026-09") is None


def test_copy_does_not_import_planner():
    import inspect

    source = inspect.getsource(db.copy_monthly_income)
    assert "planner" not in source
    assert "import planner" not in source


def test_save_income_blank_credited_on_defaults(client, conn):
    resp = client.post(
        "/planner/income",
        data={"month": "2026-09", "amount": "1,20,000.25", "credited_on": "", "note": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/?tab=planner&month=2026-09")
    row = db.get_monthly_income(conn, "2026-09")
    assert float(row["amount"]) == pytest.approx(120_000.25)
    assert db._as_date(row["credited_on"]) == planner.prior_month_end("2026-09")


def test_save_income_bad_amount_flashes(client, conn):
    resp = client.post(
        "/planner/income",
        data={"month": "2026-09", "amount": "nope"},
        follow_redirects=True,
    )
    assert b"Enter a valid salary amount." in resp.data
    assert db.get_monthly_income(conn, "2026-09") is None


def test_inactive_and_zero_recurring_excluded_from_totals(conn):
    db.upsert_monthly_income(conn, month="2026-09", amount=50_000)
    db.add_recurring_expense(conn, label="Rent", amount=20_000, day_of_month=1, active=True)
    db.add_recurring_expense(conn, label="Old gym", amount=2_000, day_of_month=3, active=False)
    db.add_recurring_expense(conn, label="Placeholder", amount=0, day_of_month=5, active=True)
    view = planner.build_planner(conn, "2026-09")
    assert view.recurring_total == pytest.approx(20_000)
    assert view.savings == pytest.approx(30_000)
    # Timeline: active with a day (including zero-amount placeholders).
    assert [r["label"] for r in view.timeline] == ["Rent", "Placeholder"]
    assert all(r["active"] for r in view.timeline)


def test_edit_recurring_via_post(client, conn):
    rid = db.add_recurring_expense(conn, label="Rent", amount=30_000, day_of_month=1)
    resp = client.post(
        f"/planner/recurring/{rid}/edit",
        data={
            "month": "2026-09",
            "label": "House rent",
            "amount": "31,500.50",
            "day_of_month": "2",
            "active": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "tab=planner" in resp.headers["Location"]
    row = conn.execute(
        "SELECT label, amount, day_of_month, active FROM recurring_expenses WHERE id = ?",
        (rid,),
    ).fetchone()
    assert row["label"] == "House rent"
    assert float(row["amount"]) == pytest.approx(31_500.50)
    assert row["day_of_month"] == 2
    assert row["active"] == 1


def test_edit_recurring_unchecked_active_and_empty_day(client, conn):
    rid = db.add_recurring_expense(conn, label="Wifi", amount=800, day_of_month=7)
    resp = client.post(
        f"/planner/recurring/{rid}/edit",
        data={
            "month": "2026-09",
            "label": "Wifi",
            "amount": "800",
            "day_of_month": "",
            # active checkbox omitted → inactive
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT day_of_month, active FROM recurring_expenses WHERE id = ?", (rid,)
    ).fetchone()
    assert row["day_of_month"] is None
    assert row["active"] == 0
    view = planner.build_planner(conn, "2026-09")
    assert view.recurring_total == 0
    assert view.timeline == []


def test_edit_recurring_bad_amount_flashes(client, conn):
    rid = db.add_recurring_expense(conn, label="Wifi", amount=800, day_of_month=7)
    resp = client.post(
        f"/planner/recurring/{rid}/edit",
        data={
            "month": "2026-09",
            "label": "Wifi",
            "amount": "abc",
            "day_of_month": "7",
            "active": "1",
        },
        follow_redirects=True,
    )
    assert b"Enter a valid amount" in resp.data
    row = conn.execute(
        "SELECT amount FROM recurring_expenses WHERE id = ?", (rid,)
    ).fetchone()
    assert float(row["amount"]) == pytest.approx(800)


def test_update_recurring_ellipsis_keeps_day(conn):
    rid = db.add_recurring_expense(conn, label="Rent", amount=1_000, day_of_month=5)
    assert db.update_recurring_expense(conn, rid, amount=2_000) is True
    row = conn.execute(
        "SELECT amount, day_of_month, active FROM recurring_expenses WHERE id = ?",
        (rid,),
    ).fetchone()
    assert float(row["amount"]) == pytest.approx(2_000)
    assert row["day_of_month"] == 5
    assert row["active"] == 1


def test_delete_recurring_cascades_paid_marks(conn):
    rid = db.add_recurring_expense(conn, label="Wifi", amount=800, day_of_month=7)
    db.set_recurring_paid(conn, rid, "2026-09", paid=True)
    db.set_recurring_paid(conn, rid, "2026-10", paid=True)
    assert db.delete_recurring_expense(conn, rid) is True
    orphans = conn.execute(
        "SELECT * FROM recurring_paid WHERE recurring_id = ?", (rid,)
    ).fetchall()
    assert orphans == []


def test_paid_mark_month_isolation(conn):
    rid = db.add_recurring_expense(conn, label="Mobile", amount=500, day_of_month=10)
    assert db.set_recurring_paid(conn, rid, "2026-09", paid=True)
    assert rid in db.paid_recurring_ids(conn, "2026-09")
    assert rid not in db.paid_recurring_ids(conn, "2026-10")
    assert db.set_recurring_paid(conn, rid, "2026-09", paid=False)
    assert rid not in db.paid_recurring_ids(conn, "2026-09")
    # Clearing Sept must not invent an Oct mark.
    assert rid not in db.paid_recurring_ids(conn, "2026-10")


def test_mark_paid_via_web_month_only(client, conn):
    rid = db.add_recurring_expense(conn, label="Rent", amount=30_000, day_of_month=1)
    db.set_recurring_paid(conn, rid, "2026-08", paid=True)
    resp = client.post(
        f"/planner/recurring/{rid}/paid",
        data={"month": "2026-09", "paid": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert rid in db.paid_recurring_ids(conn, "2026-09")
    assert rid in db.paid_recurring_ids(conn, "2026-08")
    client.post(
        f"/planner/recurring/{rid}/paid",
        data={"month": "2026-09", "paid": "0"},
        follow_redirects=False,
    )
    assert rid not in db.paid_recurring_ids(conn, "2026-09")
    assert rid in db.paid_recurring_ids(conn, "2026-08")


def test_planned_expenses_add_delete_totals(client, conn):
    db.upsert_monthly_income(conn, month="2026-09", amount=100_000)
    resp = client.post(
        "/planner/planned",
        data={"month": "2026-09", "label": "Gift", "amount": "5,000"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "tab=planner" in resp.headers["Location"]
    view = planner.build_planner(conn, "2026-09")
    assert view.planned_total == pytest.approx(5_000)
    assert len(view.planned) == 1
    pid = view.planned[0]["id"]
    # Other month unaffected.
    db.add_planned_expense(conn, month="2026-10", label="Trip", amount=9_000)
    client.post(f"/planner/planned/{pid}/delete", data={"month": "2026-09"})
    assert db.list_planned_expenses(conn, "2026-09") == []
    assert len(db.list_planned_expenses(conn, "2026-10")) == 1
    view = planner.build_planner(conn, "2026-09")
    assert view.planned_total == 0


def test_goals_add_delete_and_months_at_pace_when_savings_nonpositive(client, conn):
    db.upsert_monthly_income(conn, month="2026-09", amount=10_000)
    db.add_recurring_expense(conn, label="Rent", amount=12_000, day_of_month=1)
    resp = client.post(
        "/planner/goals",
        data={"month": "2026-09", "label": "Emergency", "target_amount": "₹ 90,000"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    view = planner.build_planner(conn, "2026-09")
    assert view.savings < 0
    assert view.goals[0]["months_at_pace"] is None
    assert view.goals[0]["pct"] == 0.0
    gid = view.goals[0]["id"]
    client.post(f"/planner/goals/{gid}/delete", data={"month": "2026-09"})
    assert db.list_savings_goals(conn) == []


def test_actual_vs_planned_month_window(conn):
    db.upsert_monthly_income(conn, month="2026-09", amount=50_000)
    db.add_recurring_expense(conn, label="Rent", amount=10_000, day_of_month=1)
    for spent_on, amount in (
        (date(2026, 8, 31), 100),
        (date(2026, 9, 1), 200),
        (date(2026, 9, 30), 300),
        (date(2026, 10, 1), 400),
    ):
        db.add_expense(
            conn,
            Expense(
                spent_on=spent_on,
                amount=amount,
                description=str(spent_on),
                source=SOURCE_MANUAL,
            ),
        )
    view = planner.build_planner(conn, "2026-09")
    assert view.actual_spend == pytest.approx(500)
    assert view.outflows == pytest.approx(10_000)
    assert view.actual_vs_planned == pytest.approx(500 - 10_000)


def test_salary_zero_has_income_for_form(conn):
    assert planner.build_planner(conn, "2026-09").has_income is False
    db.upsert_monthly_income(
        conn, month="2026-09", amount=0, credited_on=date(2026, 8, 31), note="nil"
    )
    view = planner.build_planner(conn, "2026-09")
    assert view.has_income is True
    assert view.salary == 0.0


def test_planner_page_shows_zero_salary_and_formnovalidate(client, conn):
    db.upsert_monthly_income(
        conn, month="2026-09", amount=0, credited_on=date(2026, 8, 31)
    )
    page = client.get("/?tab=planner&month=2026-09").get_data(as_text=True)
    assert 'value="0.00"' in page
    assert "formnovalidate" in page
    assert 'formaction="/planner/income/copy"' in page


def test_parse_amount_commas_and_rupee():
    assert _parse_amount("1,20,000") == pytest.approx(120_000)
    assert _parse_amount("₹ 50,000.50") == pytest.approx(50_000.50)
    assert _parse_amount("") is None
    assert _parse_amount("abc") is None


def test_copy_preserves_float_precision(client, conn):
    db.upsert_monthly_income(
        conn, month="2026-08", amount=123_456.78, credited_on=date(2026, 7, 31)
    )
    client.post("/planner/income/copy", data={"month": "2026-09"})
    row = db.get_monthly_income(conn, "2026-09")
    assert float(row["amount"]) == pytest.approx(123_456.78)
    # Second copy must replace, not double.
    client.post("/planner/income/copy", data={"month": "2026-09"})
    row = db.get_monthly_income(conn, "2026-09")
    assert float(row["amount"]) == pytest.approx(123_456.78)


def test_seed_defaults_web(client, conn):
    resp = client.post(
        "/planner/recurring/defaults",
        data={"month": "2026-09"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "tab=planner" in resp.headers["Location"]
    assert len(db.list_recurring_expenses(conn)) == 4
    resp = client.post(
        "/planner/recurring/defaults",
        data={"month": "2026-09"},
        follow_redirects=True,
    )
    assert b"already present" in resp.data
    assert len(db.list_recurring_expenses(conn)) == 4
