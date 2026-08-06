"""What the dashboard tells you about a statement it could not open."""

import sqlite3
from datetime import date, datetime

import pytest

from mydues import config, db, gmail, ingest, passwords
from mydues.models import Card
from mydues.web.app import create_app

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
    assert "28/07/2026" in page
    assert "first 4 letters of your name in CAPITAL letters" in page
    assert "add the HDFC Bank card with the cardholder" in page
    assert 'name="password"' in page


def test_the_head_line_shows_the_mailer_address_and_date(client, conn):
    log_locked(conn)

    page = client.get("/").get_data(as_text=True)

    assert 'class="from">estatement@hdfcbank.net</span>' in page
    assert 'class="when">28/07/2026</span>' in page


def test_email_address_strips_the_display_name():
    from mydues.web.app import _email_address

    assert _email_address("HDFC Bank <estatement@hdfcbank.net>") == "estatement@hdfcbank.net"
    assert _email_address("cbssbi.cas@alerts.sbi.co.in") == "cbssbi.cas@alerts.sbi.co.in"
    assert _email_address(None) is None
    assert _email_address("") is None


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


def test_a_long_backlog_is_scrollable_with_full_count(client, conn):
    """A first fetch can leave many locked mails; the panel lists all and scrolls."""
    for index in range(20):
        log_locked(conn, message_id=f"msg-{index}", filename=f"statement-{index}.pdf")

    page = client.get("/").get_data(as_text=True)

    assert page.count("/ingest/retry") == 20
    assert "Attachments that need you" in page
    assert 'class="count">20</span>' in page or ">20</span>" in page
    assert "showing 12 of" not in page
    assert "issues-scroll" in page
    assert "category-section" in page
    css = open("mydues/web/static/app.css").read()
    assert "issues-scroll" in css
    assert "max-height" in css


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
        name="Ada Lovelace",
        dob=date(1815, 12, 10),
    )
    card.id = db.add_card(conn, card)
    return card


def test_an_older_database_gains_the_new_columns(tmp_path, monkeypatch):
    """Databases created before this feature must keep working."""
    monkeypatch.setenv("MYDUES_HOME", str(tmp_path / "home"))
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
