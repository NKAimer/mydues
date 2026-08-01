"""Trying the waiting attachments again once a card can open them."""

from datetime import date, datetime

import pytest

from carddues import db, gmail, ingest
from carddues.models import Card
from carddues.web.app import create_app

from .fixtures import STATEMENT_WITH_TRANSACTIONS, write_pdf

PASSWORD = "NAVE1504"
BODY = (
    "Dear Customer, your statement is attached. The password is the first 4 letters "
    "of your name in capital letters followed by your date of birth in DDMM format."
)


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def message(message_id="msg-1", filename="july.pdf"):
    return gmail.Message(
        id=message_id,
        sender="HDFC Bank <estatement@hdfcbank.net>",
        subject="Your HDFC Bank Credit Card Statement",
        internal_date=int(datetime(2026, 7, 28, 6, 15).timestamp() * 1000),
        attachments=[
            gmail.Attachment(
                message_id=message_id, attachment_id="att-1", filename=filename, size=2048
            )
        ],
        body=BODY,
    )


@pytest.fixture
def waiting(conn, tmp_path):
    """A locked attachment logged with none of the mail recorded, as old rows are."""
    db.log_ingest(
        conn,
        message_id="msg-1",
        filename="july.pdf",
        status=ingest.STATUS_LOCKED,
        detail="No cards are registered yet, so no password could be worked out",
    )
    locked = tmp_path / "july.pdf"
    write_pdf(locked, password=PASSWORD, lines=STATEMENT_WITH_TRANSACTIONS)
    return locked


@pytest.fixture
def gmail_returns(monkeypatch, waiting):
    """Gmail that hands back the locked file, counting the downloads."""
    downloads: list[str] = []

    def download(_service, attachment):
        downloads.append(attachment.filename)
        return waiting

    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "get_message", lambda _s, message_id: message(message_id))
    monkeypatch.setattr(ingest.gmail, "download", download)
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    return downloads


def add_card(conn, **overrides):
    fields = {
        "issuer": "hdfc",
        "label": "HDFC Infinia",
        "last4": "8765",
        "name": "Naveen Kumar",
        "dob": date(1990, 4, 15),
    }
    fields.update(overrides)
    card = Card(**fields)
    card.id = db.add_card(conn, card)
    return card


def test_a_waiting_statement_opens_once_a_card_can_unlock_it(conn, gmail_returns):
    add_card(conn)

    summary = ingest.reprocess_pending(conn)

    assert summary.parsed == 1
    assert summary.remaining == 0
    assert db.list_cards(conn)[0].label == "HDFC Infinia"


def test_the_mail_context_is_filled_in_on_the_way_past(conn, gmail_returns):
    """Rows logged before the mail was recorded get it back when they are retried."""
    summary = ingest.reprocess_pending(conn)

    assert summary.parsed == 0  # no card yet, so it stays locked
    row = db.pending_ingest(conn)[0]
    assert row["sender"] == "HDFC Bank <estatement@hdfcbank.net>"
    assert row["issuer"] == "hdfc"
    assert row["received_at"].startswith("2026-07-28T06:15")
    assert "first 4 letters" in row["password_rule"]


def test_a_file_still_on_disk_is_not_downloaded_again(conn, gmail_returns, tmp_path):
    kept = gmail.attachment_path("msg-1", "july.pdf")
    kept.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(kept, password=PASSWORD, lines=STATEMENT_WITH_TRANSACTIONS)
    add_card(conn)

    summary = ingest.reprocess_pending(conn)

    assert summary.parsed == 1
    assert gmail_returns == []


def test_a_kept_file_still_backfills_missing_mail_context(conn, gmail_returns):
    """Older log rows have no sender/date; a kept PDF must not leave them blank."""
    kept = gmail.attachment_path("msg-1", "july.pdf")
    kept.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(kept, password=PASSWORD, lines=STATEMENT_WITH_TRANSACTIONS)

    summary = ingest.reprocess_pending(conn)

    assert summary.parsed == 0  # still locked without a card
    assert gmail_returns == []  # no download; metadata only
    row = db.pending_ingest(conn)[0]
    assert row["sender"] == "HDFC Bank <estatement@hdfcbank.net>"
    assert row["received_at"].startswith("2026-07-28T06:15")
    assert row["issuer"] == "hdfc"


def test_a_kept_file_skips_gmail_when_mail_context_is_already_there(conn, monkeypatch):
    kept = gmail.attachment_path("msg-1", "july.pdf")
    kept.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(kept, password="secret")
    db.log_ingest(
        conn,
        message_id="msg-1",
        filename="july.pdf",
        status=ingest.STATUS_LOCKED,
        detail="locked",
        sender="HDFC Bank <estatement@hdfcbank.net>",
        received_at=datetime(2026, 7, 28, 6, 15),
        issuer="hdfc",
    )
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("get_message")
        raise AssertionError("mail context is already stored")

    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "get_message", boom)
    monkeypatch.setattr(gmail, "is_connected", lambda: True)

    ingest.reprocess_pending(conn)

    assert called == []


def test_the_line_items_are_stored_by_the_retry(conn, gmail_returns):
    card = add_card(conn)

    ingest.reprocess_pending(conn)

    statement = db.statements_for_card(conn, card.id)[0]
    assert len(db.transactions_for_statement(conn, statement.id)) == 4


def test_a_pass_is_capped_and_says_what_is_left(conn, gmail_returns):
    for index in range(5):
        db.log_ingest(
            conn,
            message_id=f"msg-extra-{index}",
            filename="july.pdf",
            status=ingest.STATUS_LOCKED,
            detail="locked",
        )

    summary = ingest.reprocess_pending(conn, limit=2)

    assert len(summary.results) == 2
    assert summary.remaining == 6  # the five above and the one from the fixture


def test_another_issuers_backlog_is_left_alone(conn, gmail_returns):
    db.log_ingest(
        conn,
        message_id="msg-axis",
        filename="axis.pdf",
        status=ingest.STATUS_LOCKED,
        detail="locked",
        issuer="axis",
    )

    summary = ingest.reprocess_pending(conn, issuer="hdfc")

    assert [result.filename for result in summary.results] == ["july.pdf"]


def test_a_row_with_no_recorded_issuer_is_always_tried(conn, gmail_returns):
    """Attachments logged before the issuer was recorded must not be stranded."""
    summary = ingest.reprocess_pending(conn, issuer="axis")

    assert [result.filename for result in summary.results] == ["july.pdf"]


def test_a_local_import_is_skipped_since_there_is_no_mail(conn, gmail_returns):
    db.log_ingest(
        conn,
        message_id=None,
        filename="from-disk.pdf",
        status=ingest.STATUS_LOCKED,
        detail="locked",
    )

    summary = ingest.reprocess_pending(conn)

    assert "from-disk.pdf" not in [result.filename for result in summary.results]


def test_the_pass_stops_when_gmail_is_not_connected(conn, waiting, monkeypatch):
    monkeypatch.setattr(
        ingest.gmail,
        "service",
        lambda **_: (_ for _ in ()).throw(gmail.GmailNotConfigured("Gmail is not connected")),
    )

    summary = ingest.reprocess_pending(conn)

    assert summary.results[0].status == ingest.STATUS_ERROR
    assert "not connected" in summary.results[0].detail


def test_an_attachment_gone_from_the_mail_is_reported(conn, waiting, monkeypatch):
    empty = message()
    empty.attachments = []
    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *a, **k: empty)

    summary = ingest.reprocess_pending(conn)

    assert summary.results[0].status == ingest.STATUS_ERROR
    assert "no longer in the mail" in summary.results[0].detail


def test_adding_a_card_on_the_dashboard_reopens_the_backlog(client, conn, gmail_returns):
    response = client.post(
        "/cards",
        data={
            "issuer": "hdfc",
            "last4": "8765",
            "label": "HDFC Infinia",
            "name": "Naveen Kumar",
            "dob": "15/04/1990",
        },
        follow_redirects=True,
    )

    page = response.get_data(as_text=True)
    assert "Opened 1 waiting statement(s)" in page
    assert "45,231.50" in page


def test_the_dashboard_can_retry_the_backlog_on_its_own(client, conn, gmail_returns):
    add_card(conn)

    response = client.post("/ingest/reprocess", follow_redirects=True)

    assert "Opened 1 waiting statement(s)" in response.get_data(as_text=True)


def test_the_button_is_offered_only_when_gmail_is_connected(client, conn, waiting, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: False)

    page = client.get("/").get_data(as_text=True)

    assert "/ingest/reprocess" not in page


def test_reprocessing_without_gmail_says_so(client, conn, waiting, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: False)

    response = client.post("/ingest/reprocess", follow_redirects=True)

    assert "Connect Gmail first" in response.get_data(as_text=True)


def test_adding_a_card_without_gmail_just_adds_it(client, conn, waiting, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: False)

    response = client.post(
        "/cards", data={"issuer": "hdfc", "last4": "8765"}, follow_redirects=True
    )

    page = response.get_data(as_text=True)
    assert "Added" in page
    assert "waiting statement" not in page
