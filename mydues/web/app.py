"""Flask dashboard. Binds to localhost and holds no secrets of its own."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, time
from urllib.parse import urlencode

from flask import Flask, Response, flash, redirect, render_template, request, session, stream_with_context, url_for

from .. import config, db, dues, expense_ingest, gmail, ingest, issuers, planner
from ..audit import audit_cards
from ..categories import (
    CATEGORY_KEYWORDS,
    UNCATEGORIZED,
    apply_category_rules,
    category_spend_totals,
    merchant_key,
    remember_merchant_category,
    remember_payee_alias,
)
from ..charges import charge_label
from ..dues import format_inr
from ..models import CATEGORY_USER, SOURCE_GMAIL, SOURCE_MANUAL, Card, Expense, StatementRecord

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

# Stable muted dots for known categories (and Uncategorized).
CATEGORY_COLORS = {
    "Payment received": "cat-payment",
    "Fees & interest": "cat-fees",
    "EMI": "cat-emi",
    "Cash & transfers": "cat-cash",
    "Food & dining": "cat-food",
    "Groceries": "cat-groceries",
    "Travel": "cat-travel",
    "Fuel": "cat-fuel",
    "Bills & utilities": "cat-bills",
    "Entertainment": "cat-entertainment",
    "Health": "cat-health",
    "Shopping": "cat-shopping",
    UNCATEGORIZED: "cat-uncategorized",
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


def format_dmy_hm(value: date | datetime | None) -> str:
    """Date with time when a datetime is given: 05/08/2026 20:36."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    return value.strftime("%d/%m/%Y")


def _parse_time_hm(value: str | None) -> time | None:
    """Parse HH:MM or HH:MM:SS; blank → None."""
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def _combine_spent_at(spent_on: date, time_value: str | None) -> datetime | None:
    clock = _parse_time_hm(time_value)
    if clock is None:
        return None
    return datetime.combine(spent_on, clock)


def _flash_reapply_stats(stats: dict[str, int]) -> None:
    flash(
        f"Re-applied rules to {stats['expenses_updated']} expenses and "
        f"{stats['transactions_updated']} transactions.",
        "success",
    )


def _parse_amount(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


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


def _attention(conn, limit: int | None = None) -> list[dict]:
    """Attachments needing help, described well enough to act on.

    A statement from a card that was never registered cannot have its password
    worked out, so the mail it came in is the only thing left to go on.
    Loads the full backlog; the UI scrolls within the panel.
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


def _parse_lookback_days(value: str | None, *, default: int = 7) -> int:
    """Positive day count for Gmail expense search; clamps to 1..400."""
    try:
        days = int((value or "").strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(days, 400))


def _parse_lookback_months(value: str | None, *, default: int = 1) -> int:
    """Positive month count for statement Gmail search; clamps to 1..24."""
    try:
        months = int((value or "").strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(months, 24))


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


def _card_billing_window(month_start: date) -> tuple[date, date]:
    """Statement dates for a labeled month: 10th of month through 9th of next.

    Half-open [start, end): e.g. July → [10 Jul, 10 Aug).
    """
    start = month_start.replace(day=10)
    if month_start.month == 12:
        end = date(month_start.year + 1, 1, 10)
    else:
        end = date(month_start.year, month_start.month + 1, 10)
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


def _filter_args(args=None) -> dict:
    """Active list filters from the query string (blank → omitted)."""
    src = args if args is not None else request.args
    out: dict = {}
    q = (src.get("q") or "").strip()
    if q:
        out["q"] = q
    category = (src.get("category") or "").strip()
    if category:
        out["category"] = category
    min_amount = _parse_amount(src.get("min_amount"))
    if min_amount is not None:
        out["min_amount"] = min_amount
    max_amount = _parse_amount(src.get("max_amount"))
    if max_amount is not None:
        out["max_amount"] = max_amount
    if (src.get("charges") or "").strip().lower() in {"1", "true", "yes", "on"}:
        out["charges_only"] = True
    return out


def _filter_query(**extra) -> str:
    """Query string preserving month/tab/filters plus extras (e.g. highlight)."""
    params: dict = {}
    month = (request.args.get("month") or request.form.get("month") or "").strip()
    if month:
        params["month"] = month
    tab = (request.args.get("tab") or request.form.get("tab") or "").strip()
    if tab:
        params["tab"] = tab
    statement = request.args.get("statement") or request.form.get("statement")
    if statement:
        params["statement"] = statement
    txns = (request.args.get("txns") or request.form.get("txns") or "").strip()
    if txns:
        params["txns"] = txns
    for key, value in _filter_args().items():
        if key == "charges_only":
            params["charges"] = "1"
        elif key in {"min_amount", "max_amount"}:
            params[key] = f"{value:g}"
        else:
            params[key] = value
    for key, value in extra.items():
        if value is None or value == "":
            continue
        params[key] = value
    return urlencode(params)


def _coverage_summary(conn) -> dict:
    """Portfolio strip + per-card rows from audit_cards + ingest backlog."""
    findings = audit_cards(conn)
    by_card: dict[int, list] = {}
    pending_ingest = 0
    for finding in findings:
        if finding.kind == "pending_ingest":
            pending_ingest = db.count_pending_ingest(conn)
            continue
        by_card.setdefault(finding.card_id, []).append(finding)

    cards = []
    ok = stale = missing_txns = 0
    for view in dues.all_views(conn):
        card_id = view.card.id or 0
        card_findings = by_card.get(card_id, [])
        has_missing = any(f.kind in {"transactions_missing", "parse_note"} for f in card_findings)
        if view.is_stale:
            stale += 1
        if has_missing:
            missing_txns += 1
        if view.has_data and not view.is_stale and not has_missing:
            ok += 1
        cards.append(
            {
                "card_id": card_id,
                "label": view.card.label,
                "last4": view.card.last4,
                "statement_date": view.statement_date,
                "is_stale": view.is_stale,
                "confidence": view.record.confidence if view.record else None,
                "transactions_missing": view.transactions_missing
                or any(f.kind == "parse_note" for f in card_findings),
                "findings": card_findings,
                "note": view.record.note if view.record else None,
            }
        )
    return {
        "cards": cards,
        "ok": ok,
        "stale": stale,
        "pending_unlock": pending_ingest,
        "missing_txns": missing_txns,
        "findings": findings,
    }


def _due_urgency(book, *, today: date | None = None) -> dict:
    """Counts for overdue / due in 3 days / due this week."""
    today = today or date.today()
    overdue = []
    due_3 = []
    due_week = []
    for view in book.views:
        if view.outstanding is not None and view.outstanding <= 0:
            continue
        if view.due_date is None:
            continue
        days = (view.due_date - today).days
        entry = {"card_id": view.card.id, "label": view.card.label, "due_date": view.due_date}
        if days < 0:
            overdue.append(entry)
        elif days <= 3:
            due_3.append(entry)
        if 0 <= days <= 7:
            due_week.append(entry)
    return {"overdue": overdue, "due_3": due_3, "due_week": due_week}


def _format_fetch_stamp(value: str | None) -> str:
    if not value:
        return "Never"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    return stamp.strftime("%d/%m/%Y %H:%M")


def _expense_export_rows(expenses: list[Expense]) -> list[dict]:
    rows = []
    for expense in expenses:
        when = expense.spent_at or expense.spent_on
        rows.append(
            {
                "id": expense.id,
                "spent_on": expense.spent_on.isoformat() if expense.spent_on else None,
                "spent_on_dmy": format_dmy_hm(when),
                "spent_at": (
                    expense.spent_at.isoformat(timespec="seconds")
                    if expense.spent_at
                    else None
                ),
                "amount": expense.amount,
                "description": expense.description,
                "category": expense.category,
                "source": expense.source,
                "note": expense.note,
            }
        )
    return rows


def _txn_export_rows(txns) -> list[dict]:
    rows = []
    for txn in txns:
        rows.append(
            {
                "id": txn.id,
                "txn_date": txn.txn_date.isoformat() if txn.txn_date else None,
                "txn_date_dmy": format_dmy(txn.txn_date),
                "description": txn.description,
                "amount": txn.amount,
                "kind": txn.kind,
                "category": txn.category,
                "charge_kind": txn.charge_kind,
            }
        )
    return rows


def _csv_response(filename: str, fieldnames: list[str], rows: list[dict]) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(filename: str, payload) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def create_app() -> Flask:
    app = Flask(__name__)
    # Signs flash messages and the OAuth state in a single-user local session.
    app.secret_key = config.session_secret()

    app.jinja_env.filters["inr"] = format_inr
    app.jinja_env.filters["dmy"] = format_dmy
    app.jinja_env.filters["dmy_hm"] = format_dmy_hm

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
        if tab not in {"cards", "expenses", "categories", "planner"}:
            tab = "cards"

        # ?statement=<id> looks back at one earlier cycle; the rest stay latest.
        chosen = db.get_statement(conn, request.args.get("statement", type=int) or 0)
        book = dues.portfolio(conn, selected={chosen.card_id: chosen.id} if chosen else None)

        month_start = _parse_month(request.args.get("month"))
        month_end_exclusive = _month_window(month_start)[1]
        card_start, card_end = _card_billing_window(month_start)
        filters = _filter_args()
        expenses = db.list_expenses(
            conn, start=month_start, end=month_end_exclusive, **filters
        )
        # Totals ignore list filters so the month summary stays stable.
        all_month_expenses = db.list_expenses(
            conn, start=month_start, end=month_end_exclusive
        )
        expense_total = db.expense_total(
            conn, start=month_start, end=month_end_exclusive
        )
        card_spend_total = db.card_spend_total(
            conn, start=card_start, end=card_end
        )
        card_spend_by_card = db.card_spend_by_card(
            conn, start=card_start, end=card_end
        )
        budgets = db.budget_status_for_month(
            conn, start=month_start, end=month_end_exclusive
        )

        for view in book.views:
            view.charges_cycle_total = view.charges_this_cycle

        # Apply the same filters to each card's open statement table.
        if filters:
            for view in book.views:
                if view.record and view.record.id:
                    view.transactions = db.transactions_for_statement(
                        conn, view.record.id, **filters
                    )

        highlight = (request.args.get("highlight") or "").strip()
        txns_flag = (request.args.get("txns") or "").strip().lower()
        # Global open: filters or ?txns=1. Older-cycle open is per-card in the template
        # (not view.is_latest) so one statement select does not expand every card.
        txns_open = bool(filters) or txns_flag in {"1", "true", "yes"}

        # Attach last annual fee / last any charge for card tiles.
        dues.attach_last_charges(conn, book.views)

        plan_month = month_start.strftime("%Y-%m")
        planner_view = planner.build_planner(conn, plan_month)

        return render_template(
            "index.html",
            book=book,
            tab=tab,
            planner=planner_view,
            planner_prev_month=planner.shift_month_key(plan_month, -1),
            planner_next_month=planner.shift_month_key(plan_month, 1),
            planner_default_credit=planner.prior_month_end(plan_month),
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
            expense_category_totals=category_spend_totals(all_month_expenses),
            expense_month=month_start,
            expense_month_key=month_start.strftime("%Y-%m"),
            expense_month_label=month_start.strftime("%B %Y"),
            expense_prev_month=_shift_month(month_start, -1).strftime("%Y-%m"),
            expense_next_month=_shift_month(month_start, 1).strftime("%Y-%m"),
            expense_months=db.expense_months(conn),
            card_spend_total=card_spend_total,
            card_spend_by_card=card_spend_by_card,
            card_category_totals=category_spend_totals(
                db.list_transactions_in_range(conn, start=card_start, end=card_end)
            ),
            category_phrases=db.list_category_phrases(conn),
            merchant_category_rows=db.list_merchant_categories(conn),
            payee_cues=db.list_payee_cues(conn),
            payee_aliases=db.list_payee_aliases(conn),
            known_categories=list(CATEGORY_KEYWORDS.keys()),
            budgets=budgets,
            filters=filters,
            filter_q=filters.get("q", ""),
            filter_category=filters.get("category", ""),
            filter_min=filters.get("min_amount"),
            filter_max=filters.get("max_amount"),
            coverage=_coverage_summary(conn),
            due_urgency=_due_urgency(book),
            category_colors=CATEGORY_COLORS,
            charge_label=charge_label,
            highlight=highlight,
            txns_open=txns_open,
            statements_fetched=_format_fetch_stamp(
                db.get_meta(conn, db.META_STATEMENTS_FETCHED)
            ),
            expenses_fetched=_format_fetch_stamp(
                db.get_meta(conn, db.META_EXPENSES_FETCHED)
            ),
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
        lookback_months = _parse_lookback_months(request.form.get("months"))
        try:
            summary = ingest.ingest_gmail(
                conn, interactive=False, lookback_months=lookback_months
            )
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
        message += (
            f" (last {lookback_months} month"
            + ("s" if lookback_months != 1 else "")
            + ")"
        )
        flash(message, "success")
        db.touch_meta_now(conn, db.META_STATEMENTS_FETCHED)
        return redirect(url_for("index"))

    @app.get("/ingest/stream")
    def ingest_stream():
        """Stream per-item progress for Fetch / Re-parse / Try again / Expenses."""
        job = (request.args.get("job") or "").strip()
        if job not in {"fetch", "reparse", "reprocess", "expenses"}:
            return Response("Unknown job", status=400)
        # Capture before the background thread — request context is gone there.
        expense_lookback_days = _parse_lookback_days(request.args.get("days"))
        statement_lookback_months = _parse_lookback_months(request.args.get("months"))

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
                            conn,
                            interactive=False,
                            lookback_months=statement_lookback_months,
                            on_progress=on_progress,
                        )
                        message = f"Parsed {summary.parsed} statement(s)."
                        if summary.needs_attention:
                            message += (
                                f" {len(summary.needs_attention)} attachment(s) need attention."
                            )
                        message += (
                            f" (last {statement_lookback_months} month"
                            + ("s" if statement_lookback_months != 1 else "")
                            + ")"
                        )
                        done_total = len(summary.results)
                        done_parsed = summary.parsed
                        done_remaining = summary.remaining
                        db.touch_meta_now(conn, db.META_STATEMENTS_FETCHED)
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
                            conn,
                            interactive=False,
                            lookback_days=expense_lookback_days,
                            on_progress=on_progress,
                        )
                        message = (
                            f"Added {summary.added} expense(s) from Gmail"
                            + (f"; skipped {summary.skipped}." if summary.skipped else ".")
                            + f" (last {expense_lookback_days} day"
                            + ("s" if expense_lookback_days != 1 else "")
                            + ")"
                        )
                        done_total = len(summary.results)
                        done_parsed = summary.added
                        done_remaining = 0
                        db.touch_meta_now(conn, db.META_EXPENSES_FETCHED)
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

    @app.post("/category-phrases")
    def add_category_phrase():
        conn = db.connect()
        db.init(conn)
        phrase = (request.form.get("phrase") or "").strip()
        category = (request.form.get("category") or "").strip()
        if not phrase or not category:
            flash("Enter a phrase and a category.", "error")
            return redirect(url_for("index", tab="categories"))
        if db.upsert_category_phrase(conn, phrase, category) is None:
            flash("Enter a phrase and a category.", "error")
        else:
            flash(f"Phrase “{phrase}” → {category}.", "success")
            _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))

    @app.post("/category-phrases/<int:phrase_id>/edit")
    def edit_category_phrase(phrase_id: int):
        conn = db.connect()
        db.init(conn)
        phrase = (request.form.get("phrase") or "").strip()
        category = (request.form.get("category") or "").strip()
        if not phrase or not category:
            flash("Enter a phrase and a category.", "error")
            return redirect(url_for("index", tab="categories"))
        if not db.update_category_phrase(conn, phrase_id, phrase=phrase, category=category):
            flash("Could not update that phrase (gone or duplicate).", "error")
        else:
            flash(f"Updated phrase “{phrase}” → {category}.", "success")
            _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))

    @app.post("/category-phrases/<int:phrase_id>/delete")
    def delete_category_phrase(phrase_id: int):
        conn = db.connect()
        db.init(conn)
        if not db.delete_category_phrase(conn, phrase_id):
            flash("That phrase is gone.", "error")
        else:
            flash("Removed phrase rule.", "success")
            _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))

    @app.post("/merchant-categories")
    def add_merchant_category():
        conn = db.connect()
        db.init(conn)
        merchant = (request.form.get("merchant") or "").strip()
        category = (request.form.get("category") or "").strip()
        key = merchant_key(merchant)
        if not key or not category:
            flash("Enter a merchant and a category.", "error")
            return redirect(url_for("index", tab="categories"))
        db.upsert_merchant_category_key(conn, key, category)
        flash(f"Merchant “{key}” → {category}.", "success")
        _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))

    @app.post("/merchant-categories/<path:key>/edit")
    def edit_merchant_category(key: str):
        conn = db.connect()
        db.init(conn)
        merchant = (request.form.get("merchant") or "").strip()
        category = (request.form.get("category") or "").strip()
        if not merchant or not category:
            flash("Enter a merchant and a category.", "error")
            return redirect(url_for("index", tab="categories"))
        new_key = merchant_key(merchant)
        if not new_key:
            flash("Enter a merchant and a category.", "error")
            return redirect(url_for("index", tab="categories"))
        if not db.update_merchant_category(
            conn, key, category, new_key=new_key
        ):
            flash("That merchant mapping is gone.", "error")
        else:
            flash(f"Updated “{new_key}” → {category}.", "success")
            _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))

    @app.post("/merchant-categories/<path:key>/delete")
    def delete_merchant_category(key: str):
        conn = db.connect()
        db.init(conn)
        if not db.delete_merchant_category(conn, key):
            flash("That merchant mapping is gone.", "error")
        else:
            flash(f"Removed “{key}”.", "success")
            _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))

    @app.post("/payee-cues")
    def add_payee_cue():
        conn = db.connect()
        db.init(conn)
        cue = (request.form.get("cue") or "").strip()
        if not cue:
            flash("Enter a payee cue.", "error")
            return redirect(url_for("index", tab="categories"))
        if db.upsert_payee_cue(conn, cue) is None:
            flash("Enter a payee cue.", "error")
        else:
            flash(f"Payee cue “{cue}” saved.", "success")
        return redirect(url_for("index", tab="categories"))

    @app.post("/payee-cues/<int:cue_id>/edit")
    def edit_payee_cue(cue_id: int):
        conn = db.connect()
        db.init(conn)
        cue = (request.form.get("cue") or "").strip()
        if not cue:
            flash("Enter a payee cue.", "error")
            return redirect(url_for("index", tab="categories"))
        if not db.update_payee_cue(conn, cue_id, cue=cue):
            flash("Could not update that cue (gone or duplicate).", "error")
        else:
            flash(f"Updated payee cue “{cue}”.", "success")
        return redirect(url_for("index", tab="categories"))

    @app.post("/payee-cues/<int:cue_id>/delete")
    def delete_payee_cue(cue_id: int):
        conn = db.connect()
        db.init(conn)
        if not db.delete_payee_cue(conn, cue_id):
            flash("That cue is gone.", "error")
        else:
            flash("Removed payee cue.", "success")
        return redirect(url_for("index", tab="categories"))

    @app.post("/payee-aliases")
    def add_payee_alias():
        conn = db.connect()
        db.init(conn)
        raw = (request.form.get("raw") or "").strip()
        payee = (request.form.get("payee") or "").strip()
        key = merchant_key(raw)
        if not key or not payee:
            flash("Enter a raw payee and a display name.", "error")
            return redirect(url_for("index", tab="categories"))
        db.upsert_payee_alias(conn, key, payee)
        flash(f"Payee “{key}” → {payee}.", "success")
        return redirect(url_for("index", tab="categories"))

    @app.post("/payee-aliases/<path:key>/edit")
    def edit_payee_alias(key: str):
        conn = db.connect()
        db.init(conn)
        raw = (request.form.get("raw") or "").strip()
        payee = (request.form.get("payee") or "").strip()
        if not raw or not payee:
            flash("Enter a raw payee and a display name.", "error")
            return redirect(url_for("index", tab="categories"))
        new_key = merchant_key(raw)
        if not new_key:
            flash("Enter a raw payee and a display name.", "error")
            return redirect(url_for("index", tab="categories"))
        if not db.update_payee_alias(conn, key, payee, new_key=new_key):
            flash("That payee mapping is gone.", "error")
        else:
            flash(f"Updated “{new_key}” → {payee}.", "success")
        return redirect(url_for("index", tab="categories"))

    @app.post("/payee-aliases/<path:key>/delete")
    def delete_payee_alias(key: str):
        conn = db.connect()
        db.init(conn)
        if not db.delete_payee_alias(conn, key):
            flash("That payee mapping is gone.", "error")
        else:
            flash(f"Removed “{key}”.", "success")
        return redirect(url_for("index", tab="categories"))

    @app.post("/categories/reapply")
    def reapply_categories():
        conn = db.connect()
        db.init(conn)
        _flash_reapply_stats(apply_category_rules(conn))
        return redirect(url_for("index", tab="categories"))
    @app.post("/expenses")
    def add_expense():
        conn = db.connect()
        db.init(conn)
        form = request.form
        amount = _parse_amount(form.get("amount"))
        spent_on = _parse_date(form.get("spent_on")) or date.today()
        spent_at = _combine_spent_at(spent_on, form.get("spent_at"))
        description = (form.get("description") or "").strip()
        category = (form.get("category") or "").strip() or None
        note = (form.get("note") or "").strip() or None
        month_key = spent_on.strftime("%Y-%m")

        if amount is None or amount <= 0 or not description:
            flash("Enter an amount and a short description.", "error")
            return redirect(url_for("index", tab="expenses", month=month_key))
        if (form.get("spent_at") or "").strip() and spent_at is None:
            flash("Time must be HH:MM (optional seconds).", "error")
            return redirect(url_for("index", tab="expenses", month=month_key))

        db.add_expense(
            conn,
            Expense(
                spent_on=spent_on,
                amount=amount,
                description=description,
                category=category,
                category_source=CATEGORY_USER if category else None,
                note=note,
                spent_at=spent_at,
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
        spent_at = _combine_spent_at(spent_on, form.get("spent_at"))
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
        if (form.get("spent_at") or "").strip() and spent_at is None:
            flash("Time must be HH:MM (optional seconds).", "error")
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
            category_source=CATEGORY_USER if category else None,
            note=note,
            spent_at=spent_at,
        )
        if existing.description and description != existing.description:
            remember_payee_alias(conn, existing.description, description)
        if category:
            remember_merchant_category(conn, description, category)
        flash(f"Updated {format_inr(amount)} — {description}.", "success")
        return redirect(
            url_for(
                "index",
                tab="expenses",
                month=month_key,
                highlight=f"expense-{expense_id}",
            )
        )

    @app.post("/transactions/<int:txn_id>/category")
    def edit_transaction_category(txn_id: int):
        conn = db.connect()
        db.init(conn)
        category = (request.form.get("category") or "").strip() or None
        updated = db.update_transaction_category(conn, txn_id, category)
        if updated is None:
            flash("That transaction is gone.", "error")
            return redirect(url_for("index"))

        txn, card_id = updated
        if category:
            remember_merchant_category(conn, txn.description, category)

        statement_id = request.form.get("statement", type=int)
        target = url_for("index", statement=statement_id) if statement_id else url_for("index")
        return redirect(f"{target}#card-{card_id}")

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

    @app.get("/expenses/<int:expense_id>/email")
    def expense_email(expense_id: int):
        """Show the Gmail alert behind a gmail-sourced expense."""
        conn = db.connect()
        db.init(conn)
        expense = db.get_expense(conn, expense_id)
        month_key = (
            expense.spent_on.strftime("%Y-%m") if expense else date.today().strftime("%Y-%m")
        )
        back = url_for("index", tab="expenses", month=month_key)

        if expense is None:
            flash("That expense is gone.", "error")
            return redirect(back)
        if expense.source != SOURCE_GMAIL or not expense.source_ref:
            flash("This expense was not imported from Gmail.", "error")
            return redirect(back)
        if not gmail.is_connected():
            flash("Connect Gmail to view the original email.", "error")
            return redirect(back)

        try:
            service = gmail.service(interactive=False)
            message = gmail.get_message(service, expense.source_ref)
        except gmail.GmailNotConfigured as exc:
            flash(str(exc), "error")
            return redirect(back)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load expense email %s", expense.source_ref)
            flash(f"Could not load that email: {exc}", "error")
            return redirect(back)

        body = expense_ingest._strip_html(message.body or "")
        return render_template(
            "expense_email.html",
            expense=expense,
            subject=message.subject or "(no subject)",
            sender=message.sender or "",
            received=message.received_at,
            body=body,
            back_url=back,
            today=date.today(),
            gmail_connected=True,
            can_teach=True,
        )

    @app.post("/expenses/<int:expense_id>/teach")
    def teach_expense(expense_id: int):
        """Save a payee cue/alias from the email view, or reparse this message."""
        conn = db.connect()
        db.init(conn)
        expense = db.get_expense(conn, expense_id)
        month_key = (
            expense.spent_on.strftime("%Y-%m") if expense else date.today().strftime("%Y-%m")
        )
        back_email = url_for("expense_email", expense_id=expense_id)
        back_list = url_for(
            "index",
            tab="expenses",
            month=month_key,
            highlight=f"expense-{expense_id}",
        )
        if expense is None:
            flash("That expense is gone.", "error")
            return redirect(url_for("index", tab="expenses", month=month_key))

        action = (request.form.get("action") or "").strip().lower()
        if action == "cue":
            cue = (request.form.get("cue") or "").strip()
            if db.upsert_payee_cue(conn, cue) is None:
                flash("Enter a payee cue.", "error")
            else:
                flash(f"Payee cue “{cue}” saved.", "success")
            return redirect(back_email)

        if action == "alias":
            raw = (request.form.get("raw") or expense.description or "").strip()
            payee = (request.form.get("payee") or "").strip()
            key = merchant_key(raw)
            if not key or not payee:
                flash("Enter a raw payee and a display name.", "error")
                return redirect(back_email)
            db.upsert_payee_alias(conn, key, payee)
            flash(f"Payee “{key}” → {payee}.", "success")
            return redirect(back_email)

        if action == "reparse":
            if expense.source != SOURCE_GMAIL or not expense.source_ref:
                flash("This expense has no Gmail message to reparse.", "error")
                return redirect(back_email)
            if not gmail.is_connected():
                flash("Connect Gmail to reparse this message.", "error")
                return redirect(back_email)
            try:
                changed = expense_ingest.refresh_expense_from_gmail(
                    conn, expense, interactive=False
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Teach reparse failed for expense %s", expense_id)
                flash(f"Could not reparse that email: {exc}", "error")
                return redirect(back_email)
            if changed:
                flash("Reparsed this message and updated the expense.", "success")
            else:
                flash("Reparsed this message; nothing changed.", "success")
            return redirect(back_list)

        flash("Unknown teach action.", "error")
        return redirect(back_email)

    @app.post("/category-budgets")
    def upsert_budget():
        conn = db.connect()
        db.init(conn)
        category = (request.form.get("category") or "").strip()
        amount = _parse_amount(request.form.get("amount_limit"))
        month_key = (request.form.get("month") or date.today().strftime("%Y-%m")).strip()
        if not category or amount is None or amount <= 0:
            flash("Enter a category and a positive monthly limit.", "error")
            return redirect(url_for("index", tab="expenses", month=month_key))
        db.upsert_category_budget(conn, category, amount)
        flash(f"Budget for {category}: {format_inr(amount)} / month.", "success")
        return redirect(
            url_for("index", tab="expenses", month=month_key, highlight="budgets")
        )

    @app.post("/category-budgets/delete")
    def delete_budget():
        conn = db.connect()
        db.init(conn)
        category = (request.form.get("category") or "").strip()
        month_key = (request.form.get("month") or date.today().strftime("%Y-%m")).strip()
        if not category or not db.delete_category_budget(conn, category):
            flash("That budget is gone.", "error")
        else:
            flash(f"Removed budget for {category}.", "success")
        return redirect(url_for("index", tab="expenses", month=month_key))

    def _planner_month() -> str:
        return (request.form.get("month") or date.today().strftime("%Y-%m")).strip()

    def _planner_redirect(month_key: str | None = None):
        return redirect(
            url_for("index", tab="planner", month=month_key or _planner_month())
        )

    @app.post("/planner/income")
    def planner_save_income():
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        amount = _parse_amount(request.form.get("amount"))
        if amount is None or amount < 0:
            flash("Enter a valid salary amount.", "error")
            return _planner_redirect(month_key)
        credited_raw = (request.form.get("credited_on") or "").strip()
        credited_on = None
        if credited_raw:
            try:
                credited_on = date.fromisoformat(credited_raw)
            except ValueError:
                flash("Credited-on date must be YYYY-MM-DD.", "error")
                return _planner_redirect(month_key)
        else:
            credited_on = planner.prior_month_end(month_key)
        note = (request.form.get("note") or "").strip() or None
        db.upsert_monthly_income(
            conn,
            month=month_key,
            amount=amount,
            credited_on=credited_on,
            note=note,
        )
        flash(f"Salary for {month_key}: {format_inr(amount)}.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/income/copy")
    def planner_copy_income():
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        from_month = planner.shift_month_key(month_key, -1)
        if not db.copy_monthly_income(conn, from_month=from_month, to_month=month_key):
            flash(f"No salary saved for {from_month} to copy.", "error")
        else:
            flash(f"Copied salary from {from_month}.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/recurring")
    def planner_add_recurring():
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        label = (request.form.get("label") or "").strip()
        amount = _parse_amount(request.form.get("amount"))
        day_raw = (request.form.get("day_of_month") or "").strip()
        day = int(day_raw) if day_raw.isdigit() else None
        if not label or amount is None or amount < 0:
            flash("Enter a label and amount for the recurring expense.", "error")
            return _planner_redirect(month_key)
        if db.add_recurring_expense(
            conn, label=label, amount=amount, day_of_month=day
        ) is None:
            flash("Could not add that recurring expense.", "error")
        else:
            flash(f"Added recurring “{label}”.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/recurring/defaults")
    def planner_seed_defaults():
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        added = planner.seed_default_recurrings(conn)
        if added:
            flash(f"Added {added} default recurring item(s). Set the amounts.", "success")
        else:
            flash("Default recurrings already present.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/recurring/<int:recurring_id>/edit")
    def planner_edit_recurring(recurring_id: int):
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        label = (request.form.get("label") or "").strip()
        amount = _parse_amount(request.form.get("amount"))
        day_raw = (request.form.get("day_of_month") or "").strip()
        day = int(day_raw) if day_raw.isdigit() else None
        active = request.form.get("active") == "1"
        if amount is None or amount < 0:
            flash("Enter a valid amount for the recurring expense.", "error")
            return _planner_redirect(month_key)
        if not db.update_recurring_expense(
            conn,
            recurring_id,
            label=label or None,
            amount=amount,
            day_of_month=day,
            active=active,
        ):
            flash("Could not update that recurring.", "error")
        else:
            flash("Updated recurring expense.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/recurring/<int:recurring_id>/delete")
    def planner_delete_recurring(recurring_id: int):
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        if not db.delete_recurring_expense(conn, recurring_id):
            flash("That recurring is gone.", "error")
        else:
            flash("Removed recurring expense.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/recurring/<int:recurring_id>/paid")
    def planner_toggle_paid(recurring_id: int):
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        paid = request.form.get("paid") == "1"
        if not db.set_recurring_paid(conn, recurring_id, month_key, paid=paid):
            flash("Could not update paid status.", "error")
        else:
            flash("Marked paid." if paid else "Cleared paid mark.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/planned")
    def planner_add_planned():
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        label = (request.form.get("label") or "").strip()
        amount = _parse_amount(request.form.get("amount"))
        if db.add_planned_expense(
            conn, month=month_key, label=label, amount=amount if amount is not None else -1
        ) is None:
            flash("Enter a label and amount for the one-off plan.", "error")
        else:
            flash(f"Added planned “{label}”.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/planned/<int:planned_id>/delete")
    def planner_delete_planned(planned_id: int):
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        if not db.delete_planned_expense(conn, planned_id):
            flash("That planned item is gone.", "error")
        else:
            flash("Removed planned expense.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/goals")
    def planner_add_goal():
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        label = (request.form.get("label") or "").strip()
        target = _parse_amount(request.form.get("target_amount"))
        if db.add_savings_goal(conn, label=label, target_amount=target or 0) is None:
            flash("Enter a goal label and a positive target.", "error")
        else:
            flash(f"Added savings goal “{label}”.", "success")
        return _planner_redirect(month_key)

    @app.post("/planner/goals/<int:goal_id>/delete")
    def planner_delete_goal(goal_id: int):
        conn = db.connect()
        db.init(conn)
        month_key = _planner_month()
        if not db.delete_savings_goal(conn, goal_id):
            flash("That goal is gone.", "error")
        else:
            flash("Removed savings goal.", "success")
        return _planner_redirect(month_key)

    @app.get("/export/expenses.csv")
    def export_expenses_csv():
        conn = db.connect()
        db.init(conn)
        month_start = _parse_month(request.args.get("month"))
        start, end = _month_window(month_start)
        expenses = db.list_expenses(conn, start=start, end=end, **_filter_args())
        fieldnames = [
            "id",
            "spent_on",
            "amount",
            "description",
            "category",
            "source",
            "note",
        ]
        csv_rows = [
            {
                "id": expense.id,
                "spent_on": format_dmy_hm(expense.spent_at or expense.spent_on),
                "amount": expense.amount,
                "description": expense.description,
                "category": expense.category or "",
                "source": expense.source,
                "note": expense.note or "",
            }
            for expense in expenses
        ]
        return _csv_response(
            f"expenses-{month_start.strftime('%Y-%m')}.csv", fieldnames, csv_rows
        )

    @app.get("/export/expenses.json")
    def export_expenses_json():
        conn = db.connect()
        db.init(conn)
        month_start = _parse_month(request.args.get("month"))
        start, end = _month_window(month_start)
        expenses = db.list_expenses(conn, start=start, end=end, **_filter_args())
        payload = {
            "month": month_start.strftime("%Y-%m"),
            "expenses": _expense_export_rows(expenses),
        }
        return _json_response(
            f"expenses-{month_start.strftime('%Y-%m')}.json", payload
        )

    @app.get("/export/statement/<int:statement_id>/transactions.csv")
    def export_statement_txns_csv(statement_id: int):
        conn = db.connect()
        db.init(conn)
        record = db.get_statement(conn, statement_id)
        if record is None:
            return Response("Statement not found", status=404)
        txns = db.transactions_for_statement(conn, statement_id, **_filter_args())
        fieldnames = [
            "id",
            "txn_date",
            "description",
            "amount",
            "kind",
            "category",
            "charge_kind",
        ]
        csv_rows = [
            {
                "id": txn.id,
                "txn_date": format_dmy(txn.txn_date),
                "description": txn.description,
                "amount": txn.amount,
                "kind": txn.kind,
                "category": txn.category or "",
                "charge_kind": txn.charge_kind or "",
            }
            for txn in txns
        ]
        return _csv_response(
            f"statement-{statement_id}-transactions.csv", fieldnames, csv_rows
        )

    @app.get("/export/statement/<int:statement_id>/transactions.json")
    def export_statement_txns_json(statement_id: int):
        conn = db.connect()
        db.init(conn)
        record = db.get_statement(conn, statement_id)
        if record is None:
            return Response("Statement not found", status=404)
        txns = db.transactions_for_statement(conn, statement_id, **_filter_args())
        payload = {
            "statement_id": statement_id,
            "transactions": _txn_export_rows(txns),
        }
        return _json_response(
            f"statement-{statement_id}-transactions.json", payload
        )

    @app.post("/expenses/refresh")
    def refresh_expenses():
        """Re-fetch Gmail alerts to fix boilerplate descriptions / categories."""
        conn = db.connect()
        db.init(conn)
        if not gmail.is_connected():
            flash("Connect Gmail first to refresh expense descriptions.", "error")
            return redirect(url_for("index", tab="expenses"))
        try:
            summary = expense_ingest.refresh_gmail_expenses(conn, interactive=False)
        except gmail.GmailNotConfigured as exc:
            flash(str(exc), "error")
            return redirect(url_for("index", tab="expenses"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Expense refresh failed")
            flash(f"Could not refresh expenses: {exc}", "error")
            return redirect(url_for("index", tab="expenses"))

        flash(
            f"Refreshed {summary.updated} expense(s) from Gmail"
            + (f"; skipped {summary.skipped}" if summary.skipped else "")
            + (f"; failed {summary.failed}" if summary.failed else "")
            + ".",
            "success" if summary.updated or not summary.failed else "warning",
        )
        return redirect(url_for("index", tab="expenses"))

    @app.post("/expenses/fetch")
    def fetch_expenses():
        """No-JS fallback for expense alert ingest."""
        conn = db.connect()
        db.init(conn)
        if not gmail.is_connected():
            flash("Connect Gmail first so expense alerts can be fetched.", "error")
            return redirect(url_for("index", tab="expenses"))
        lookback_days = _parse_lookback_days(request.form.get("days"))
        try:
            summary = expense_ingest.ingest_expense_alerts(
                conn, interactive=False, lookback_days=lookback_days
            )
        except gmail.GmailNotConfigured as exc:
            flash(str(exc), "error")
            return redirect(url_for("index", tab="expenses"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Expense alert ingest failed")
            flash(f"Could not fetch expense alerts: {exc}", "error")
            return redirect(url_for("index", tab="expenses"))

        db.touch_meta_now(conn, db.META_EXPENSES_FETCHED)
        flash(
            f"Added {summary.added} expense(s) from Gmail"
            + (f"; skipped {summary.skipped}." if summary.skipped else ".")
            + f" (last {lookback_days} day{'s' if lookback_days != 1 else ''})",
            "success",
        )
        return redirect(url_for("index", tab="expenses"))

    return app
