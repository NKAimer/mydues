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
    re.compile(
        r"(?i)(?:spent\s+at|debited\s+(?:for|at|to)|paid\s+to|towards|"
        r"info\s*:|merchant\s*:)\s+([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80})"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z])(?:at|to)\s+([A-Za-z0-9][A-Za-z0-9 &.'@/_-]{1,80})"
    ),
)
_MERCHANT_STOP = re.compile(
    r"(?i)\s+(?:using\s+your|on\s+your\s+card|on\s+\d|ref(?:erence)?(?:\s+no)?\.?|"
    r"upi\s*:|avl\s+bal|available\s+balance|not\s+you|if\s+not|otp|a/c|account).*$"
)
_SUBJECT_PREFIX = re.compile(
    r"(?i)^(transaction\s+alert|txn\s+alert|debit\s+alert|card\s+transaction)\s*[:\-–]?\s*"
)
_DISCLAIMER_LINE = re.compile(
    r"(?i)\b(?:otp|do\s+not\s+share|confidential|unsubscribe|click\s+here|"
    r"terms\s+and\s+conditions|ignore\s+this)\b"
)
_DATE_INLINE = re.compile(
    r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b"
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


def _clean_merchant(raw: str) -> str | None:
    value = _MERCHANT_STOP.sub("", raw)
    value = re.split(r"\s{2,}|\n|(?:Rs\.?|INR|₹)\s*\d", value, maxsplit=1)[0]
    value = value.strip(" .,;:|-")
    if not value or len(value) < 2:
        return None
    if value.lower() in _MERCHANT_STOPWORDS:
        return None
    if re.fullmatch(r"[\d.,]+", value):
        return None
    if value.lower() in {"rs", "inr"}:
        return None
    return value[:_DESC_MAX]


def _extract_merchant(blob: str) -> str | None:
    for pattern in _MERCHANT_PATTERNS:
        for match in pattern.finditer(blob):
            merchant = _clean_merchant(match.group(1))
            if merchant:
                return merchant
    return None


def _clean_subject(subject: str) -> str:
    cleaned = _SUBJECT_PREFIX.sub("", subject.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:;-")
    return (cleaned or subject.strip())[:_DESC_MAX] or "Gmail alert"


def _short_note(*, subject: str, body_plain: str, description: str) -> str | None:
    """One cleaned audit line; never the full email body."""
    parts: list[str] = []
    subj = re.sub(r"\s+", " ", subject).strip()
    if subj:
        parts.append(subj)
    for chunk in re.split(r"[.!?]\s+|\n+", body_plain):
        line = re.sub(r"\s+", " ", chunk).strip()
        if not line or _DISCLAIMER_LINE.search(line):
            continue
        if line.lower() == subj.lower():
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
    description = merchant or _clean_subject(subject)
    note = _short_note(subject=subject, body_plain=body_plain, description=description)
    category, _source = resolve_category(description, note=note, conn=None)

    return Expense(
        spent_on=spent_on,
        amount=amount,
        description=description,
        source=SOURCE_GMAIL,
        category=category,
        note=note,
    )


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

        category, _source = resolve_category(
            expense.description, note=expense.note, conn=conn
        )
        expense.category = category

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
