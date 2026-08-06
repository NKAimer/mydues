from datetime import date, datetime

import pytest

from mydues import db, dues
from mydues.models import SOURCE_MANUAL, SOURCE_STATEMENT, Card, StatementRecord
from mydues.web.app import create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def seed(conn):
    card = Card(issuer="hdfc", label="HDFC Infinia", last4="8765", credit_limit=500000.0)
    card.id = db.add_card(conn, card)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=45231.50,
            min_due=2270.0,
            due_date=date.today(),
            statement_date=date.today(),
            source=SOURCE_STATEMENT,
            source_ref="msg-1",
            as_of=datetime.now(),
        ),
    )
    return card


def test_dashboard_renders_the_billed_amount_and_its_caveat(client, conn):
    seed(conn)
    page = client.get("/").get_data(as_text=True)

    assert "HDFC Infinia" in page
    assert "Statement total" in page
    assert "45,231.50" in page
    assert "<dt>Limit</dt>" not in page
    assert "billed statement amounts" in page
    assert "Due today" in page


def test_empty_dashboard_prompts_for_a_card(client, conn):
    page = client.get("/").get_data(as_text=True)
    assert "No cards yet" in page


def test_add_card_through_the_form(client, conn):
    response = client.post(
        "/cards",
        data={"issuer": "icici", "last4": "4409", "label": "ICICI Amazon Pay"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert [c.label for c in db.list_cards(conn)] == ["ICICI Amazon Pay"]


def test_add_card_rejects_a_bad_card_number(client, conn):
    page = client.post(
        "/cards", data={"issuer": "icici", "last4": "44"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "last 4 digits" in page
    assert db.list_cards(conn) == []


def test_manual_entry_overrides_the_parsed_figure(client, conn):
    card = seed(conn)
    client.post(
        f"/cards/{card.id}/set",
        data={
            "total_due": "46,310.00",
            "min_due": "2,320",
            "due_date": "2026-08-02",
            "statement_date": date.today().isoformat(),
        },
        follow_redirects=True,
    )

    view = dues.view_for_card(conn, db.get_card(conn, card.id))
    assert view.source == SOURCE_MANUAL
    assert view.billed_amount == pytest.approx(46310.00)
    assert view.due_date == date(2026, 8, 2)


def test_dates_are_written_and_read_the_indian_way(client, conn):
    """A form filled in as dd/mm/yyyy must not be read as mm/dd/yyyy."""
    card = seed(conn)

    client.post(
        f"/cards/{card.id}/set",
        data={
            "total_due": "1000",
            "due_date": "02/08/2026",
            "statement_date": date.today().strftime("%d/%m/%Y"),
        },
        follow_redirects=True,
    )

    view = dues.view_for_card(conn, db.get_card(conn, card.id))
    assert view.source == SOURCE_MANUAL
    assert view.due_date == date(2026, 8, 2)
    assert view.statement_date == date.today()


def test_the_forms_ask_for_dates_as_ddmmyyyy(client, conn):
    seed(conn)
    page = client.get("/").get_data(as_text=True)

    assert 'type="date"' not in page
    assert page.count('placeholder="dd/mm/yyyy"') >= 3


def test_a_date_of_birth_is_taken_as_ddmmyyyy(client, conn):
    client.post(
        "/cards",
        data={"issuer": "hdfc", "last4": "8765", "dob": "10/12/1815"},
        follow_redirects=True,
    )

    assert db.list_cards(conn)[0].dob == date(1815, 12, 10)


def test_recording_a_payment_settles_the_card(client, conn):
    card = seed(conn)
    client.post(f"/cards/{card.id}/paid", data={"amount": ""}, follow_redirects=True)

    view = dues.view_for_card(conn, db.get_card(conn, card.id))
    assert view.outstanding == pytest.approx(0.0)
    assert view.status == dues.STATUS_SETTLED


def test_json_endpoint_exposes_source_and_freshness(client, conn):
    seed(conn)
    payload = client.get("/api/dues").get_json()

    assert payload["total_outstanding"] == pytest.approx(45231.50)
    card = payload["cards"][0]
    assert card["source"] == SOURCE_STATEMENT
    assert card["billed_amount"] == pytest.approx(45231.50)
    assert card["stale"] is False
    assert card["as_of"]


def test_delete_card_removes_its_history(client, conn):
    card = seed(conn)
    client.post(f"/cards/{card.id}/delete", follow_redirects=True)

    assert db.list_cards(conn) == []
    assert db.statements_for_card(conn, card.id) == []


def test_coverage_panel_and_transactions_not_read(client, conn):
    from mydues.models import Transaction

    card = seed(conn)
    # Non-zero bill with no line items → missing txns finding
    page = client.get("/").get_data(as_text=True)
    assert "Coverage" in page
    assert "Transactions not read" in page or "missing" in page.lower()

    # Fresh card with txns counted as ok once statements exist
    stmt = db.statements_for_card(conn, card.id)[0]
    db.save_transactions(
        conn,
        card.id,
        stmt.id,
        [Transaction(description="SWIGGY", amount=100)],
    )
    # Re-save a tiny bill so missing check clears
    from mydues.models import StatementRecord

    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=100,
            min_due=10,
            due_date=date.today(),
            statement_date=date.today(),
            source=SOURCE_STATEMENT,
            source_ref="msg-ok",
            as_of=datetime.now(),
        ),
    )


def test_stale_card_flagged_in_coverage(client, conn):
    card = Card(issuer="hdfc", label="Old Card", last4="1111")
    card.id = db.add_card(conn, card)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=100,
            due_date=date.today(),
            statement_date=date(2025, 1, 1),
            source=SOURCE_STATEMENT,
            source_ref="old",
            as_of=datetime(2025, 1, 1),
        ),
    )
    page = client.get("/").get_data(as_text=True)
    assert "stale" in page.lower()
    assert "Coverage" in page


def test_due_urgency_strip(client, conn):
    today = date.today()

    def add(last4, due, total=1000):
        card = Card(issuer="hdfc", label=f"Card {last4}", last4=last4)
        card.id = db.add_card(conn, card)
        db.save_statement(
            conn,
            StatementRecord(
                card_id=card.id,
                total_due=total,
                due_date=due,
                statement_date=today,
                source=SOURCE_STATEMENT,
                source_ref=f"msg-{last4}",
                as_of=datetime.now(),
            ),
        )
        return card

    add("1001", today.replace(day=1) if today.day > 3 else date(today.year, today.month, 1) - __import__("datetime").timedelta(days=5))
    # Overdue
    add("1002", today - __import__("datetime").timedelta(days=2))
    # Due in 2 days
    add("1003", today + __import__("datetime").timedelta(days=2))
    # Due in 10 days
    add("1004", today + __import__("datetime").timedelta(days=10))

    page = client.get("/").get_data(as_text=True)
    assert "Overdue" in page
    assert "Due in 3 days" in page
    assert "Due this week" in page


def test_cycle_chip_on_non_latest_statement(client, conn):
    card = seed(conn)
    older = db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=100,
            due_date=date(2026, 6, 1),
            statement_date=date(2026, 5, 15),
            source=SOURCE_STATEMENT,
            source_ref="older",
            as_of=datetime(2026, 5, 15),
        ),
    )
    latest = db.statements_for_card(conn, card.id)
    assert len(latest) >= 2
    page = client.get(f"/?statement={older}").get_data(as_text=True)
    assert "Viewing" in page
    assert "Back to latest" in page


def test_category_color_dots_and_uncategorized(client, conn):
    from mydues.models import Expense

    db.add_expense(
        conn,
        Expense(
            spent_on=date.today(),
            amount=50,
            description="X",
            category="Food & dining",
            source=SOURCE_MANUAL,
        ),
    )
    page = client.get(f"/?tab=expenses&month={date.today().strftime('%Y-%m')}").get_data(
        as_text=True
    )
    assert "cat-dot" in page
    assert "cat-food" in page or "cat-uncategorized" in page


def test_fetch_timestamps_never_and_after_meta(client, conn):
    page = client.get("/").get_data(as_text=True)
    assert "Never" in page
    db.set_meta(conn, db.META_EXPENSES_FETCHED, "2026-08-05T10:30:00")
    page = client.get("/?tab=expenses").get_data(as_text=True)
    assert "05/08/2026 10:30" in page


def test_highlight_param_marks_expense_row(client, conn):
    from mydues.models import Expense

    eid = db.add_expense(
        conn,
        Expense(
            spent_on=date.today(),
            amount=10,
            description="Highlight me",
            source=SOURCE_MANUAL,
        ),
    )
    page = client.get(
        f"/?tab=expenses&month={date.today().strftime('%Y-%m')}&highlight=expense-{eid}"
    ).get_data(as_text=True)
    assert f'id="expense-{eid}"' in page
    assert "row-highlight" in page


def test_density_toggle_and_delete_confirm_hooks(client, conn):
    from mydues.models import Expense

    db.add_expense(
        conn,
        Expense(
            spent_on=date.today(),
            amount=10,
            description="Hook check",
            source=SOURCE_MANUAL,
        ),
    )
    page = client.get(
        f"/?tab=expenses&month={date.today().strftime('%Y-%m')}"
    ).get_data(as_text=True)
    assert "data-density-select" in page
    assert "data-confirm" in page or "confirm(" in page
    assert "filter-actions" in page
    assert "filter-fields" in page
    assert "filter-toolbar" in page
    assert "sticky-actions" not in page
    assert "sticky-month" not in page
    assert "sticky-tabs" not in page
    css = open("mydues/web/static/app.css").read()
    assert "max-width: 640px" in css
    assert "stackable-table" in css
    assert ".sticky-actions" not in css
    assert ".sticky-month .month-nav" not in css
    assert ".sticky-tabs" not in css
    assert "position: sticky" not in css


def test_empty_expenses_month_copy(client, conn):
    page = client.get("/?tab=expenses&month=2020-01").get_data(as_text=True)
    assert "No expenses for this month" in page


def test_transactions_panel_open_persistence(client, conn):
    from mydues.models import KIND_DEBIT, Transaction

    card = seed(conn)
    other = Card(issuer="amex", label="Amex Gold", last4="1234")
    other.id = db.add_card(conn, other)
    other_stmt_id = db.save_statement(
        conn,
        StatementRecord(
            card_id=other.id,
            total_due=1000,
            due_date=date.today(),
            statement_date=date.today(),
            source=SOURCE_STATEMENT,
            source_ref="other-latest",
            as_of=datetime.now(),
        ),
    )
    older_id = db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=900,
            due_date=date(2026, 5, 20),
            statement_date=date(2026, 5, 10),
            source=SOURCE_STATEMENT,
            source_ref="older-txns",
            as_of=datetime(2026, 5, 10),
        ),
    )
    latest = db.statements_for_card(conn, card.id)[0]
    db.save_transactions(
        conn,
        card.id,
        older_id,
        [
            Transaction(
                description="OLDER SHOP",
                amount=111,
                kind=KIND_DEBIT,
                txn_date=date(2026, 5, 1),
            )
        ],
    )
    db.save_transactions(
        conn,
        card.id,
        latest.id,
        [
            Transaction(
                description="LATEST SHOP",
                amount=222,
                kind=KIND_DEBIT,
                txn_date=date.today(),
            )
        ],
    )
    db.save_transactions(
        conn,
        other.id,
        other_stmt_id,
        [
            Transaction(
                description="OTHER CARD SHOP",
                amount=50,
                kind=KIND_DEBIT,
                txn_date=date.today(),
            )
        ],
    )

    def _txns_open_flags(html: str) -> list[bool]:
        """Whether each card's transactions <details> has the open attribute."""
        flags = []
        needle = 'class="txns"'
        start = 0
        while True:
            i = html.find(needle, start)
            if i < 0:
                break
            # Look at the tag rest: ` open>` or `>` / other attrs
            tag_end = html.find(">", i)
            tag = html[i:tag_end]
            flags.append(" open" in tag or tag.endswith("open"))
            start = tag_end + 1
        return flags

    older_with_flag = client.get(f"/?statement={older_id}&txns=1").get_data(as_text=True)
    assert any(_txns_open_flags(older_with_flag))
    assert "OLDER SHOP" in older_with_flag
    assert "LATEST SHOP" not in older_with_flag

    # Older statement for one card opens that card's panel only (no global force-open).
    older_no_flag = client.get(f"/?statement={older_id}").get_data(as_text=True)
    open_flags = _txns_open_flags(older_no_flag)
    assert open_flags.count(True) == 1
    assert open_flags.count(False) >= 1
    assert "OLDER SHOP" in older_no_flag
    assert "OTHER CARD SHOP" in older_no_flag

    latest_open = client.get("/?txns=1").get_data(as_text=True)
    assert all(_txns_open_flags(latest_open))
    assert "LATEST SHOP" in latest_open

    # Clean dashboard: every transactions panel must be closed (no open attr).
    latest_closed = client.get("/").get_data(as_text=True)
    assert 'class="txns"' in latest_closed
    assert 'class="txns" open' not in latest_closed
    assert not any(_txns_open_flags(latest_closed))

    # Cycle links must not inject txns=1 (global open would expand every card).
    assert "&amp;txns=1" not in older_no_flag
    assert 'name="txns"' not in older_no_flag

    filtered = client.get("/?q=SHOP").get_data(as_text=True)
    assert all(_txns_open_flags(filtered))
    assert 'name="txns"' not in filtered


def test_last_charges_on_card_tile(client, conn):
    from mydues.models import KIND_DEBIT, Transaction

    card = seed(conn)
    stmt = db.statements_for_card(conn, card.id)[0]
    db.save_transactions(
        conn,
        card.id,
        stmt.id,
        [
            Transaction(
                description="Annual membership fee",
                amount=2999,
                kind=KIND_DEBIT,
                txn_date=date(2025, 8, 5),
            ),
            Transaction(
                description="Interest charges",
                amount=450,
                kind=KIND_DEBIT,
                txn_date=date(2025, 12, 7),
            ),
            Transaction(
                description="SWIGGY",
                amount=200,
                kind=KIND_DEBIT,
                txn_date=date(2025, 12, 8),
            ),
        ],
    )

    view = dues.view_for_card(conn, db.get_card(conn, card.id))
    dues.attach_last_charges(conn, [view])
    assert view.last_annual_fee is not None
    assert view.last_annual_fee["amount"] == pytest.approx(2999)
    assert view.last_annual_fee["txn_date"] == date(2025, 8, 5)
    assert view.last_charge is not None
    assert view.last_charge["kind"] == "interest"
    assert view.last_charge["amount"] == pytest.approx(450)

    page = client.get("/").get_data(as_text=True)
    assert "Last annual fee" in page
    assert "2,999" in page
    assert "Also:" in page
    assert "Interest" in page
    assert "450" in page


def test_no_last_charge_lines_without_charge_kinds(client, conn):
    from mydues.models import KIND_DEBIT, Transaction

    card = seed(conn)
    stmt = db.statements_for_card(conn, card.id)[0]
    db.save_transactions(
        conn,
        card.id,
        stmt.id,
        [
            Transaction(
                description="SWIGGY BLR",
                amount=350,
                kind=KIND_DEBIT,
                txn_date=date(2026, 7, 1),
            )
        ],
    )
    page = client.get("/").get_data(as_text=True)
    assert "Last annual fee" not in page
    assert "Last charge" not in page
    assert "class=\"last-charge\"" not in page
