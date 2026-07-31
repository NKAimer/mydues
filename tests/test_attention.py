"""What the dashboard tells you about a statement it could not open."""

import sqlite3
from datetime import date, datetime

import pytest

from carddues import config, db, gmail, ingest, passwords
from carddues.models import Card
from carddues.web.app import create_app

from .fixtures import write_pdf

RULE = (
    "This statement is password protected. The password is the first 4 letters of "
    "your name in CAPITAL letters followed by your date of birth in DDMM format."
)
BODY = f"Dear Customer,\n\nYour July statement is attached.\n\n{RULE}\n\nRegards,\nHDFC Bank"


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def log_locked(conn, **overrides):
    fields = {
        "message_id": "msg-1",
        "filename": "statement.pdf",
        "status": ingest.STATUS_LOCKED,
        "detail": "statement.pdf is password protected and none of the 191 candidates worked",
        "sender": "HDFC Bank <estatement@hdfcbank.net>",
        "subject": "Your HDFC Bank Credit Card Statement - Jul 2026",
        "received_at": datetime(2026, 7, 28, 6, 15),
        "issuer": "hdfc",
        "password_rule": passwords.stated_rule(BODY),
    }
    fields.update(overrides)
    db.log_ingest(conn, **fields)


def test_the_mail_is_described_when_no_card_is_registered(client, conn):
    log_locked(conn)

    page = client.get("/").get_data(as_text=True)

    assert "estatement@hdfcbank.net" in page
    assert "Your HDFC Bank Credit Card Statement - Jul 2026" in page
    assert "28 Jul 2026, 06:15" in page
    assert "first 4 letters of your name in CAPITAL letters" in page
    assert "add the HDFC Bank card with the cardholder" in page
    assert 'name="password"' in page


def test_the_nudge_goes_away_once_the_card_is_registered(client, conn):
    log_locked(conn)
    db.add_card(conn, Card(issuer="hdfc", label="HDFC Infinia", last4="8765"))

    page = client.get("/").get_data(as_text=True)

    assert "add the HDFC Bank card with the cardholder" not in page
    # The mail context is still there, since the password is still needed.
    assert "estatement@hdfcbank.net" in page


def test_a_mail_without_a_stated_rule_says_nothing_extra(client, conn):
    log_locked(conn, password_rule="")

    page = client.get("/").get_data(as_text=True)

    assert "The mail says" not in page
    assert "estatement@hdfcbank.net" in page


def test_an_unknown_sender_still_lists_the_mail(client, conn):
    log_locked(conn, issuer=None, sender="statements@somebank.example")

    page = client.get("/").get_data(as_text=True)

    assert "statements@somebank.example" in page
    assert "with the cardholder" not in page


def test_a_long_backlog_is_capped_with_a_count(client, conn):
    """A first fetch can leave a hundred locked mails; the panel must stay readable."""
    for index in range(20):
        log_locked(conn, message_id=f"msg-{index}", filename=f"statement-{index}.pdf")

    page = client.get("/").get_data(as_text=True)

    assert page.count("/ingest/retry") == 12
    assert "showing 12 of 20" in page


def test_locked_detail_names_the_issuer_that_has_no_card(conn, tmp_path):
    db.add_card(conn, Card(issuer="axis", label="Axis", last4="1111"))
    path = tmp_path / "locked.pdf"
    write_pdf(path, password="whatever")

    result = ingest.ingest_pdf(conn, path, issuer_hint="hdfc")

    assert result.status == ingest.STATUS_LOCKED
    assert result.detail == "No HDFC Bank card is registered, so no password could be worked out"


def test_locked_detail_when_nothing_is_registered_at_all(conn, tmp_path):
    path = tmp_path / "locked.pdf"
    write_pdf(path, password="whatever")

    result = ingest.ingest_pdf(conn, path, issuer_hint="hdfc")

    assert result.detail == "No cards are registered yet, so no password could be worked out"


def test_locked_detail_points_at_missing_name_and_birth_date(conn, tmp_path):
    db.add_card(conn, Card(issuer="hdfc", label="HDFC", last4="8765"))
    path = tmp_path / "locked.pdf"
    write_pdf(path, password="whatever")

    result = ingest.ingest_pdf(conn, path, issuer_hint="hdfc")

    assert "cardholder name and date of birth" in result.detail


def test_a_fetch_records_the_mail_behind_each_attachment(conn, monkeypatch, tmp_path):
    """The context has to be captured during the fetch; it is gone afterwards."""
    attachment = gmail.Attachment(
        message_id="msg-9", attachment_id="att-1", filename="july.pdf", size=1024
    )
    message = gmail.Message(
        id="msg-9",
        sender="HDFC Bank <estatement@hdfcbank.net>",
        subject="Your statement",
        internal_date=int(datetime(2026, 7, 28, 6, 15).timestamp() * 1000),
        attachments=[attachment],
        body=BODY,
    )
    locked = tmp_path / "july.pdf"
    write_pdf(locked, password="unguessable")

    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "search", lambda *a, **k: ["msg-9"])
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *a, **k: message)
    monkeypatch.setattr(ingest.gmail, "download", lambda *a, **k: locked)

    summary = ingest.ingest_gmail(conn, interactive=False)
    assert summary.count(ingest.STATUS_LOCKED) == 1

    row = db.unresolved_ingest(conn)[0]
    assert row["sender"] == "HDFC Bank <estatement@hdfcbank.net>"
    assert row["subject"] == "Your statement"
    assert row["issuer"] == "hdfc"
    assert row["received_at"].startswith("2026-07-28T06:15")
    assert "first 4 letters" in row["password_rule"]


def test_a_retry_keeps_the_recorded_mail_context(conn, card_with_details):
    """Retrying knows nothing about the mail, so it must not blank the context."""
    log_locked(conn)
    path = gmail.attachment_path("msg-1", "statement.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(path, password="right-one")

    ingest.retry_locked(
        conn, message_id="msg-1", filename="statement.pdf", password="wrong-one"
    )

    row = db.unresolved_ingest(conn)[0]
    assert row["sender"] == "HDFC Bank <estatement@hdfcbank.net>"
    assert "first 4 letters" in row["password_rule"]


@pytest.fixture
def card_with_details(conn):
    card = Card(
        issuer="hdfc",
        label="HDFC Infinia",
        last4="8765",
        name="Naveen Kumar",
        dob=date(1990, 4, 15),
    )
    card.id = db.add_card(conn, card)
    return card


def test_an_older_database_gains_the_new_columns(tmp_path, monkeypatch):
    """Databases created before this feature must keep working."""
    monkeypatch.setenv("CARDDUES_HOME", str(tmp_path / "home"))
    config.ensure_dirs()
    old = sqlite3.connect(config.db_path())
    old.executescript(
        """
        CREATE TABLE ingest_log (
            id INTEGER PRIMARY KEY,
            message_id TEXT,
            filename TEXT,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (message_id, filename)
        );
        INSERT INTO ingest_log (message_id, filename, status, detail, created_at)
        VALUES ('old-1', 'old.pdf', 'locked', 'no password', '2026-07-01T09:00:00');
        """
    )
    old.commit()
    old.close()

    conn = db.connect()
    db.init(conn)

    row = db.unresolved_ingest(conn)[0]
    assert row["filename"] == "old.pdf"
    assert row["sender"] is None

    log_locked(conn, message_id="new-1", filename="new.pdf")
    assert len(db.unresolved_ingest(conn)) == 2
