"""Shared spend categorisation: keywords, merchant memory, expense alerts."""

from datetime import date, datetime

import pytest

from carddues.categories import (
    CATEGORY_MEMORY,
    UNCATEGORIZED,
    apply_category_rules,
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
    SOURCE_MANUAL,
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


def test_memory_fuzzy_match_pharmacies_to_pharmacy(conn):
    remember_merchant_category(conn, "Apollo pharmacies", "Health")
    category, source = resolve_category("APOLLO PHARMACY #12 MUMBAI", conn=conn)
    assert category == "Health"
    assert source == CATEGORY_MEMORY


def test_memory_fuzzy_match_compact_bharat_connect(conn):
    remember_merchant_category(conn, "BharatConnectUtiliti", "Bills & utilities")
    assert lookup_merchant_category(conn, "UPI-Bharat Connect Uties") == "Bills & utilities"
    category, source = resolve_category("BharatConnectUtiliti", conn=conn)
    assert category == "Bills & utilities"
    assert source == CATEGORY_MEMORY


def test_memory_fuzzy_match_exact_still_works(conn):
    remember_merchant_category(conn, "SWIGGY BANGALORE", "Travel")
    assert lookup_merchant_category(conn, "SWIGGY BANGALORE") == "Travel"
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category == "Travel"
    assert source == CATEGORY_MEMORY


def test_memory_longest_key_wins(conn):
    remember_merchant_category(conn, "apollo", "Health")
    remember_merchant_category(conn, "apollo pharmacy", "Shopping")
    assert lookup_merchant_category(conn, "APOLLO PHARMACY BANGALORE") == "Shopping"


def test_apply_category_rules_uses_fuzzy_merchant_memory(conn):
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
                description="APOLLO PHARMACY BANGALORE",
                amount=99.0,
                kind=KIND_DEBIT,
                category="Health",
                category_source=CATEGORY_GUESS,
            ),
        ],
    )
    # Keyword guess would keep Health; force a different category via memory.
    remember_merchant_category(conn, "Apollo pharmacies", "Shopping")
    stats = apply_category_rules(conn)
    assert stats["transactions_updated"] == 1

    txn = db.transactions_for_statement(conn, statement_id)[0]
    assert txn.category == "Shopping"
    assert txn.category_source == CATEGORY_MEMORY


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

    page = client.get("/").get_data(as_text=True)
    assert 'id="known-categories"' in page
    assert 'list="known-categories"' in page
    assert "Food &amp; dining" in page or "Food & dining" in page

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
    before = len(db.list_category_phrases(conn))
    assert before > 0  # seeded from builtins on init

    phrase_id = db.upsert_category_phrase(conn, "sunrise traders", "Shopping")
    assert phrase_id is not None
    rows = db.list_category_phrases(conn)
    assert len(rows) == before + 1
    custom = next(row for row in rows if row["id"] == phrase_id)
    assert custom["phrase"] == "sunrise traders"
    assert custom["category"] == "Shopping"

    same_id = db.upsert_category_phrase(conn, "SUNRISE TRADERS", "Travel")
    assert same_id == phrase_id
    rows = db.list_category_phrases(conn)
    assert len(rows) == before + 1
    custom = next(row for row in rows if row["id"] == phrase_id)
    assert custom["category"] == "Travel"

    assert db.delete_category_phrase(conn, phrase_id)
    assert len(db.list_category_phrases(conn)) == before
    assert all(row["id"] != phrase_id for row in db.list_category_phrases(conn))


def test_init_seeds_category_phrases(conn):
    rows = db.list_category_phrases(conn)
    phrases = {row["phrase"] for row in rows}
    assert "swiggy" in phrases
    assert "zomato" in phrases
    food = next(row for row in rows if row["phrase"] == "swiggy")
    assert food["category"] == "Food & dining"
    # Re-init must not duplicate when the table already has rows.
    again = db.seed_category_phrases_from_builtins(conn)
    assert again == 0
    assert len(db.list_category_phrases(conn)) == len(rows)


def test_categorise_prefers_user_phrase_over_builtin():
    # Builtin would map "cafe" → Food & dining; provided phrases win alone.
    assert categorise("LOCAL CAFE ORDER") == "Food & dining"
    assert (
        categorise("LOCAL CAFE ORDER", phrases=[("cafe", "Shopping")]) == "Shopping"
    )


def test_categorise_phrases_only_when_provided():
    # Empty list means no match — do not fall back to builtins.
    assert categorise("SWIGGY BANGALORE", phrases=[]) is None
    assert categorise("SWIGGY BANGALORE", phrases=[("zomato", "Food & dining")]) is None


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


def test_resolve_uses_seeded_phrase(conn):
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category == "Food & dining"
    assert source == CATEGORY_GUESS


def test_deleting_seeded_phrase_stops_matching(conn):
    rows = db.list_category_phrases(conn)
    swiggy = next(row for row in rows if row["phrase"] == "swiggy")
    assert db.delete_category_phrase(conn, swiggy["id"])
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category is None
    assert source is None
    # Builtins still work when no phrases list is supplied.
    assert categorise("SWIGGY BANGALORE") == "Food & dining"


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
    assert b"swiggy" in response.data
    assert b"No custom phrase rules yet." not in response.data
    assert b"before built-in keywords" not in response.data
    before = len(db.list_category_phrases(conn))

    response = client.post(
        "/category-phrases",
        data={"phrase": "sunrise", "category": "Shopping"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "tab=categories" in response.headers["Location"]
    rows = db.list_category_phrases(conn)
    assert len(rows) == before + 1
    custom = next(row for row in rows if row["phrase"] == "sunrise")
    phrase_id = custom["id"]
    assert custom["category"] == "Shopping"

    response = client.post(
        f"/category-phrases/{phrase_id}/edit",
        data={"phrase": "sunrise traders", "category": "Travel"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    rows = db.list_category_phrases(conn)
    custom = next(row for row in rows if row["id"] == phrase_id)
    assert custom["phrase"] == "sunrise traders"
    assert custom["category"] == "Travel"

    response = client.post(
        f"/category-phrases/{phrase_id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert len(db.list_category_phrases(conn)) == before
    assert all(row["phrase"] != "sunrise traders" for row in db.list_category_phrases(conn))
    # Seeded builtins remain visible after deleting a custom rule.
    assert any(row["phrase"] == "swiggy" for row in db.list_category_phrases(conn))


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
        data={"merchant": key, "category": "Health"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.list_merchant_categories(conn)[0]["category"] == "Health"

    response = client.post(
        f"/merchant-categories/{key}/edit",
        data={"merchant": "apollo pharmacies", "category": "Health"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    rows = db.list_merchant_categories(conn)
    assert len(rows) == 1
    assert rows[0]["merchant_key"] == "apollo pharmacies"
    assert rows[0]["category"] == "Health"

    response = client.post(
        f"/merchant-categories/apollo pharmacies/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.list_merchant_categories(conn) == []


def test_apply_category_rules_updates_eligible_expenses(conn):
    blank_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=10.0,
            description="SUNRISE TRADERS",
            category=None,
            category_source=None,
            source=SOURCE_MANUAL,
        ),
    )
    guess_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 2),
            amount=20.0,
            description="SUNRISE MART",
            category="Misc",
            category_source=CATEGORY_GUESS,
            source=SOURCE_MANUAL,
        ),
    )
    user_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 3),
            amount=30.0,
            description="SUNRISE CAFE",
            category="Travel",
            category_source=CATEGORY_USER,
            source=SOURCE_MANUAL,
        ),
    )
    assert blank_id and guess_id and user_id

    db.upsert_category_phrase(conn, "sunrise", "Shopping")
    stats = apply_category_rules(conn)
    assert stats["expenses_updated"] == 2
    assert stats["transactions_updated"] == 0

    assert db.get_expense(conn, blank_id).category == "Shopping"
    assert db.get_expense(conn, blank_id).category_source == CATEGORY_GUESS
    assert db.get_expense(conn, guess_id).category == "Shopping"
    assert db.get_expense(conn, guess_id).category_source == CATEGORY_GUESS
    assert db.get_expense(conn, user_id).category == "Travel"
    assert db.get_expense(conn, user_id).category_source == CATEGORY_USER


def test_apply_category_rules_skips_statement_and_user_txns(conn):
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
                description="SUNRISE TRADERS",
                amount=99.0,
                kind=KIND_DEBIT,
                category=None,
                category_source=CATEGORY_GUESS,
            ),
            Transaction(
                description="ZOMATO ORDER",
                amount=200.0,
                kind=KIND_DEBIT,
                category="Food & Beverages",
                category_source=CATEGORY_STATEMENT,
            ),
            Transaction(
                description="SUNRISE CAFE",
                amount=50.0,
                kind=KIND_DEBIT,
                category="Travel",
                category_source=CATEGORY_USER,
            ),
        ],
    )
    db.upsert_category_phrase(conn, "sunrise", "Shopping")
    stats = apply_category_rules(conn)
    assert stats["transactions_updated"] == 1

    by_desc = {
        row.description: row for row in db.transactions_for_statement(conn, statement_id)
    }
    assert by_desc["SUNRISE TRADERS"].category == "Shopping"
    assert by_desc["SUNRISE TRADERS"].category_source == CATEGORY_GUESS
    # Phrase rules must not overwrite issuer-printed categories.
    assert by_desc["ZOMATO ORDER"].category == "Food & Beverages"
    assert by_desc["ZOMATO ORDER"].category_source == CATEGORY_STATEMENT
    assert by_desc["SUNRISE CAFE"].category == "Travel"
    assert by_desc["SUNRISE CAFE"].category_source == CATEGORY_USER


def test_apply_category_rules_memory_overrides_statement_category(conn):
    """Learned merchants can correct noisy issuer labels like Miscellaneous Stores."""
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
                description="UPI_APOLLO PHARMACY IND - Ref No: RT123",
                amount=433.0,
                kind=KIND_DEBIT,
                category="Miscellaneous Stores",
                category_source=CATEGORY_STATEMENT,
            ),
        ],
    )
    remember_merchant_category(conn, "apollo pharmacy", "Health")
    stats = apply_category_rules(conn)
    assert stats["transactions_updated"] == 1
    txn = db.transactions_for_statement(conn, statement_id)[0]
    assert txn.category == "Health"
    assert txn.category_source == CATEGORY_MEMORY


def test_delete_phrase_reapply_clears_previous_guess(conn):
    expense_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=10.0,
            description="OBSCURE SUNRISE SHOP",
            category=None,
            category_source=None,
            source=SOURCE_MANUAL,
        ),
    )
    phrase_id = db.upsert_category_phrase(conn, "sunrise", "Shopping")
    apply_category_rules(conn)
    assert db.get_expense(conn, expense_id).category == "Shopping"
    assert db.get_expense(conn, expense_id).category_source == CATEGORY_GUESS

    assert db.delete_category_phrase(conn, phrase_id)
    stats = apply_category_rules(conn)
    assert stats["expenses_updated"] == 1
    cleared = db.get_expense(conn, expense_id)
    assert cleared.category is None
    assert cleared.category_source is None


def test_phrase_crud_reapplies_and_reapply_route(client, conn):
    expense_id = db.add_expense(
        conn,
        Expense(
            spent_on=date(2026, 8, 1),
            amount=10.0,
            description="SUNRISE TRADERS",
            category=None,
            category_source=None,
            source=SOURCE_MANUAL,
        ),
    )
    response = client.post(
        "/category-phrases",
        data={"phrase": "sunrise", "category": "Shopping"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Re-applied rules" in body
    assert db.get_expense(conn, expense_id).category == "Shopping"

    page = client.get("/?tab=categories").get_data(as_text=True)
    assert "Re-apply category rules" in page
    assert "Manual edits are left alone" in page or "manual" in page.lower()

    db.update_expense(
        conn,
        expense_id,
        spent_on=date(2026, 8, 1),
        amount=10.0,
        description="SUNRISE TRADERS",
        category="Misc",
        category_source=CATEGORY_GUESS,
    )
    db.upsert_category_phrase(conn, "sunrise", "Travel")
    response = client.post("/categories/reapply", follow_redirects=True)
    assert response.status_code == 200
    assert "Re-applied rules" in response.get_data(as_text=True)
    assert db.get_expense(conn, expense_id).category == "Travel"
    assert db.get_expense(conn, expense_id).category_source == CATEGORY_GUESS


def test_manual_expense_sets_user_category_source(client, conn):
    client.post(
        "/expenses",
        data={
            "spent_on": "01/08/2026",
            "amount": "50",
            "description": "Coffee",
            "category": "Food & dining",
        },
    )
    rows = db.list_expenses(conn, start=date(2026, 8, 1), end=date(2026, 9, 1))
    assert len(rows) == 1
    assert rows[0].category == "Food & dining"
    assert rows[0].category_source == CATEGORY_USER

    expense_id = rows[0].id
    client.post(
        f"/expenses/{expense_id}/edit",
        data={
            "spent_on": "01/08/2026",
            "amount": "50",
            "description": "Coffee",
            "category": "",
        },
    )
    cleared = db.get_expense(conn, expense_id)
    assert cleared.category is None
    assert cleared.category_source is None


def test_parse_alert_sets_category_source():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert: INR 420.00 spent",
        body="Rs. 420.00 spent at SWIGGY BANGALORE on 01-08-2026 using your card.",
        received_at=datetime(2026, 8, 1, 12, 0),
    )
    assert expense is not None
    assert expense.category == "Food & dining"
    assert expense.category_source == CATEGORY_GUESS
