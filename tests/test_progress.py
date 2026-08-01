"""Progress callbacks and SSE ingest stream."""

from datetime import date, datetime

import pytest

from mydues import db, gmail, ingest
from mydues.models import SOURCE_STATEMENT, Card, StatementRecord
from mydues.web.app import create_app

from .fixtures import ICICI_AMAZON_DOUBLED_HEADERS, write_pdf


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_reparse_emits_progress_with_filenames(conn, monkeypatch, tmp_path):
    card = Card(issuer="icici", label="ICICI Amazon Pay", last4="4019")
    card.id = db.add_card(conn, card)
    db.save_statement(
        conn,
        StatementRecord(
            card_id=card.id,
            total_due=7317.0,
            statement_date=date(2026, 7, 28),
            due_date=date(2026, 8, 15),
            source=SOURCE_STATEMENT,
            source_ref="msg-amazon",
            as_of=datetime(2026, 8, 1, 1, 18),
        ),
    )
    filename = "4315XXXXXXXX4019.pdf"
    db.log_ingest(
        conn,
        message_id="msg-amazon",
        filename=filename,
        status=ingest.STATUS_PARSED,
        detail="ok",
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
                filename=filename,
                size=2048,
            )
        ],
        body="",
    )
    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *_a, **_k: message)
    monkeypatch.setattr(ingest.gmail, "download", lambda *_a, **_k: path)
    monkeypatch.setattr(ingest.gmail, "attachment_path", lambda *_a, **_k: tmp_path / "missing.pdf")

    events: list[ingest.ProgressEvent] = []
    summary = ingest.reparse_parsed(conn, issuer="icici", on_progress=events.append)

    assert summary.parsed == 1
    assert events[0].phase == "fetch"
    assert events[0].total == 1
    parse_events = [e for e in events if e.phase == "parse"]
    assert len(parse_events) == 1
    assert parse_events[0].filename == filename
    assert parse_events[0].index == 1
    done = [e for e in events if e.status]
    assert done[-1].filename == filename
    assert done[-1].status == ingest.STATUS_PARSED


def test_ingest_gmail_emits_progress_for_each_attachment(conn, monkeypatch, tmp_path):
    card = Card(issuer="icici", label="ICICI Amazon Pay", last4="4019")
    card.id = db.add_card(conn, card)

    path = tmp_path / "stmt.pdf"
    write_pdf(path, lines=ICICI_AMAZON_DOUBLED_HEADERS.splitlines())
    filename = "4315XXXXXXXX4019.pdf"
    message = gmail.Message(
        id="msg-new",
        sender="<credit_cards@icici.bank.in>",
        subject="Amazon Pay ICICI Bank Credit Card Statement",
        internal_date=int(datetime(2026, 7, 29, 11, 28).timestamp() * 1000),
        attachments=[
            gmail.Attachment(
                message_id="msg-new",
                attachment_id="att-1",
                filename=filename,
                size=2048,
            )
        ],
        body="",
    )
    monkeypatch.setattr(ingest.gmail, "service", lambda **_: object())
    monkeypatch.setattr(ingest.gmail, "search", lambda *_a, **_k: ["msg-new"])
    monkeypatch.setattr(ingest.gmail, "get_message", lambda *_a, **_k: message)
    monkeypatch.setattr(ingest.gmail, "download", lambda *_a, **_k: path)

    events: list[ingest.ProgressEvent] = []
    summary = ingest.ingest_gmail(conn, interactive=False, on_progress=events.append)

    assert summary.parsed == 1
    assert any(e.phase == "fetch" and e.total == 1 for e in events)
    assert any(e.phase == "parse" and e.filename == filename for e in events)
    assert any(e.status == ingest.STATUS_PARSED and e.filename == filename for e in events)


def test_ingest_stream_returns_done_for_empty_batch(client, conn, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: True)

    response = client.get("/ingest/stream?job=reprocess")
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers.get("X-Accel-Buffering") == "no"

    body = b"".join(response.response).decode()
    assert "data:" in body
    assert '"type": "done"' in body or '"type":"done"' in body
    assert "Nothing is waiting" in body


def test_ingest_stream_rejects_unknown_job(client, conn):
    response = client.get("/ingest/stream?job=nope")
    assert response.status_code == 400


def test_expense_stream_uses_lookback_days_without_request_context_error(
    client, conn, monkeypatch
):
    from mydues import expense_ingest
    from mydues.expense_ingest import ExpenseIngestSummary

    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    captured: dict = {}

    def fake_ingest(conn, **kwargs):
        captured["lookback_days"] = kwargs.get("lookback_days")
        return ExpenseIngestSummary(added=0, skipped=2)

    monkeypatch.setattr(expense_ingest, "ingest_expense_alerts", fake_ingest)

    response = client.get("/ingest/stream?job=expenses&days=1")
    assert response.status_code == 200
    body = b"".join(response.response).decode()
    assert "Working outside of request context" not in body
    assert '"type": "done"' in body
    assert captured["lookback_days"] == 1
    assert "last 1 day" in body


def test_statement_stream_uses_lookback_months(client, conn, monkeypatch):
    from mydues import ingest as ingest_mod
    from mydues.ingest import IngestSummary

    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    captured: dict = {}

    def fake_ingest(conn, **kwargs):
        captured["lookback_months"] = kwargs.get("lookback_months")
        return IngestSummary()

    monkeypatch.setattr(ingest_mod, "ingest_gmail", fake_ingest)

    response = client.get("/ingest/stream?job=fetch&months=1")
    assert response.status_code == 200
    body = b"".join(response.response).decode()
    assert "Working outside of request context" not in body
    assert '"type": "done"' in body
    assert captured["lookback_months"] == 1
    assert "last 1 month" in body


def test_cards_tab_shows_statement_months_lookback(client, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    page = client.get("/?tab=cards").get_data(as_text=True)
    assert "Fetch statements" in page
    assert 'name="months"' in page
    assert 'value="1"' in page
    assert "Fetch from Gmail" in page


def test_dashboard_wires_progress_forms_when_gmail_connected(client, conn, monkeypatch):
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    page = client.get("/").get_data(as_text=True)
    assert 'data-job="fetch"' in page
    assert 'data-job="reparse"' in page
    assert "progress.js" in page
    assert "progress-dialog" in page
