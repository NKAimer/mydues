from datetime import date, datetime, timedelta

import pytest

from carddues import db, dues
from carddues.models import SOURCE_MANUAL, SOURCE_STATEMENT, Card, StatementRecord

TODAY = date(2026, 7, 31)


def make_card(conn, label="HDFC Infinia", last4="8765", limit=500000.0):
    card = Card(issuer="hdfc", label=label, last4=last4, credit_limit=limit)
    card.id = db.add_card(conn, card)
    return card


def add_record(conn, card, **kwargs):
    record = StatementRecord(
        card_id=card.id,
        total_due=kwargs.pop("total_due", 45000.0),
        as_of=kwargs.pop("as_of", datetime(2026, 7, 6, 9, 0)),
        source=kwargs.pop("source", SOURCE_STATEMENT),
        statement_date=kwargs.pop("statement_date", date(2026, 7, 5)),
        due_date=kwargs.pop("due_date", date(2026, 8, 2)),
        min_due=kwargs.pop("min_due", 2250.0),
        **kwargs,
    )
    record.id = db.save_statement(conn, record)
    return record


def test_view_reports_billed_amount_and_freshness(conn):
    card = make_card(conn)
    add_record(conn, card)
    view = dues.view_for_card(conn, card, today=TODAY)

    assert view.billed_amount == pytest.approx(45000.0)
    assert view.outstanding == pytest.approx(45000.0)
    assert view.days_to_due == 2
    assert view.status == dues.STATUS_DUE_SOON
    assert view.age_days == 26
    assert not view.is_stale
    assert "excludes later spending" in view.freshness_note


def test_manual_entry_wins_over_parsed_statement_for_same_cycle(conn):
    card = make_card(conn)
    add_record(conn, card, total_due=45000.0)
    add_record(conn, card, total_due=46310.0, source=SOURCE_MANUAL, as_of=datetime(2026, 7, 7))

    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.source == SOURCE_MANUAL
    assert view.billed_amount == pytest.approx(46310.0)


def test_newer_statement_wins_over_older_manual_entry(conn):
    card = make_card(conn)
    add_record(
        conn,
        card,
        total_due=46310.0,
        source=SOURCE_MANUAL,
        statement_date=date(2026, 6, 5),
        as_of=datetime(2026, 6, 7),
    )
    add_record(conn, card, total_due=45000.0, statement_date=date(2026, 7, 5))

    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.source == SOURCE_STATEMENT
    assert view.billed_amount == pytest.approx(45000.0)


def test_payments_reduce_outstanding_and_settle_the_card(conn):
    card = make_card(conn)
    add_record(conn, card, total_due=45000.0)

    db.add_payment(conn, card.id, 20000.0, date(2026, 7, 10))
    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.outstanding == pytest.approx(25000.0)
    assert view.min_due_remaining == pytest.approx(0.0)
    assert view.status == dues.STATUS_DUE_SOON

    db.add_payment(conn, card.id, 25000.0, date(2026, 7, 20))
    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.outstanding == pytest.approx(0.0)
    assert view.status == dues.STATUS_SETTLED


def test_payment_before_the_statement_does_not_count(conn):
    card = make_card(conn)
    add_record(conn, card, total_due=45000.0, statement_date=date(2026, 7, 5))
    db.add_payment(conn, card.id, 10000.0, date(2026, 6, 28))

    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.outstanding == pytest.approx(45000.0)


def test_overdue_status(conn):
    card = make_card(conn)
    add_record(conn, card, due_date=date(2026, 7, 25))
    assert dues.view_for_card(conn, card, today=TODAY).status == dues.STATUS_OVERDUE


def test_stale_statement_is_flagged(conn):
    card = make_card(conn)
    old = TODAY - timedelta(days=70)
    add_record(conn, card, statement_date=old, due_date=old + timedelta(days=20))

    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.is_stale
    assert "probably" in view.freshness_note


def test_statement_without_a_date_says_so(conn):
    card = make_card(conn)
    add_record(conn, card, statement_date=None)
    view = dues.view_for_card(conn, card, today=TODAY)
    assert "age is unknown" in view.freshness_note


def test_card_without_data_reports_no_data(conn):
    card = make_card(conn)
    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.status == dues.STATUS_NO_DATA
    assert view.outstanding is None
    assert "No statement parsed yet" in view.freshness_note


def test_utilisation_and_available_credit_are_derived(conn):
    card = make_card(conn, limit=500000.0)
    add_record(conn, card, total_due=45000.0)
    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.utilisation == pytest.approx(9.0)
    assert view.available_credit == pytest.approx(455000.0)


def test_portfolio_orders_by_urgency(conn):
    urgent = make_card(conn, label="Axis", last4="7788")
    later = make_card(conn, label="ICICI", last4="4409")
    add_record(conn, urgent, due_date=date(2026, 7, 20), total_due=1000.0)
    add_record(conn, later, due_date=date(2026, 8, 20), total_due=2000.0)

    book = dues.portfolio(conn, today=TODAY)
    assert [v.card.label for v in book.views] == ["Axis", "ICICI"]
    assert book.total_outstanding == pytest.approx(3000.0)
    assert book.next_due.card.label == "Axis"
    assert len(book.attention) == 1


def test_credit_balance_is_not_treated_as_owed(conn):
    card = make_card(conn)
    add_record(conn, card, total_due=-2500.0)
    view = dues.view_for_card(conn, card, today=TODAY)
    assert view.status == dues.STATUS_SETTLED
    assert view.utilisation == pytest.approx(0.0)


@pytest.mark.parametrize(
    "amount,expected",
    [
        (0, "₹0.00"),
        (999.5, "₹999.50"),
        (45231.5, "₹45,231.50"),
        (1234567.89, "₹12,34,567.89"),
        (-3410.75, "-₹3,410.75"),
        (None, "—"),
    ],
)
def test_indian_number_formatting(amount, expected):
    assert dues.format_inr(amount) == expected


def test_portfolio_orders_cards_by_statement_date_newest_first(conn):
    older = make_card(conn, label="Older Card", last4="1111")
    newer = make_card(conn, label="Newer Card", last4="2222")
    undated = make_card(conn, label="No Statement", last4="3333")
    add_record(conn, older, statement_date=date(2026, 7, 5))
    older_cycle = add_record(conn, newer, statement_date=date(2026, 7, 5), total_due=1000.0)
    add_record(conn, newer, statement_date=date(2026, 7, 20), total_due=2000.0)

    book = dues.portfolio(conn, today=TODAY)
    assert [v.card.last4 for v in book.views] == ["2222", "1111", "3333"]

    # Viewing an older cycle must not reorder the tile list.
    looking_back = dues.portfolio(
        conn, today=TODAY, selected={newer.id: older_cycle.id}
    )
    assert [v.card.last4 for v in looking_back.views] == ["2222", "1111", "3333"]
    viewed = next(v for v in looking_back.views if v.card.id == newer.id)
    assert viewed.statement_date == date(2026, 7, 5)
    assert not viewed.is_latest
