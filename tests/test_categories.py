"""Shared spend categorisation: keywords, merchant memory, expense alerts."""

from datetime import date, datetime

import pytest

from carddues.categories import (
    CATEGORY_MEMORY,
    UNCATEGORIZED,
    categorise,
    category_spend_totals,
    lookup_merchant_category,
    remember_merchant_category,
    resolve_category,
)
from carddues import db, expense_ingest
from carddues.models import (
    CATEGORY_GUESS,
    CATEGORY_STATEMENT,
    CATEGORY_USER,
    KIND_CREDIT,
    KIND_DEBIT,
    Card,
    Expense,
    SOURCE_STATEMENT,
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


def test_keyword_guesses_common_merchants():
    assert categorise("SWIGGY BANGALORE") == "Food & dining"
    assert categorise("IOCL FUEL STATION") == "Fuel"
    assert categorise("PAYMENT - THANK YOU") == "Payment received"
    assert categorise("MYNTRA FASHION") == "Shopping"


def test_resolve_prefers_printed_over_keywords():
    category, source = resolve_category("SWIGGY", printed="Food & Beverages")
    assert category == "Food & Beverages"
    assert source == CATEGORY_STATEMENT


def test_resolve_falls_back_to_keyword_guess():
    category, source = resolve_category("ZOMATO ORDER")
    assert category == "Food & dining"
    assert source == CATEGORY_GUESS


def test_unrecognised_merchant_stays_uncategorized():
    category, source = resolve_category("SUNRISE TRADERS PVT LTD")
    assert category is None
    assert source is None


def test_memory_overrides_keywords(conn):
    remember_merchant_category(conn, "SWIGGY BANGALORE", "Travel")
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category == "Travel"
    assert source == CATEGORY_MEMORY


def test_memory_for_unknown_merchant(conn):
    assert categorise("ZZZNOVELMERCHANT99") is None
    remember_merchant_category(conn, "ZZZNOVELMERCHANT99", "Shopping")
    category, source = resolve_category("ZZZNOVELMERCHANT99", conn=conn)
    assert category == "Shopping"
    assert source == CATEGORY_MEMORY


def test_printed_beats_memory(conn):
    remember_merchant_category(conn, "SWIGGY", "Travel")
    category, source = resolve_category(
        "SWIGGY", printed="Food & Beverages", conn=conn
    )
    assert category == "Food & Beverages"
    assert source == CATEGORY_STATEMENT


def test_parse_alert_assigns_keyword_category():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert: INR 420.00 spent",
        body="Rs. 420.00 spent at SWIGGY BANGALORE on 01-08-2026 using your card.",
        received_at=datetime(2026, 8, 1, 12, 0),
    )
    assert expense is not None
    assert "SWIGGY" in expense.description.upper()
    assert expense.category == "Food & dining"


def test_category_spend_totals_sums_debits_by_category():
    rows = [
        Transaction(description="A", amount=100.0, kind=KIND_DEBIT, category="Food"),
        Transaction(description="B", amount=50.0, kind=KIND_DEBIT, category="Food"),
        Transaction(description="C", amount=200.0, kind=KIND_DEBIT, category="Travel"),
        Transaction(description="D", amount=80.0, kind=KIND_CREDIT, category="Food"),
        Transaction(description="E", amount=25.0, kind=KIND_DEBIT, category=None),
        Transaction(description="F", amount=10.0, kind=KIND_DEBIT, category=""),
    ]
    assert category_spend_totals(rows) == [
        ("Travel", 200.0),
        ("Food", 150.0),
        (UNCATEGORIZED, 35.0),
    ]


def test_category_spend_totals_works_for_expenses():
    rows = [
        Expense(spent_on=date(2026, 8, 1), amount=300.0, description="X", category="Bills"),
        Expense(spent_on=date(2026, 8, 2), amount=100.0, description="Y", category=None),
        Expense(spent_on=date(2026, 8, 3), amount=50.0, description="Z", category="Bills"),
    ]
    assert category_spend_totals(rows) == [
        ("Bills", 350.0),
        (UNCATEGORIZED, 100.0),
    ]


def test_edit_transaction_category_sets_user_source_and_memory(client, conn):
    card = Card(issuer="hdfc", label="HDFC Infinia", last4="8765")
    card.id = db.add_card(conn, card)
    statement = StatementRecord(
        card_id=card.id,
        total_due=1000.0,
        as_of=datetime.now(),
        source=SOURCE_STATEMENT,
        statement_date=date(2026, 7, 15),
    )
    statement_id = db.save_statement(conn, statement)
    db.save_transactions(
        conn,
        card.id,
        statement_id,
        [
            Transaction(
                description="SWIGGY BANGALORE",
                amount=420.0,
                txn_date=date(2026, 7, 2),
                kind=KIND_DEBIT,
                category="Food & dining",
                category_source=CATEGORY_GUESS,
            )
        ],
    )
    txn = db.transactions_for_statement(conn, statement_id)[0]
    assert txn.id is not None

    response = client.post(
        f"/transactions/{txn.id}/category",
        data={"category": "Travel"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert f"#card-{card.id}" in response.headers["Location"]

    updated = db.transactions_for_statement(conn, statement_id)[0]
    assert updated.category == "Travel"
    assert updated.category_source == CATEGORY_USER
    assert lookup_merchant_category(conn, "SWIGGY BANGALORE") == "Travel"


def test_clear_transaction_category(client, conn):
    card = Card(issuer="hdfc", label="HDFC Infinia", last4="8765")
    card.id = db.add_card(conn, card)
    statement_id = db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=500.0,
            as_of=datetime.now(),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 7, 15),
        ),
    )
    db.save_transactions(
        conn,
        card.id,
        statement_id,
        [
            Transaction(
                description="OBSCURE SHOP",
                amount=99.0,
                kind=KIND_DEBIT,
                category="Misc",
                category_source=CATEGORY_USER,
            )
        ],
    )
    txn = db.transactions_for_statement(conn, statement_id)[0]

    client.post(f"/transactions/{txn.id}/category", data={"category": ""})
    cleared = db.transactions_for_statement(conn, statement_id)[0]
    assert cleared.category is None
    assert cleared.category_source is None


def test_edit_transaction_category_preserves_statement_query(client, conn):
    card = Card(issuer="hdfc", label="HDFC Infinia", last4="8765")
    card.id = db.add_card(conn, card)
    older = db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=200.0,
            as_of=datetime(2026, 6, 1),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 5, 15),
            source_ref="old",
        ),
    )
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=300.0,
            as_of=datetime(2026, 7, 1),
            source=SOURCE_STATEMENT,
            statement_date=date(2026, 6, 15),
            source_ref="new",
        ),
    )
    db.save_transactions(
        conn,
        card.id,
        older,
        [Transaction(description="CAFE", amount=50.0, kind=KIND_DEBIT)],
    )
    txn = db.transactions_for_statement(conn, older)[0]

    response = client.post(
        f"/transactions/{txn.id}/category",
        data={"category": "Food", "statement": str(older)},
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert f"statement={older}" in location
    assert f"#card-{card.id}" in location


def test_upsert_and_delete_category_phrase(conn):
    phrase_id = db.upsert_category_phrase(conn, "sunrise traders", "Shopping")
    assert phrase_id is not None
    rows = db.list_category_phrases(conn)
    assert len(rows) == 1
    assert rows[0]["phrase"] == "sunrise traders"
    assert rows[0]["category"] == "Shopping"

    same_id = db.upsert_category_phrase(conn, "SUNRISE TRADERS", "Travel")
    assert same_id == phrase_id
    rows = db.list_category_phrases(conn)
    assert len(rows) == 1
    assert rows[0]["category"] == "Travel"

    assert db.delete_category_phrase(conn, phrase_id)
    assert db.list_category_phrases(conn) == []


def test_categorise_prefers_user_phrase_over_builtin():
    # Builtin would map "cafe" → Food & dining; user rule wins.
    assert categorise("LOCAL CAFE ORDER") == "Food & dining"
    assert (
        categorise("LOCAL CAFE ORDER", phrases=[("cafe", "Shopping")]) == "Shopping"
    )


def test_categorise_longest_phrase_wins():
    assert (
        categorise(
            "CAFE COFFEE DAY",
            phrases=[("cafe", "Shopping"), ("cafe coffee", "Travel")],
        )
        == "Travel"
    )


def test_resolve_prefers_user_phrase_over_builtin(conn):
    db.upsert_category_phrase(conn, "swiggy", "Travel")
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category == "Travel"
    assert source == CATEGORY_GUESS


def test_memory_beats_user_phrase(conn):
    db.upsert_category_phrase(conn, "swiggy", "Travel")
    remember_merchant_category(conn, "SWIGGY BANGALORE", "Health")
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category == "Health"
    assert source == CATEGORY_MEMORY


def test_printed_beats_user_phrase(conn):
    db.upsert_category_phrase(conn, "swiggy", "Travel")
    category, source = resolve_category(
        "SWIGGY", printed="Food & Beverages", conn=conn
    )
    assert category == "Food & Beverages"
    assert source == CATEGORY_STATEMENT


def test_store_reresolves_with_conn_phrase(conn):
    from carddues.ingest import store
    from carddues.models import ParsedStatement

    card = Card(issuer="hdfc", label="HDFC Infinia", last4="8765")
    card.id = db.add_card(conn, card)
    db.upsert_category_phrase(conn, "sunrise", "Shopping")

    statement = ParsedStatement(
        total_due=500.0,
        last4="8765",
        issuer="hdfc",
        transactions=[
            Transaction(
                description="SUNRISE TRADERS PVT LTD",
                amount=99.0,
                kind=KIND_DEBIT,
                category=None,
                category_source=CATEGORY_GUESS,
            ),
            Transaction(
                description="ZOMATO",
                amount=200.0,
                kind=KIND_DEBIT,
                category="Food & Beverages",
                category_source=CATEGORY_STATEMENT,
            ),
        ],
    )
    status, _detail, card_id = store(conn, statement, source_ref="phrase-test")
    assert status == "parsed"
    assert card_id == card.id

    rows = db.transactions_for_statement(
        conn, db.statements_for_card(conn, card.id)[0].id
    )
    by_desc = {row.description: row for row in rows}
    assert by_desc["SUNRISE TRADERS PVT LTD"].category == "Shopping"
    assert by_desc["SUNRISE TRADERS PVT LTD"].category_source == CATEGORY_GUESS
    # Issuer-printed category is left alone.
    assert by_desc["ZOMATO"].category == "Food & Beverages"
    assert by_desc["ZOMATO"].category_source == CATEGORY_STATEMENT


def test_categories_tab_phrase_crud(client, conn):
    response = client.get("/?tab=categories")
    assert response.status_code == 200
    assert b"Phrase rules" in response.data
    assert b"Learned merchants" in response.data

    response = client.post(
        "/category-phrases",
        data={"phrase": "sunrise", "category": "Shopping"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "tab=categories" in response.headers["Location"]
    rows = db.list_category_phrases(conn)
    assert len(rows) == 1
    phrase_id = rows[0]["id"]

    response = client.post(
        f"/category-phrases/{phrase_id}/edit",
        data={"phrase": "sunrise traders", "category": "Travel"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    rows = db.list_category_phrases(conn)
    assert rows[0]["phrase"] == "sunrise traders"
    assert rows[0]["category"] == "Travel"

    response = client.post(
        f"/category-phrases/{phrase_id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.list_category_phrases(conn) == []


def test_categories_tab_merchant_crud(client, conn):
    response = client.post(
        "/merchant-categories",
        data={"merchant": "SWIGGY BANGALORE", "category": "Travel"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "tab=categories" in response.headers["Location"]
    rows = db.list_merchant_categories(conn)
    assert len(rows) == 1
    key = rows[0]["merchant_key"]
    assert "swiggy" in key
    assert rows[0]["category"] == "Travel"

    response = client.post(
        f"/merchant-categories/{key}/edit",
        data={"category": "Health"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.list_merchant_categories(conn)[0]["category"] == "Health"

    response = client.post(
        f"/merchant-categories/{key}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.list_merchant_categories(conn) == []

