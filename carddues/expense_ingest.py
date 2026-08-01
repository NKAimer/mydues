"""Fetch and parse bank / UPI spend-alert emails into the expenses ledger.

Separate from statement-PDF ingest so dues Fetch never double-counts alert mail.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape

from . import db, gmail
from .categories import resolve_category
from .ingest import ProgressCallback, ProgressEvent
from .models import SOURCE_GMAIL, Expense
from .text import parse_amount, parse_date

logger = logging.getLogger(__name__)


def _emit(on_progress: ProgressCallback | None, event: ProgressEvent) -> None:
    if on_progress is None:
        return
    try:
        on_progress(event)
    except Exception:  # noqa: BLE001
        logger.exception("Progress callback failed")


# Subject tokens that look like a real spend alert (not promos / KYC / offers).
ALERT_SUBJECT_HINTS = (
    "transaction alert",
    "txn alert",
    "spent",
    "debited",
    "debit alert",
    "card transaction",
    "transaction of rs",
    "transaction of inr",
    "purchase",
    # HDFC InstaAlerts (no "transaction alert" / "debited" in the subject).
    "payment was made using your credit card",
    # UPI spend alerts (PhonePe / GPay / bank UPI notifications).
    "upi",
    "upi alert",
    "upi transaction",
    "upi payment",
    "upi txn",
    "paid via upi",
    "sent using upi",
    "sent via upi",
)

# Full statement subjects — never treat these as expenses.
STATEMENT_SUBJECT_SKIP = (
    "credit card statement",
    "e-statement",
    "estatement",
    "monthly statement",
    "account statement",
    "card statement",
)

# Loan / EMI / limit-enhancement promos that share bank senders with alerts.
LOAN_OFFER_SUBJECT_SKIP = (
    "loan",
    "personal loan",
    "pre-approved",
    "pre approved",
    "loan offer",
    "emi offer",
    "credit limit enhancement",
)

_LOAN_OFFER_RE = re.compile(
    r"(?i)\b(?:"
    r"personal\s+loan"
    r"|loan\s+offer"
    r"|pre[-\s]?approved"
    r"|get\s+a\s+loan"
    r"|apply\s+now"
    r"|emi\s+offer"
    r"|limited\s+period\s+offer"
    r"|credit\s+limit\s+enhancement"
    r"|instant\s+loan"
    r"|cash\s+loan"
    r")\b"
)

_INR = re.compile(
    r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
# Capture merchant after common alert lead-ins; stop before dates / card boilerplate.
_MERCHANT_PATTERNS = (
    # SBI / PhonePe: "Rs.X spent on your SBI Credit Card ending with 3418 at MERCHANT on DATE"
    re.compile(
        r"(?i)spent\s+on\s+your\s+.+?\s+at\s+"
        r"([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80}?)"
        r"(?=\s+on\s+\d|\s+via\s+|\s+using\s+|\s+ref(?:erence)?\b|[.,]|$)"
    ),
    re.compile(
        r"(?i)(?:purchase\s+transaction(?:\s+of\s+(?:rs\.?|inr|₹)\s*[0-9,]+\.?\d*)?)"
        r"\s+(?:at|with|towards|to)\s+"
        r"([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80})"
    ),
    re.compile(
        r"(?i)(?:used\s+your\s+.+?\s+card\s+ending\s+with\s+\d+\s+for\s+a\s+)?"
        r"purchase\s+(?:txn|transaction)\s+(?:at|with|towards|to)\s+"
        r"([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80})"
    ),
    re.compile(
        r"(?i)(?:spent\s+at|debited\s+(?:for|at|to)|paid\s+to|towards|"
        r"info\s*:|merchant\s*:)\s+([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80})"
    ),
    re.compile(
        r"(?i)\bUPI[_/]+([A-Za-z][A-Za-z0-9 &.'@/_-]{1,60})"
    ),
    # Avoid "to inform you that…" from SBI boilerplate.
    re.compile(
        r"(?i)(?<![A-Za-z])(?:at|to(?!\s+inform))\s+"
        r"([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80})"
    ),
)
_VPA_RE = re.compile(
    r"(?i)\b([A-Za-z][A-Za-z0-9._-]{2,40})@(?:oksbi|okaxis|okhdfc|okicici|ybl|"
    r"paytm|ibl|axl|hdfcbank|icici|sbi|upi|rzp|razorpay|rxairtel)\b"
)
_MERCHANT_STOP = re.compile(
    r"(?i)\s+(?:using\s+your|on\s+your\s+card|on\s+\d|ref(?:erence)?(?:\s+no)?\.?|"
    r"upi\s*:|avl\s+bal|available\s+balance|not\s+you|if\s+not|otp|a/c|account|"
    r"via\s+upi|ending\s+with|for\s+a\s+purchase).*$"
)
_SUBJECT_PREFIX = re.compile(
    r"(?i)^(transaction\s+alert|txn\s+alert|debit\s+alert|card\s+transaction|"
    r"upi\s+(?:payment|alert|txn|transaction))\s*[:\-–]?\s*"
)
_DISCLAIMER_LINE = re.compile(
    r"(?i)\b(?:otp|do\s+not\s+share|confidential|unsubscribe|click\s+here|"
    r"terms\s+and\s+conditions|ignore\s+this|dear\s+customer|greetings\s+from)\b"
)
_DATE_INLINE = re.compile(
    r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b"
)
_BAD_MERCHANT = re.compile(
    r"(?i)\b(?:"
    r"confirm\s+that\s+your"
    r"|inform\s+you\s+that"
    r"|credit\s+card\s+no\s+ending"
    r"|card\s+no\s+ending\s+with"
    r"|primary\s+card\s+holder"
    r"|visa\s+infinite"
    r"|everyday\s+cashback"
    r"|dear\s+customer"
    r"|greetings\s+from"
    r"|world\s+of\s+visa"
    r"|account\s+opening"
    r"|auto\s+repay"
    r"|transaction\s+alert"
    r"|txn\s+alert"
    r"|not\s+you"
    r"|if\s+not\s+you"
    r")\b"
)
_NOTE_MAX = 160
_DESC_MAX = 80
_MERCHANT_STOPWORDS = frozenset(
    {
        "rs",
        "inr",
        "your",
        "the",
        "a",
        "an",
        "card",
        "account",
        "bank",
        "payment",
        "transaction",
        "purchase",
        "date",
        "customer",
        "hdfc",
        "icici",
        "sbi",
        "axis",
        "yes",
        "hsbc",
    }
)


@dataclass
class ExpenseIngestSummary:
    added: int = 0
    skipped: int = 0
    results: list[str] = field(default_factory=list)


def build_alert_query(lookback_days: int = 90) -> str:
    """Gmail search for transaction-alert subjects only (not all bank mail)."""
    subjects = " OR ".join(f'subject:"{hint}"' for hint in ALERT_SUBJECT_HINTS)
    skip = " ".join(
        f'-subject:"{token}"'
        for token in (*STATEMENT_SUBJECT_SKIP, *LOAN_OFFER_SUBJECT_SKIP)
    )
    return (
        f"newer_than:{lookback_days}d -has:attachment ({subjects}) {skip}"
    ).strip()


def _strip_html(text: str) -> str:
    plain = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    plain = re.sub(r"(?s)<[^>]+>", " ", plain)
    return unescape(re.sub(r"\s+", " ", plain)).strip()


def _is_bad_merchant(text: str) -> bool:
    """True when text is bank boilerplate / marketing, not a merchant name."""
    value = (text or "").strip()
    if not value or len(value) < 2:
        return True
    if _BAD_MERCHANT.search(value):
        return True
    lowered = value.lower()
    if lowered in _MERCHANT_STOPWORDS:
        return True
    if re.fullmatch(r"[\d.,\s₹rsinr]+", lowered):
        return True
    # Subjects that are only "Rs. 99 spent" style with no merchant.
    if re.fullmatch(
        r"(?i)(?:rs\.?|inr|₹)?\s*[\d,]+\.?\d*\s*(?:spent|debited|paid)?",
        value.strip(),
    ):
        return True
    return False


def _clean_merchant(raw: str) -> str | None:
    value = _MERCHANT_STOP.sub("", raw)
    value = re.split(r"\s{2,}|\n|(?:Rs\.?|INR|₹)\s*\d", value, maxsplit=1)[0]
    value = value.strip(" .,;:|-_")
    # Drop trailing "Date" from mangled VPA lines.
    value = re.sub(r"(?i)\s+date$", "", value).strip()
    if not value or len(value) < 2:
        return None
    if _is_bad_merchant(value):
        return None
    if value.lower() in _MERCHANT_STOPWORDS:
        return None
    if re.fullmatch(r"[\d.,]+", value):
        return None
    return value[:_DESC_MAX]


def _vpa_merchant(blob: str) -> str | None:
    """Readable brand-ish local-part from a UPI VPA when body has no better hit."""
    for match in _VPA_RE.finditer(blob):
        local = match.group(1)
        # uber1.rzp / uberindiasystem187204.rzp → uber / uberindiasystem…
        head = re.split(r"[._]", local, maxsplit=1)[0]
        head = re.sub(r"\d+$", "", head)
        if len(head) < 3:
            continue
        cleaned = _clean_merchant(head)
        if cleaned:
            return cleaned
    return None


def _extract_merchant(blob: str) -> str | None:
    """Best non-boilerplate merchant string found in subject+body."""
    for pattern in _MERCHANT_PATTERNS:
        for match in pattern.finditer(blob):
            merchant = _clean_merchant(match.group(1))
            if merchant:
                return merchant
    return _vpa_merchant(blob)


def _clean_subject(subject: str) -> str | None:
    cleaned = _SUBJECT_PREFIX.sub("", subject.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:;-")
    cleaned = (cleaned or "").strip()[:_DESC_MAX]
    if not cleaned or _is_bad_merchant(cleaned):
        return None
    return cleaned


def _short_note(*, subject: str, body_plain: str, description: str) -> str | None:
    """One cleaned audit line; never the full email body."""
    parts: list[str] = []
    subj = re.sub(r"\s+", " ", subject).strip()
    if subj and not _is_bad_merchant(subj):
        parts.append(subj)
    elif subj:
        parts.append(subj[:_NOTE_MAX])
    for chunk in re.split(r"[.!?]\s+|\n+", body_plain):
        line = re.sub(r"\s+", " ", chunk).strip()
        if not line or _DISCLAIMER_LINE.search(line):
            continue
        if line.lower() == subj.lower():
            continue
        if _is_bad_merchant(line) and "purchase" not in line.lower():
            continue
        parts.append(line)
        break
    note = " — ".join(parts) if parts else ""
    note = re.sub(r"\s+", " ", note).strip()
    if len(note) > _NOTE_MAX:
        note = note[: _NOTE_MAX - 1].rstrip() + "…"
    if not note:
        return None
    if note.lower() == description.lower():
        return None
    if description.lower() in note.lower() and len(note) <= len(description) + 8:
        return None
    return note


def parse_alert_email(
    *,
    subject: str,
    body: str,
    received_at: datetime | None = None,
) -> Expense | None:
    """Pull amount / date / merchant description from a typical Indian bank alert."""
    subject_l = subject.lower()
    if any(token in subject_l for token in STATEMENT_SUBJECT_SKIP):
        return None
    if any(token in subject_l for token in LOAN_OFFER_SUBJECT_SKIP):
        return None

    body_plain = _strip_html(body)
    blob = f"{subject}\n{body_plain}"
    if _LOAN_OFFER_RE.search(blob):
        return None

    # Prefer debit-style alerts; skip pure credit/refund wording when no debit cue.
    creditish = any(w in subject_l for w in ("credited", "received", "refund", "cashback"))
    debitish = any(
        w in subject_l
        for w in (
            "spent",
            "debited",
            "debit",
            "paid",
            "purchase",
            "txn",
            "transaction",
            "upi",
            "sent using",
            "sent via",
        )
    )
    # Bodies that clearly describe a card purchase even when subject is vague.
    if not debitish and re.search(
        r"(?i)\b(?:purchase\s+transaction|spent\s+at|paid\s+to|debited)\b", body_plain
    ):
        debitish = True
    if creditish and not debitish:
        return None

    amounts = [parse_amount(m.group(1)) for m in _INR.finditer(blob)]
    amounts = [a for a in amounts if a is not None and a > 0]
    if not amounts:
        return None
    amount = amounts[0]

    spent_on: date | None = None
    for match in _DATE_INLINE.finditer(blob):
        spent_on = parse_date(match.group(1), today=date.today())
        if spent_on:
            break
    if spent_on is None and received_at is not None:
        spent_on = received_at.date()
    if spent_on is None:
        spent_on = date.today()

    merchant = _extract_merchant(blob)
    description = merchant or _clean_subject(subject) or "Gmail alert"
    note = _short_note(subject=subject, body_plain=body_plain, description=description)
    category, source = resolve_category(description, note=note, conn=None)

    return Expense(
        spent_on=spent_on,
        amount=amount,
        description=description,
        source=SOURCE_GMAIL,
        category=category,
        category_source=source,
        note=note,
    )


@dataclass
class ExpenseRefreshSummary:
    updated: int = 0
    skipped: int = 0
    failed: int = 0


def refresh_expense_from_gmail(
    conn,
    expense: Expense,
    *,
    service=None,
    interactive: bool = False,
) -> bool:
    """Re-fetch the Gmail alert and refresh description/note/(category).

    Manual ``user`` categories are preserved; description and note still update.
    Returns True when the row changed.
    """
    from .models import CATEGORY_USER

    if expense.id is None or expense.source != SOURCE_GMAIL or not expense.source_ref:
        return False
    if service is None:
        service = gmail.service(interactive=interactive)
    message = gmail.get_message(service, expense.source_ref)
    received_at = message.received_at
    if received_at is None and expense.spent_on is not None:
        received_at = datetime.combine(expense.spent_on, datetime.min.time())
    parsed = parse_alert_email(
        subject=message.subject or "",
        body=message.body or "",
        received_at=received_at,
    )
    if parsed is None:
        return False

    description = parsed.description
    note = parsed.note
    if expense.category_source == CATEGORY_USER:
        category = expense.category
        category_source = CATEGORY_USER
    else:
        category, category_source = resolve_category(
            description, note=note, conn=conn
        )

    if (
        description == expense.description
        and note == expense.note
        and category == expense.category
        and category_source == expense.category_source
    ):
        return False

    return db.update_expense(
        conn,
        expense.id,
        spent_on=expense.spent_on,
        amount=expense.amount,
        description=description,
        category=category,
        category_source=category_source,
        note=note,
    )


def refresh_gmail_expenses(
    conn,
    *,
    interactive: bool = False,
    only_bad: bool = True,
) -> ExpenseRefreshSummary:
    """Re-parse Gmail-sourced expenses from their original messages."""
    summary = ExpenseRefreshSummary()
    if not gmail.is_connected():
        return summary
    service = gmail.service(interactive=interactive)
    rows = conn.execute(
        "SELECT * FROM expenses WHERE source = ? AND source_ref IS NOT NULL",
        (SOURCE_GMAIL,),
    ).fetchall()
    for row in rows:
        expense = db.get_expense(conn, int(row["id"]))
        if expense is None:
            continue
        if only_bad and not _is_bad_merchant(expense.description):
            # Still refresh when category is blank — description may be ok but weak.
            if expense.category:
                summary.skipped += 1
                continue
        try:
            changed = refresh_expense_from_gmail(
                conn, expense, service=service, interactive=False
            )
        except Exception:  # noqa: BLE001
            logger.exception("Refresh failed for expense %s", expense.id)
            summary.failed += 1
            continue
        if changed:
            summary.updated += 1
        else:
            summary.skipped += 1
    return summary



def ingest_expense_alerts(
    conn,
    *,
    lookback_days: int = 90,
    limit: int = 200,
    interactive: bool = False,
    on_progress: ProgressCallback | None = None,
) -> ExpenseIngestSummary:
    """Search Gmail for spend alerts and store new expenses."""
    summary = ExpenseIngestSummary()
    service = gmail.service(interactive=interactive)
    query = build_alert_query(lookback_days)
    logger.info("Expense alert query: %s", query)
    message_ids = gmail.search(service, query, max_results=limit)

    total = len(message_ids)
    _emit(on_progress, ProgressEvent(index=0, total=total, filename="", phase="fetch"))

    for index, message_id in enumerate(message_ids, start=1):
        message = gmail.get_message(service, message_id)
        label = (message.subject or message_id)[:80]
        _emit(
            on_progress,
            ProgressEvent(index=index, total=total, filename=label, phase="parse"),
        )

        expense = parse_alert_email(
            subject=message.subject or "",
            body=message.body or "",
            received_at=message.received_at,
        )
        if expense is None:
            summary.skipped += 1
            _emit(
                on_progress,
                ProgressEvent(
                    index=index,
                    total=total,
                    filename=label,
                    status="skipped",
                    detail="not a spend alert",
                    phase="done",
                ),
            )
            continue

        category, source = resolve_category(
            expense.description, note=expense.note, conn=conn
        )
        expense.category = category
        expense.category_source = source

        expense.source_ref = message_id
        expense_id = db.add_expense(conn, expense)
        if expense_id is None:
            summary.skipped += 1
            status, detail = "duplicate", "already stored"
        else:
            summary.added += 1
            status, detail = "added", f"{expense.amount:.2f} {expense.description}"
        summary.results.append(f"{status}: {label}")
        _emit(
            on_progress,
            ProgressEvent(
                index=index,
                total=total,
                filename=label,
                status=status,
                detail=detail,
                phase="done",
            ),
        )

    return summary
