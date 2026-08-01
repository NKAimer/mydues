"""Flask dashboard. Binds to localhost and holds no secrets of its own."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from flask import Flask, Response, flash, redirect, render_template, request, session, stream_with_context, url_for

from .. import config, db, dues, expense_ingest, gmail, ingest, issuers
from ..categories import remember_merchant_category
from ..dues import format_inr
from ..models import SOURCE_MANUAL, Card, Expense, StatementRecord

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


def _parse_month(value: str | None) -> date:
    """First day of YYYY-MM, or the current month."""
    today = date.today()
    if value:
        try:
            year_s, month_s = value.strip().split("-", 1)
            year, month = int(year_s), int(month_s)
            if 1 <= month <= 12:
                return date(year, month, 1)
        except ValueError:
            pass
    return date(today.year, today.month, 1)


def _month_window(month_start: date) -> tuple[date, date]:
    start = month_start.replace(day=1)
    # Exclusive end = first of next month.
    if month_start.month == 12:
        end = date(month_start.year + 1, 1, 1)
    else:
        end = date(month_start.year, month_start.month + 1, 1)
    return start, end


def _shift_month(month_start: date, delta: int) -> date:
    year = month_start.year
    month = month_start.month + delta
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, 1)


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
        tab = (request.args.get("tab") or "cards").strip().lower()
        if tab not in {"cards", "expenses"}:
            tab = "cards"

        # ?statement=<id> looks back at one earlier cycle; the rest stay latest.
        chosen = db.get_statement(conn, request.args.get("statement", type=int) or 0)
        book = dues.portfolio(conn, selected={chosen.card_id: chosen.id} if chosen else None)

        month_start = _parse_month(request.args.get("month"))
        month_end_exclusive = _month_window(month_start)[1]
        expenses = db.list_expenses(conn, start=month_start, end=month_end_exclusive)
        expense_total = db.expense_total(conn, start=month_start, end=month_end_exclusive)

        return render_template(
            "index.html",
            book=book,
            tab=tab,
            status_labels=STATUS_LABELS,
            issuers=sorted(issuers.ISSUERS.values(), key=lambda i: i.name),
            today=date.today(),
            unresolved=_attention(conn),
            unresolved_total=db.count_unresolved_ingest(conn),
            gmail_connected=gmail.is_connected(),
            gmail_client_type=gmail.client_type(),
            credentials_path=config.credentials_path(),
            callback_uri=_callback_uri(),
            expenses=expenses,
            expense_total=expense_total,
            expense_month=month_start,
            expense_month_key=month_start.strftime("%Y-%m"),
            expense_month_label=month_start.strftime("%B %Y"),
            expense_prev_month=_shift_month(month_start, -1).strftime("%Y-%m"),
            expense_next_month=_shift_month(month_start, 1).strftime("%Y-%m"),
            expense_months=db.expense_months(conn),
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

    @app.get("/ingest/stream")
    def ingest_stream():
        """Stream per-item progress for Fetch / Re-parse / Try again / Expenses."""
        job = (request.args.get("job") or "").strip()
        if job not in {"fetch", "reparse", "reprocess", "expenses"}:
            return Response("Unknown job", status=400)

        def event_stream():
            import json
            import queue
            import threading

            events: queue.Queue = queue.Queue()

            def on_progress(event: ingest.ProgressEvent) -> None:
                payload = {
                    "type": "item" if event.status else "progress",
                    "index": event.index,
                    "total": event.total,
                    "filename": event.filename,
                    "phase": event.phase,
                    "status": event.status,
                    "detail": event.detail,
                }
                events.put(payload)

            def run_job() -> None:
                conn = db.connect()
                try:
                    db.init(conn)
                    if job == "fetch":
                        summary = ingest.ingest_gmail(
                            conn, interactive=False, on_progress=on_progress
                        )
                        message = f"Parsed {summary.parsed} statement(s)."
                        if summary.needs_attention:
                            message += (
                                f" {len(summary.needs_attention)} attachment(s) need attention."
                            )
                        done_total = len(summary.results)
                        done_parsed = summary.parsed
                        done_remaining = summary.remaining
                    elif job == "reparse":
                        if not gmail.is_connected():
                            raise gmail.GmailNotConfigured(
                                "Connect Gmail first so the statements can be fetched again."
                            )
                        summary = ingest.reparse_parsed(
                            conn, interactive=False, on_progress=on_progress
                        )
                        if not summary.results:
                            message = "No parsed statements to re-read."
                        else:
                            message = (
                                f"Re-parsed {summary.parsed} of {len(summary.results)} "
                                "statement(s)."
                            )
                            if summary.remaining:
                                message += f" {summary.remaining} still to re-read."
                        done_total = len(summary.results)
                        done_parsed = summary.parsed
                        done_remaining = summary.remaining
                    elif job == "expenses":
                        if not gmail.is_connected():
                            raise gmail.GmailNotConfigured(
                                "Connect Gmail first so expense alerts can be fetched."
                            )
                        summary = expense_ingest.ingest_expense_alerts(
                            conn, interactive=False, on_progress=on_progress
                        )
                        message = (
                            f"Added {summary.added} expense(s) from Gmail"
                            + (f"; skipped {summary.skipped}." if summary.skipped else ".")
                        )
                        done_total = len(summary.results)
                        done_parsed = summary.added
                        done_remaining = 0
                    else:
                        if not gmail.is_connected():
                            raise gmail.GmailNotConfigured(
                                "Connect Gmail first so the attachments can be fetched again."
                            )
                        summary = ingest.reprocess_pending(
                            conn, interactive=False, on_progress=on_progress
                        )
                        message = (
                            f"Opened {summary.parsed} of {len(summary.results)} "
                            "waiting attachment(s)."
                            if summary.results
                            else "Nothing is waiting to be opened."
                        )
                        if summary.remaining:
                            message += f" {summary.remaining} still waiting."
                        done_total = len(summary.results)
                        done_parsed = summary.parsed
                        done_remaining = summary.remaining
                    events.put(
                        {
                            "type": "done",
                            "parsed": done_parsed,
                            "total": done_total,
                            "remaining": done_remaining,
                            "message": message,
                        }
                    )
                except gmail.GmailNotConfigured as exc:
                    events.put({"type": "error", "message": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Streaming %s failed", job)
                    events.put({"type": "error", "message": str(exc)})
                finally:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    events.put(None)

            threading.Thread(target=run_job, daemon=True).start()
            while True:
                item = events.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"

        response = Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

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

    @app.post("/expenses")
    def add_expense():
        conn = db.connect()
        db.init(conn)
        form = request.form
        amount = _parse_amount(form.get("amount"))
        spent_on = _parse_date(form.get("spent_on")) or date.today()
        description = (form.get("description") or "").strip()
        category = (form.get("category") or "").strip() or None
        note = (form.get("note") or "").strip() or None
        month_key = spent_on.strftime("%Y-%m")

        if amount is None or amount <= 0 or not description:
            flash("Enter an amount and a short description.", "error")
            return redirect(url_for("index", tab="expenses", month=month_key))

        db.add_expense(
            conn,
            Expense(
                spent_on=spent_on,
                amount=amount,
                description=description,
                category=category,
                note=note,
                source=SOURCE_MANUAL,
            ),
        )
        flash(f"Added {format_inr(amount)} — {description}.", "success")
        return redirect(url_for("index", tab="expenses", month=month_key))

    @app.post("/expenses/<int:expense_id>/edit")
    def edit_expense(expense_id: int):
        conn = db.connect()
        db.init(conn)
        existing = db.get_expense(conn, expense_id)
        form = request.form
        amount = _parse_amount(form.get("amount"))
        spent_on = _parse_date(form.get("spent_on")) or (existing.spent_on if existing else date.today())
        description = (form.get("description") or "").strip()
        category = (form.get("category") or "").strip() or None
        note = (form.get("note") or "").strip() or None
        month_key = spent_on.strftime("%Y-%m")

        if existing is None:
            flash("That expense is gone.", "error")
            return redirect(url_for("index", tab="expenses", month=month_key))
        if amount is None or amount <= 0 or not description:
            flash("Enter an amount and a short description.", "error")
            return redirect(
                url_for("index", tab="expenses", month=existing.spent_on.strftime("%Y-%m"))
            )

        db.update_expense(
            conn,
            expense_id,
            spent_on=spent_on,
            amount=amount,
            description=description,
            category=category,
            note=note,
        )
        if category:
            remember_merchant_category(conn, description, category)
        flash(f"Updated {format_inr(amount)} — {description}.", "success")
        return redirect(url_for("index", tab="expenses", month=month_key))

    @app.post("/expenses/<int:expense_id>/delete")
    def delete_expense(expense_id: int):
        conn = db.connect()
        db.init(conn)
        existing = db.get_expense(conn, expense_id)
        month_key = (
            existing.spent_on.strftime("%Y-%m") if existing else date.today().strftime("%Y-%m")
        )
        if existing is None or not db.delete_expense(conn, expense_id):
            flash("That expense is gone.", "error")
        else:
            flash(f"Removed {existing.description}.", "success")
        return redirect(url_for("index", tab="expenses", month=month_key))

    @app.post("/expenses/fetch")
    def fetch_expenses():
        """No-JS fallback for expense alert ingest."""
        conn = db.connect()
        db.init(conn)
        if not gmail.is_connected():
            flash("Connect Gmail first so expense alerts can be fetched.", "error")
            return redirect(url_for("index", tab="expenses"))
        try:
            summary = expense_ingest.ingest_expense_alerts(conn, interactive=False)
        except gmail.GmailNotConfigured as exc:
            flash(str(exc), "error")
            return redirect(url_for("index", tab="expenses"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Expense alert ingest failed")
            flash(f"Could not fetch expense alerts: {exc}", "error")
            return redirect(url_for("index", tab="expenses"))

        flash(
            f"Added {summary.added} expense(s) from Gmail"
            + (f"; skipped {summary.skipped}." if summary.skipped else "."),
            "success",
        )
        return redirect(url_for("index", tab="expenses"))

    return app
