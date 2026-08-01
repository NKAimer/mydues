"""Unlocking statements: from the email's stated rule, and from a typed password."""

from datetime import date

import pytest

from mydues import config, db, gmail, ingest
from mydues.models import Card
from mydues.web.app import create_app

from .fixtures import write_pdf

HINT = (
    "Dear Customer, your statement is attached and password protected. "
    "The password is the last 4 digits of your card number followed by the "
    "first 4 letters of your name in capital letters."
)
# The rule above puts the digits first, an order the fallback guesses never try.
STATED_PASSWORD = "8765NAVE"


@pytest.fixture
def card(conn):
    entry = Card(
        issuer="hdfc",
        label="HDFC Infinia",
        last4="8765",
        name="Naveen Kumar",
        dob=date(1990, 4, 15),
    )
    entry.id = db.add_card(conn, entry)
    return entry


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_the_stated_rule_opens_a_statement_guessing_cannot(conn, card, tmp_path):
    path = tmp_path / "statement.pdf"
    write_pdf(path, password=STATED_PASSWORD)

    from mydues import passwords

    assert STATED_PASSWORD not in passwords.candidates_for(card)

    result = ingest.ingest_pdf(conn, path, password_hint=HINT)
    assert result.status == ingest.STATUS_PARSED


def test_without_the_hint_the_same_statement_stays_locked(conn, card, tmp_path):
    path = tmp_path / "statement.pdf"
    write_pdf(path, password=STATED_PASSWORD)

    result = ingest.ingest_pdf(conn, path)
    assert result.status == ingest.STATUS_LOCKED


def test_the_stated_rule_is_tried_before_the_guesses(conn, card, tmp_path, monkeypatch):
    path = tmp_path / "statement.pdf"
    write_pdf(path)
    tried: list[str] = []

    def record(target, candidates=None):
        tried.extend(candidates or [])
        return original(target, candidates)

    original = ingest.pdfdoc.extract
    monkeypatch.setattr(ingest.pdfdoc, "extract", record)

    ingest.ingest_pdf(conn, path, password_hint=HINT)
    assert tried[0] == STATED_PASSWORD
    assert len(tried) > 1  # the guesses still follow


def test_an_unusual_rule_falls_back_to_guessing(conn, card, tmp_path):
    """The password here follows no stated rule, so the permutations must run."""
    path = tmp_path / "statement.pdf"
    write_pdf(path, password="nave1504")

    result = ingest.ingest_pdf(conn, path, password_hint="The password is your library card number.")
    assert result.status == ingest.STATUS_PARSED


def make_locked_attachment(conn, message_id="msg-1", filename="statement.pdf"):
    """A locked attachment left on disk, as a real fetch would leave it."""
    config.ensure_dirs()
    path = gmail.attachment_path(message_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(path, password="secret-one")
    db.log_ingest(
        conn,
        message_id=message_id,
        filename=filename,
        status=ingest.STATUS_LOCKED,
        detail="none of the candidate passwords worked",
    )
    return path


def test_retry_opens_the_kept_file_with_a_supplied_password(conn, card):
    path = make_locked_attachment(conn)

    result = ingest.retry_locked(
        conn, message_id="msg-1", filename="statement.pdf", password="secret-one"
    )

    assert result.status == ingest.STATUS_PARSED
    assert not path.exists()  # cleaned up once it parsed


def test_a_working_password_is_remembered_for_next_month(conn, card):
    make_locked_attachment(conn)

    ingest.retry_locked(
        conn, message_id="msg-1", filename="statement.pdf", password="secret-one"
    )

    assert db.get_card(conn, card.id).extra_passwords == ["secret-one"]


def test_a_wrong_password_leaves_the_file_for_another_try(conn, card):
    path = make_locked_attachment(conn)

    result = ingest.retry_locked(
        conn, message_id="msg-1", filename="statement.pdf", password="nope"
    )

    assert result.status == ingest.STATUS_LOCKED
    assert path.exists()
    assert [row["status"] for row in db.unresolved_ingest(conn)] == [ingest.STATUS_LOCKED]


def test_retry_reports_when_the_file_is_gone_and_gmail_is_unavailable(conn, card):
    db.log_ingest(
        conn,
        message_id="msg-2",
        filename="missing.pdf",
        status=ingest.STATUS_LOCKED,
        detail="locked",
    )

    result = ingest.retry_locked(
        conn, message_id="msg-2", filename="missing.pdf", password="secret-one"
    )

    assert result.status == ingest.STATUS_ERROR
    assert "not connected" in result.detail


def test_the_dashboard_offers_an_unlock_box_for_locked_attachments(client, conn, card):
    make_locked_attachment(conn)

    page = client.get("/").get_data(as_text=True)
    assert 'name="password"' in page
    assert "/ingest/retry" in page
    assert "statement.pdf" in page


def test_a_locally_imported_file_gets_no_unlock_box(client, conn, card):
    """There is no mail to re-fetch, so `mydues import --password` is the way."""
    db.log_ingest(
        conn,
        message_id=None,
        filename="from-disk.pdf",
        status=ingest.STATUS_LOCKED,
        detail="locked",
    )

    page = client.get("/").get_data(as_text=True)
    assert "from-disk.pdf" in page
    assert "/ingest/retry" not in page


def test_unlocking_from_the_dashboard(client, conn, card):
    make_locked_attachment(conn)

    response = client.post(
        "/ingest/retry",
        data={"message_id": "msg-1", "filename": "statement.pdf", "password": "secret-one"},
        follow_redirects=True,
    )

    page = response.get_data(as_text=True)
    assert "Unlocked statement.pdf" in page
    assert "45,231.50" in page
    assert "Attachments that need you" not in page


def test_the_dashboard_reports_a_wrong_password(client, conn, card):
    make_locked_attachment(conn)

    response = client.post(
        "/ingest/retry",
        data={"message_id": "msg-1", "filename": "statement.pdf", "password": "wrong"},
        follow_redirects=True,
    )

    assert "candidate passwords worked" in response.get_data(as_text=True)


def test_the_dashboard_needs_a_password_to_retry(client, conn, card):
    make_locked_attachment(conn)

    response = client.post(
        "/ingest/retry",
        data={"message_id": "msg-1", "filename": "statement.pdf", "password": "  "},
        follow_redirects=True,
    )

    assert "A password is needed" in response.get_data(as_text=True)
