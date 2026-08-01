"""Flask dashboard. Binds to localhost and holds no secrets of its own."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for

from .. import config, db, dues, gmail, ingest, issuers
from ..dues import format_inr
from ..models import SOURCE_MANUAL, Card, StatementRecord

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    dues.STATUS_OVERDUE: "Overdue",
    dues.STATUS_DUE_TODAY: "Due today",
    dues.STATUS_DUE_SOON: "Due soon",
    dues.STATUS_UPCOMING: "Upcoming",
    dues.STATUS_SETTLED: "Settled",
    dues.STATUS_NO_DATA: "No data",
    dues.STATUS_UNKNOWN_DUE_DATE: "No due date",
}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def format_dmy(value: date | datetime | None) -> str:
    """Dates are written and read the Indian way: 05/08/2026."""
    return value.strftime("%d/%m/%Y") if value is not None else ""


def _parse_amount(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


ATTENTION_SHOWN = 12

# "HDFC Bank <estatement@hdfcbank.net>" → the address alone for the head line.
_EMAIL_IN_BRACKETS = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_BARE_EMAIL = re.compile(r"^[^<>@\s]+@[^<>@\s]+$")


def _email_address(sender: str | None) -> str | None:
    """The mailer's address, without the display name."""
    if not sender:
        return None
    value = sender.strip()
    match = _EMAIL_IN_BRACKETS.search(value)
    if match:
        return match.group(1)
    if _BARE_EMAIL.match(value):
        return value
    return value


def _attention(conn, limit: int = ATTENTION_SHOWN) -> list[dict]:
    """Attachments needing help, described well enough to act on.

    A statement from a card that was never registered cannot have its password
    worked out, so the mail it came in is the only thing left to go on.
    """
    registered = {card.issuer for card in db.list_cards(conn)}
    items: list[dict] = []
    for row in db.unresolved_ingest(conn, limit=limit):
        issuer_key = row["issuer"]
        issuer = issuers.ISSUERS.get(issuer_key) if issuer_key else None
        received = _as_datetime(row["received_at"])
        items.append(
            {
                "status": row["status"],
                "filename": row["filename"],
                "detail": row["detail"],
                "message_id": row["message_id"],
                "sender": row["sender"],
                "from_email": _email_address(row["sender"]),
                "subject": row["subject"],
                # Head line uses the Indian date; the mail block repeats it.
                "received": received.strftime("%d/%m/%Y") if received else None,
                "issuer_name": issuer.name if issuer else None,
                "password_rule": row["password_rule"],
                "needs_card": bool(issuer_key) and issuer_key not in registered,
                "can_retry": row["status"] == ingest.STATUS_LOCKED and bool(row["message_id"]),
            }
        )
    return items


def _reopen(conn, *, issuer: str | None = None) -> str:
    """Try the waiting attachments again, and say what came of it.

    Card details supplied now, a name or a date of birth, are what a statement
    that stayed locked months ago was missing.
    """
    if not gmail.is_connected():
        return ""

    try:
        summary = ingest.reprocess_pending(conn, issuer=issuer, interactive=False)
    except Exception:  # noqa: BLE001 - adding the card itself already worked
        logger.exception("Reprocessing waiting attachments failed")
        return "Could not reopen the waiting statements; try Fetch from Gmail."

    said = []
    if summary.parsed:
        said.append(f"Opened {summary.parsed} waiting statement(s).")
    elif summary.results:
        said.append("None of the waiting statements opened with these details.")
    if summary.remaining:
        said.append(f"{summary.remaining} attachment(s) still waiting.")
    return " ".join(said)


def _as_datetime(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def create_app() -> Flask:
    app = Flask(__name__)
    # Signs flash messages and the OAuth state in a single-user local session.
    app.secret_key = config.session_secret()

    app.jinja_env.filters["inr"] = format_inr
    app.jinja_env.filters["dmy"] = format_dmy

    def _callback_uri() -> str:
        """Where Google should send the browser back.

        Desktop clients have no registered redirect URI and Google documents a
        bare loopback origin for them, so the dashboard root doubles as the
        callback. Web clients get an explicit path, which the user registers.
        """
        if gmail.client_type() == "installed":
            return url_for("index", _external=True)
        return url_for("oauth_callback", _external=True)

    def _finish_auth():
        error = request.args.get("error")
        if error:
            flash(f"Google returned '{error}'. Gmail was not connected.", "error")
            return redirect(url_for("index"))

        expected = session.pop("oauth_state", None)
        received = request.args.get("state")
        if not expected or expected != received:
            flash("Authorisation state did not match. Start the connection again.", "error")
            return redirect(url_for("index"))

        code = request.args.get("code")
        if not code:
            flash("Google did not return an authorisation code.", "error")
            return redirect(url_for("index"))

        try:
            gmail.exchange_code(_callback_uri(), code, state=received)
        except (gmail.GmailNotConfigured, gmail.GmailAuthFailed) as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except Exception as exc:  # noqa: BLE001 - surface provider errors verbatim
            logger.exception("Token exchange failed")
            flash(f"Could not complete authorisation: {exc}", "error")
            return redirect(url_for("index"))

        flash("Gmail connected. Read-only access only.", "success")
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        if request.args.get("code") or request.args.get("error"):
            return _finish_auth()

        conn = db.connect()
        db.init(conn)
        # ?statement=<id> looks back at one earlier cycle; the rest stay latest.
        chosen = db.get_statement(conn, request.args.get("statement", type=int) or 0)
        book = dues.portfolio(conn, selected={chosen.card_id: chosen.id} if chosen else None)
        return render_template(
            "index.html",
            book=book,
            status_labels=STATUS_LABELS,
            issuers=sorted(issuers.ISSUERS.values(), key=lambda i: i.name),
            today=date.today(),
            unresolved=_attention(conn),
            unresolved_total=db.count_unresolved_ingest(conn),
            gmail_connected=gmail.is_connected(),
            gmail_client_type=gmail.client_type(),
            credentials_path=config.credentials_path(),
            callback_uri=_callback_uri(),
        )

    @app.get("/auth/start")
    def auth_start():
        """Send the browser to Google's consent screen."""
        try:
            url, state = gmail.authorization_url(_callback_uri())
        except gmail.GmailNotConfigured as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        session["oauth_state"] = state
        return redirect(url)

    @app.get("/oauth/callback")
    def oauth_callback():
        """Receive the authorisation code and store a token."""
        return _finish_auth()

    @app.post("/auth/disconnect")
    def auth_disconnect():
        gmail.disconnect()
        flash(
            "Forgot the stored token. Revoke the grant itself under your Google "
            "account permissions.",
            "success",
        )
        return redirect(url_for("index"))

    @app.get("/api/dues")
    def api_dues():
        conn = db.connect()
        db.init(conn)
        book = dues.portfolio(conn)
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_outstanding": book.total_outstanding,
            "total_min_due": book.total_min_due,
            "utilisation_percent": book.utilisation,
            "cards": [
                {
                    "card": view.card.label,
                    "issuer": view.card.issuer,
                    "last4": view.card.last4,
                    "status": view.status,
                    "billed_amount": view.billed_amount,
                    "outstanding": view.outstanding,
                    "min_due": view.min_due_remaining,
                    "due_date": view.due_date.isoformat() if view.due_date else None,
                    "statement_date": (
                        view.statement_date.isoformat() if view.statement_date else None
                    ),
                    "source": view.source,
                    "as_of": view.as_of.isoformat() if view.as_of else None,
                    "stale": view.is_stale,
                }
                for view in book.views
            ],
        }

    @app.post("/cards")
    def add_card():
        conn = db.connect()
        db.init(conn)
        form = request.form
        last4 = (form.get("last4") or "").strip()
        if len(last4) != 4 or not last4.isdigit():
            flash("Enter the last 4 digits of the card.", "error")
            return redirect(url_for("index"))

        issuer = form.get("issuer") or "other"
        card = Card(
            issuer=issuer,
            label=(form.get("label") or "").strip()
            or f"{issuers.get(issuer).name} ••{last4}",
            last4=last4,
            credit_limit=_parse_amount(form.get("credit_limit")),
            name=(form.get("name") or "").strip() or None,
            dob=_parse_date(form.get("dob")),
            pan=(form.get("pan") or "").strip() or None,
            extra_passwords=[p for p in [(form.get("password") or "").strip()] if p],
        )
        db.add_card(conn, card)
        flash(f"Added {card.label}. {_reopen(conn, issuer=card.issuer)}".strip(), "success")
        return redirect(url_for("index"))

    @app.post("/cards/<int:card_id>/set")
    def set_due(card_id: int):
        """Manual entry: what the user types always wins over a parsed value."""
        conn = db.connect()
        db.init(conn)
        card = db.get_card(conn, card_id)
        if card is None:
            flash("That card no longer exists.", "error")
            return redirect(url_for("index"))

        total = _parse_amount(request.form.get("total_due"))
        if total is None:
            flash("Enter the total amount due.", "error")
            return redirect(url_for("index"))

        record = StatementRecord(
            card_id=card_id,
            total_due=total,
            min_due=_parse_amount(request.form.get("min_due")),
            due_date=_parse_date(request.form.get("due_date")),
            statement_date=_parse_date(request.form.get("statement_date")) or date.today(),
            credit_limit=_parse_amount(request.form.get("credit_limit")),
            source=SOURCE_MANUAL,
            as_of=datetime.now(),
            confidence=1.0,
            note=(request.form.get("note") or "").strip() or None,
        )
        db.save_statement(conn, record)
        flash(f"Updated {card.label}.", "success")
        return redirect(url_for("index"))

    @app.post("/cards/<int:card_id>/paid")
    def record_payment(card_id: int):
        conn = db.connect()
        db.init(conn)
        card = db.get_card(conn, card_id)
        if card is None:
            flash("That card no longer exists.", "error")
            return redirect(url_for("index"))

        view = dues.view_for_card(conn, card)
        amount = _parse_amount(request.form.get("amount")) or (view.outstanding or 0.0)
        if amount <= 0:
            flash("Nothing outstanding to record.", "error")
            return redirect(url_for("index"))

        db.add_payment(
            conn, card_id, amount, _parse_date(request.form.get("paid_on")) or date.today()
        )
        flash(f"Recorded {format_inr(amount)} against {card.label}.", "success")
        return redirect(url_for("index"))

    @app.post("/cards/<int:card_id>/delete")
    def delete_card(card_id: int):
        conn = db.connect()
        db.init(conn)
        card = db.get_card(conn, card_id)
        if card:
            db.delete_card(conn, card_id)
            flash(f"Removed {card.label}.", "success")
        return redirect(url_for("index"))

    @app.post("/ingest")
    def run_ingest():
        conn = db.connect()
        db.init(conn)
        try:
            summary = ingest.ingest_gmail(conn, interactive=False)
        except gmail.GmailNotConfigured as exc:
            flash(f"{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ingest failed")
            flash(f"Ingest failed: {exc}", "error")
            return redirect(url_for("index"))

        message = f"Parsed {summary.parsed} statement(s)."
        if summary.needs_attention:
            message += f" {len(summary.needs_attention)} attachment(s) need attention."
        flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/ingest/reprocess")
    def reprocess_pending():
        """Work through the backlog a batch at a time."""
        conn = db.connect()
        db.init(conn)
        if not gmail.is_connected():
            flash("Connect Gmail first so the attachments can be fetched again.", "error")
            return redirect(url_for("index"))

        message = _reopen(conn)
        flash(message or "Nothing is waiting to be opened.", "success")
        return redirect(url_for("index"))

    @app.post("/ingest/reparse")
    def reparse_parsed():
        """Rewrite stored cycles with the current parsers."""
        conn = db.connect()
        db.init(conn)
        if not gmail.is_connected():
            flash("Connect Gmail first so the statements can be fetched again.", "error")
            return redirect(url_for("index"))

        try:
            summary = ingest.reparse_parsed(conn, interactive=False)
        except Exception:  # noqa: BLE001
            logger.exception("Reparse failed")
            flash("Could not re-parse statements.", "error")
            return redirect(url_for("index"))

        if not summary.results:
            flash("No parsed statements to re-read.", "success")
        else:
            message = f"Re-parsed {summary.parsed} of {len(summary.results)} statement(s)."
            if summary.remaining:
                message += f" {summary.remaining} still to re-read."
            category = "success"
            if summary.needs_attention:
                category = "error" if summary.parsed == 0 else "warning"
                first = summary.needs_attention[0]
                message += f" {first.status}: {first.filename}: {first.detail}"
            flash(message, category)
        return redirect(url_for("index"))

    @app.post("/ingest/retry")
    def retry_locked():
        conn = db.connect()
        db.init(conn)
        form = request.form
        password = (form.get("password") or "").strip()
        message_id = (form.get("message_id") or "").strip()
        filename = (form.get("filename") or "").strip()
        if not (password and message_id and filename):
            flash("A password is needed to unlock that statement.", "error")
            return redirect(url_for("index"))

        result = ingest.retry_locked(
            conn, message_id=message_id, filename=filename, password=password
        )
        if result.status == ingest.STATUS_PARSED:
            flash(f"Unlocked {filename}. {result.detail}", "success")
        else:
            flash(f"{filename}: {result.detail}", "error")
        return redirect(url_for("index"))

    return app
