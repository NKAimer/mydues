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
    assert "45,231.50" in page
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
        data={"issuer": "hdfc", "last4": "8765", "dob": "15/04/1990"},
        follow_redirects=True,
    )

    assert db.list_cards(conn)[0].dob == date(1990, 4, 15)


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
