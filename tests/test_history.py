"""Looking back at earlier cycles without losing sight of the current one."""

from datetime import date, datetime

import pytest

from carddues import db, dues
from carddues.models import SOURCE_MANUAL, SOURCE_STATEMENT, Card, StatementRecord
from carddues.web.app import create_app


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def card(conn):
    entry = Card(issuer="icici", label="ICICI Amazon Pay", last4="4019")
    entry.id = db.add_card(conn, entry)
    return entry


@pytest.fixture
def cycles(conn, card):
    """Three months of statements, saved oldest first."""
    for month, total in ((5, 1000.0), (6, 2000.0), (7, 3000.0)):
        db.save_statement(
            conn,
            StatementRecord(
                card_id=card.id,
                total_due=total,
                statement_date=date(2026, month, 12),
                due_date=date(2026, month, 30),
                source=SOURCE_STATEMENT,
                source_ref=f"msg-{month}",
                as_of=datetime(2026, month, 12, 9, 0),
            ),
        )
    return db.statements_for_card(conn, card.id)


def test_the_stored_statements_come_back_newest_first(cycles):
    assert [record.statement_date.month for record in cycles] == [7, 6, 5]


def test_a_card_shows_its_latest_statement_by_default(conn, card, cycles):
    view = dues.view_for_card(conn, card, today=date(2026, 7, 20))

    assert view.billed_amount == pytest.approx(3000.0)
    assert view.is_latest
    assert len(view.history) == 3


def test_asking_for_a_cycle_shows_that_one(conn, card, cycles):
    may = cycles[-1]

    view = dues.view_for_card(conn, card, today=date(2026, 7, 20), record_id=may.id)

    assert view.billed_amount == pytest.approx(1000.0)
    assert view.due_date == date(2026, 5, 30)
    assert not view.is_latest


def test_an_unknown_statement_id_falls_back_to_the_latest(conn, card, cycles):
    view = dues.view_for_card(conn, card, today=date(2026, 7, 20), record_id=9999)

    assert view.billed_amount == pytest.approx(3000.0)
    assert view.is_latest


def test_a_manual_correction_still_wins_the_default(conn, card, cycles):
    """Manual entry beats a parsed figure for the same cycle."""
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=3100.0,
            statement_date=date(2026, 7, 12),
            source=SOURCE_MANUAL,
            as_of=datetime(2026, 7, 21, 10, 0),
        ),
    )

    view = dues.view_for_card(conn, card, today=date(2026, 7, 22))

    assert view.billed_amount == pytest.approx(3100.0)
    assert view.is_latest


def test_the_dashboard_lists_the_months_newest_first(client, conn, card, cycles):
    page = client.get("/").get_data(as_text=True)

    assert "Statements" in page
    where = [page.index(f"12/0{month}/2026") for month in (7, 6, 5)]
    assert where == sorted(where)


def test_the_dashboard_opens_the_month_that_was_asked_for(client, conn, card, cycles):
    may = cycles[-1]

    page = client.get(f"/?statement={may.id}").get_data(as_text=True)

    assert "₹1,000.00" in page
    assert "viewing an older cycle" in page
    assert "Back to the latest statement" in page


def test_choosing_a_month_leaves_the_other_cards_alone(client, conn, card, cycles):
    other = Card(issuer="hdfc", label="HDFC Infinia", last4="8765")
    other.id = db.add_card(conn, other)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=other.id,
            total_due=7777.0,
            statement_date=date(2026, 7, 5),
            source=SOURCE_STATEMENT,
            source_ref="msg-h",
            as_of=datetime(2026, 7, 5, 9, 0),
        ),
    )

    page = client.get(f"/?statement={cycles[-1].id}").get_data(as_text=True)

    assert "₹1,000.00" in page
    assert "₹7,777.00" in page


def test_a_single_statement_needs_no_month_picker(client, conn, card):
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=500.0,
            statement_date=date(2026, 7, 12),
            source=SOURCE_STATEMENT,
            source_ref="msg-only",
            as_of=datetime(2026, 7, 12, 9, 0),
        ),
    )

    page = client.get("/").get_data(as_text=True)

    assert "Back to the latest statement" not in page
    assert "class=chosen" not in page
