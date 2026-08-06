"""Reading a statement's line items, and showing them."""

import pytest

from mydues import db, dues, ingest
from mydues.models import CATEGORY_GUESS, CATEGORY_STATEMENT, Card
from mydues.parsers import transactions as txns
from mydues.web.app import create_app

from .fixtures import (
    HDFC_TABLE,
    STATEMENT_WITH_TRANSACTIONS,
    TRANSACTION_TABLE,
    write_pdf,
)


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def card(conn):
    entry = Card(issuer="hdfc", label="HDFC Infinia", last4="8765")
    entry.id = db.add_card(conn, entry)
    return entry


def test_a_line_per_transaction_is_read_in_order():
    rows = txns.from_lines("\n".join(STATEMENT_WITH_TRANSACTIONS))

    assert [txn.description for txn in rows] == [
        "SWIGGY BANGALORE",
        "UBER INDIA SYSTEMS",
        "CROMA RETAIL WHITEFIELD",
        "PAYMENT RECEIVED THANK YOU",
    ]
    assert rows[0].txn_date.isoformat() == "2026-07-02"
    assert rows[2].amount == pytest.approx(8499.0)


def test_reward_points_are_not_mistaken_for_the_amount():
    """The trailing 12 on the Swiggy line is points earned, not money."""
    swiggy = txns.from_lines("02/07/2026 SWIGGY BANGALORE 640.00 12")[0]

    assert swiggy.amount == pytest.approx(640.0)


def test_a_posting_date_is_not_read_as_part_of_the_description():
    uber = txns.from_lines("03/07/2026 04/07/2026 UBER INDIA SYSTEMS 249.50")[0]

    assert uber.description == "UBER INDIA SYSTEMS"
    assert uber.txn_date.isoformat() == "2026-07-03"


def test_a_credit_is_marked_and_counted_the_other_way():
    payment = txns.from_lines("05/07/2026 PAYMENT RECEIVED THANK YOU 5,000.00 Cr")[0]

    assert payment.is_credit
    assert payment.amount == pytest.approx(5000.0)
    assert payment.signed_amount == pytest.approx(-5000.0)


def test_summary_rows_are_not_transactions():
    """A row of dates and figures under a label header must not become one."""
    rows = txns.from_lines(HDFC_TABLE)

    assert [txn.description for txn in rows] == ["SWIGGY BANGALORE"]


def test_a_total_row_in_the_grid_is_left_out():
    rows = txns.from_tables(TRANSACTION_TABLE)

    assert "Total" not in [txn.description for txn in rows]
    assert len(rows) == 3


def test_a_printed_category_is_kept_as_the_statements_own():
    zomato = txns.from_tables(TRANSACTION_TABLE)[0]

    assert zomato.category == "Food & Beverages"
    assert zomato.category_source == CATEGORY_STATEMENT


def test_a_missing_category_is_guessed_from_the_merchant():
    myntra = txns.from_tables(TRANSACTION_TABLE)[1]

    assert myntra.category == "Shopping"
    assert myntra.category_source == CATEGORY_GUESS


def test_an_unrecognised_merchant_gets_no_category():
    obscure = txns.from_lines("02/07/2026 SUNRISE TRADERS PVT LTD 120.00")[0]

    assert obscure.category is None
    assert obscure.category_source is None


def test_a_payment_is_categorised_ahead_of_any_merchant_match():
    assert txns.categorise("PAYMENT RECEIVED THANK YOU") == "Payment received"
    assert txns.categorise("AMAZON PAY INDIA") == "Shopping"
    assert txns.categorise("IOCL FUEL STATION") == "Fuel"


def test_a_richer_grid_wins_over_shorter_text():
    short_text = "12/06/2026 ONLY ONE LINE ITEM 480.00\n"
    rows = txns.extract_transactions(short_text, TRANSACTION_TABLE)

    assert [txn.description for txn in rows] == [
        "ZOMATO ONLINE ORDER",
        "MYNTRA DESIGNS",
        "REFUND MYNTRA DESIGNS",
    ]


def test_lines_win_over_a_sparse_table():
    """A partial pdfplumber table must not wipe out a full line extract."""
    lines = "\n".join(
        [
            "01/07/2026 MERCHANT ONE 100.00",
            "02/07/2026 MERCHANT TWO 200.00",
            "03/07/2026 MERCHANT THREE 300.00",
        ]
    )
    sparse_table = [
        [
            ["Date", "Details", "Amount"],
            ["01/07/2026", "ONLY ONE ROW", "100.00"],
            ["02/07/2026", "SECOND", "200.00"],
        ]
    ]
    rows = txns.extract_transactions(lines, sparse_table)
    assert len(rows) == 3


def test_ingesting_a_statement_stores_its_line_items(conn, card, tmp_path):
    path = tmp_path / "july.pdf"
    write_pdf(path, lines=STATEMENT_WITH_TRANSACTIONS)

    result = ingest.ingest_pdf(conn, path, source_ref="msg-1")

    assert result.status == ingest.STATUS_PARSED
    assert "4 transaction(s)" in result.detail
    statement = db.statements_for_card(conn, card.id)[0]
    stored = db.transactions_for_statement(conn, statement.id)
    assert [txn.description for txn in stored][0] == "SWIGGY BANGALORE"
    assert len(stored) == 4


def test_reading_the_same_statement_again_does_not_duplicate_them(conn, card, tmp_path):
    path = tmp_path / "july.pdf"
    write_pdf(path, lines=STATEMENT_WITH_TRANSACTIONS)

    ingest.ingest_pdf(conn, path, source_ref="msg-1")
    ingest.ingest_pdf(conn, path, source_ref="msg-1")

    statement = db.statements_for_card(conn, card.id)[0]
    assert len(db.transactions_for_statement(conn, statement.id)) == 4


def test_the_view_nets_refunds_off_the_line_items(conn, card, tmp_path):
    path = tmp_path / "july.pdf"
    write_pdf(path, lines=STATEMENT_WITH_TRANSACTIONS)
    ingest.ingest_pdf(conn, path, source_ref="msg-1")

    view = dues.view_for_card(conn, db.get_card(conn, card.id))

    assert len(view.transactions) == 4
    # 640 + 249.50 + 8,499 less the 5,000 paid.
    assert view.transactions_total == pytest.approx(4388.5)


def test_the_dashboard_shows_a_table_that_opens_and_closes(client, conn, card, tmp_path):
    path = tmp_path / "july.pdf"
    write_pdf(path, lines=STATEMENT_WITH_TRANSACTIONS)
    ingest.ingest_pdf(conn, path, source_ref="msg-1")

    page = client.get("/").get_data(as_text=True)

    assert "Show transactions" in page
    assert "Hide transactions" in page
    assert "SWIGGY BANGALORE" in page
    assert "02/07/2026" in page
    assert "-₹5,000.00" in page  # the payment reads as a credit
    assert "guess" in page  # the category was worked out, not printed


def test_a_statement_with_no_readable_line_items_shows_no_table(client, conn, card, tmp_path):
    path = tmp_path / "bare.pdf"
    write_pdf(path)  # summary figures only

    ingest.ingest_pdf(conn, path, source_ref="msg-2")
    page = client.get("/").get_data(as_text=True)

    assert "Show transactions" not in page


def test_transactions_for_statement_filters(conn, card):
    from datetime import date, datetime

    from mydues.models import KIND_DEBIT, SOURCE_STATEMENT, StatementRecord, Transaction

    sid = db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=1000,
            statement_date=date(2026, 8, 1),
            source=SOURCE_STATEMENT,
            as_of=datetime.now(),
        ),
    )
    db.save_transactions(
        conn,
        card.id,
        sid,
        [
            Transaction(description="SWIGGY", amount=200, kind=KIND_DEBIT, category="Food & dining"),
            Transaction(description="Annual fee", amount=500, kind=KIND_DEBIT),
            Transaction(description="UBER", amount=100, kind=KIND_DEBIT, category="Travel"),
        ],
    )
    assert len(db.transactions_for_statement(conn, sid, q="swiggy")) == 1
    assert len(db.transactions_for_statement(conn, sid, category="Travel")) == 1
    assert len(db.transactions_for_statement(conn, sid, min_amount=200, max_amount=200)) == 1
    charges = db.transactions_for_statement(conn, sid, charges_only=True)
    assert len(charges) == 1
    assert charges[0].charge_kind == "annual_fee"


def test_export_statement_transactions(client, conn, card):
    from datetime import date, datetime

    from mydues.models import KIND_DEBIT, SOURCE_STATEMENT, StatementRecord, Transaction

    sid = db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=1000,
            statement_date=date(2026, 8, 1),
            source=SOURCE_STATEMENT,
            as_of=datetime.now(),
        ),
    )
    db.save_transactions(
        conn,
        card.id,
        sid,
        [Transaction(description="SWIGGY", amount=200, kind=KIND_DEBIT, txn_date=date(2026, 8, 2))],
    )
    missing = client.get("/export/statement/99999/transactions.csv")
    assert missing.status_code == 404

    csv_resp = client.get(f"/export/statement/{sid}/transactions.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"
    assert "02/08/2026" in csv_resp.get_data(as_text=True)

    json_resp = client.get(f"/export/statement/{sid}/transactions.json")
    assert json_resp.status_code == 200
    payload = json_resp.get_json()
    assert payload["statement_id"] == sid
    assert len(payload["transactions"]) == 1
    assert payload["transactions"][0]["txn_date"] == "2026-08-02"
