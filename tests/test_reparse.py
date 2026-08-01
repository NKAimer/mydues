"""Re-reading already-parsed statements after a parser fix."""

from datetime import date, datetime

import pytest

from mydues import db, gmail, ingest, issuers
from mydues.models import Card, SOURCE_STATEMENT, StatementRecord
from mydues.web.app import create_app

from .fixtures import ICICI_AMAZON_DOUBLED_HEADERS, write_pdf


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


def test_icici_bank_in_is_a_known_sender():
    assert "icici.bank.in" in issuers.get("icici").senders
    assert issuers.detect_from_sender("<credit_cards@icici.bank.in>") == "icici"


def test_reparse_rewrites_wrong_dates_on_an_already_parsed_attachment(
    conn, card, monkeypatch, tmp_path
):
    # What the buggy parser stored for the July Amazon Pay cycle.
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=7317.0,
            statement_date=date(2026, 6, 29),
            due_date=date(2023, 10, 26),
            source=SOURCE_STATEMENT,
            source_ref="msg-amazon",
            as_of=datetime(2026, 8, 1, 1, 18),
        ),
    )
    db.log_ingest(
        conn,
        message_id="msg-amazon",
        filename="4315XXXXXXXX4019.pdf",
        status=ingest.STATUS_PARSED,
        detail="ICICI Bank ••4019: total due 7317.00",
        issuer="icici",
        sender="<credit_cards@icici.bank.in>",
        received_at=datetime(2026, 7, 29, 11, 28),
    )

    path = tmp_path / "amazon.pdf"
    write_pdf(path, lines=ICICI_AMAZON_DOUBLED_HEADERS.splitlines())
    message = gmail.Message(
        id="msg-amazon",
        sender="<credit_cards@icici.bank.in>",
        subject="Amazon Pay ICICI Bank Credit Card Statement",
        internal_date=int(datetime(2026, 7, 29, 11, 28).timestamp() * 1000),
        attachments=[
            gmail.Attachment(
                message_id="msg-amazon",
                attachment_id="att-1",
                filename="4315XXXXXXXX4019.pdf",
                size=2048,
            )
        ],
        body="",
    )
    downloads: list[str] = []

    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *_a, **_k: message)
    monkeypatch.setattr(
        ingest.gmail,
        "download",
        lambda *_a, **_k: downloads.append("yes") or path,
    )
    monkeypatch.setattr(ingest.gmail, "attachment_path", lambda *_a, **_k: tmp_path / "missing.pdf")

    summary = ingest.reparse_parsed(conn, issuer="icici")

    assert summary.parsed == 1
    assert downloads == ["yes"]
    stored_rows = db.statements_for_card(conn, card.id)
    assert len(stored_rows) == 1
    stored = stored_rows[0]
    assert stored.statement_date == date(2026, 7, 28)
    assert stored.due_date == date(2026, 8, 15)
    assert stored.total_due == pytest.approx(7317.0)
    assert stored.statement_date != date(2026, 6, 29)


def test_failed_reparse_keeps_ingest_parsed_for_retry(conn, card, monkeypatch):
    db.log_ingest(
        conn,
        message_id="msg-gone",
        filename="gone.pdf",
        status=ingest.STATUS_PARSED,
        detail="ok",
        issuer="icici",
    )
    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(
        ingest,
        "_local_copy",
        lambda *_a, **_k: (_ for _ in ()).throw(ingest.AttachmentGone("gone.pdf")),
    )

    summary = ingest.reparse_parsed(conn, issuer="icici")

    assert summary.parsed == 0
    assert len(summary.needs_attention) == 1
    row = conn.execute(
        "SELECT status, detail FROM ingest_log WHERE message_id = ?", ("msg-gone",)
    ).fetchone()
    assert row["status"] == ingest.STATUS_PARSED
    assert "Reparse error" in row["detail"]
    assert db.count_parsed_ingest(conn, issuer="icici") == 1


def test_fetch_still_skips_parsed_attachments(conn, card, monkeypatch):
    db.log_ingest(
        conn,
        message_id="msg-amazon",
        filename="already.pdf",
        status=ingest.STATUS_PARSED,
        detail="ok",
        issuer="icici",
    )
    message = gmail.Message(
        id="msg-amazon",
        sender="<credit_cards@icici.bank.in>",
        subject="statement",
        internal_date=0,
        attachments=[
            gmail.Attachment(
                message_id="msg-amazon",
                attachment_id="a",
                filename="already.pdf",
                size=1,
            )
        ],
    )
    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "search", lambda *_a, **_k: ["msg-amazon"])
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *_a, **_k: message)
    monkeypatch.setattr(
        ingest.gmail,
        "download",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not download parsed mail")),
    )

    summary = ingest.ingest_gmail(conn, interactive=False)

    assert summary.results == []


def test_reparse_without_limit_covers_more_than_a_batch(conn, monkeypatch):
    """Dashboard/default reparse must not stop after the old batch of 40."""
    batch = ingest.REPROCESS_BATCH
    total = batch + 5
    for index in range(total):
        db.log_ingest(
            conn,
            message_id=f"msg-{index}",
            filename=f"stmt-{index}.pdf",
            status=ingest.STATUS_PARSED,
            detail="ok",
            issuer="icici",
            received_at=datetime(2026, 1, 1, 12, index % 60),
        )

    seen: list[str] = []

    def fake_local_copy(message_id, filename, **_kwargs):
        seen.append(filename)
        raise ingest.AttachmentGone(filename)

    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest, "_local_copy", fake_local_copy)

    summary = ingest.reparse_parsed(conn)

    assert len(summary.results) == total
    assert len(seen) == total
    assert summary.remaining == 0


def test_reparse_limit_still_batches(conn, monkeypatch):
    for index in range(5):
        db.log_ingest(
            conn,
            message_id=f"msg-{index}",
            filename=f"stmt-{index}.pdf",
            status=ingest.STATUS_PARSED,
            detail="ok",
            issuer="hdfc",
            received_at=datetime(2026, 2, 1, 12, index),
        )

    seen: list[str] = []

    def fake_local_copy(message_id, filename, **_kwargs):
        seen.append(filename)
        raise ingest.AttachmentGone(filename)

    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest, "_local_copy", fake_local_copy)

    summary = ingest.reparse_parsed(conn, limit=2)

    assert len(summary.results) == 2
    assert len(seen) == 2
    assert summary.remaining == 3
    # Newest first: minutes 4 and 3.
    assert seen == ["stmt-4.pdf", "stmt-3.pdf"]


def test_dashboard_offers_reparse_when_gmail_is_connected(client, conn, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: True)

    page = client.get("/").get_data(as_text=True)

    assert "/ingest/reparse" in page
    assert "Re-parse statements" in page


def test_dashboard_reparse_reports_what_it_rewrote(client, conn, card, monkeypatch, tmp_path):
    db.log_ingest(
        conn,
        message_id="msg-amazon",
        filename="4315XXXXXXXX4019.pdf",
        status=ingest.STATUS_PARSED,
        detail="old",
        issuer="icici",
    )
    path = tmp_path / "amazon.pdf"
    write_pdf(path, lines=ICICI_AMAZON_DOUBLED_HEADERS.splitlines())
    message = gmail.Message(
        id="msg-amazon",
        sender="<credit_cards@icici.bank.in>",
        subject="statement",
        internal_date=int(datetime(2026, 7, 29, 11, 28).timestamp() * 1000),
        attachments=[
            gmail.Attachment(
                message_id="msg-amazon",
                attachment_id="a",
                filename="4315XXXXXXXX4019.pdf",
                size=1,
            )
        ],
        body="",
    )
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *_a, **_k: message)
    monkeypatch.setattr(ingest.gmail, "download", lambda *_a, **_k: path)
    monkeypatch.setattr(ingest.gmail, "attachment_path", lambda *_a, **_k: tmp_path / "nope.pdf")

    response = client.post("/ingest/reparse", follow_redirects=True)

    assert "Re-parsed 1 of 1 statement(s)" in response.get_data(as_text=True)
