"""Keep demat / loan / MF PDFs from being stored as credit-card dues.

Bank senders and subject hints are broad on purpose so real card mail is not
missed. This gate is the precision layer: reject known non-card documents, and
require bill-like wording before a parse result is trusted.
"""

from __future__ import annotations

import re

# Always reject: these documents are never a credit-card bill.
_HARD_NON_CARD_RE = re.compile(
    r"(?i)\b(?:"
    r"demat(?:\s+account)?"
    r"|depository\s+participant"
    r"|mutual\s+fund\s+statement"
    r"|consolidated\s+account\s+statement"
    r"|loan\s+account\s+statement"
    r"|home\s+loan\s+statement"
    r"|statement of transactions in savings"
    r"|savings\s+a/?c\b"
    r"|current\s+account\s+statement"
    r")\b"
)

# Title-only reject: real bills often link to MITC in footnotes; only skip when
# the PDF itself opens as terms / key-fact material.
_SOFT_TITLE_RE = re.compile(
    r"(?i)\b(?:"
    r"key\s+fact\s+statement"
    r"|most\s+important\s+terms"
    r"|important\s+terms\s+(?:and|&)\s+conditions"
    r")\b"
)
_SOFT_TITLE_WINDOW = 900

# Filenames that arrive with statement mail but are not the bill itself.
_NON_STATEMENT_FILE_RE = re.compile(
    r"(?i)(?:most\s+important\s+terms|terms\s*(?:&|and)\s*conditions|key\s+fact|"
    r"mitc|schedule\s+of\s+charges)"
)

# At least one of these appears on every issuer layout we support.
_CARD_EVIDENCE = (
    "credit card",
    "minimum amount due",
    "minimum payment due",
    "minimal payment due",
    "min amount due",
    "payment due date",
    "total amount due",
    "total dues",
    "total payment due",
    "total outstanding",
    "new balance",
)

# Gmail query exclusions (subject tokens) for mail that is never a card bill.
GMAIL_SUBJECT_EXCLUSIONS = (
    "demat",
    "mutual fund",
    "consolidated account statement",
)

# Savings / current-account e-statements share bank senders with card mail.
_SAVINGS_MAIL_RE = re.compile(
    r"(?i)(?:bank statement from|statement of transactions in savings"
    r"|savings\s+a/?c\b|for your\s+(?:savings|current)\s+account)"
)


def _normalize_filename(filename: str) -> str:
    return re.sub(r"[_\s]+", " ", filename or "")


def looks_like_credit_card_statement(text: str) -> bool:
    """True when `text` looks like a credit-card bill, not another bank PDF."""
    if not (text or "").strip():
        return False
    if _HARD_NON_CARD_RE.search(text):
        return False
    head = text[:_SOFT_TITLE_WINDOW]
    if _SOFT_TITLE_RE.search(head):
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _CARD_EVIDENCE)


def is_credit_card_mail(*, subject: str = "", filename: str = "") -> bool:
    """False for covering emails / filenames that are clearly not card bills."""
    normalized_name = _normalize_filename(filename)
    if normalized_name and _NON_STATEMENT_FILE_RE.search(normalized_name):
        return False
    blob = f"{subject}\n{normalized_name}"
    if not blob.strip():
        return True
    if _HARD_NON_CARD_RE.search(blob):
        return False
    if _SOFT_TITLE_RE.search(blob):
        return False
    # "ICICI Bank Statement from … for XXXX5705" is a savings e-statement.
    if _SAVINGS_MAIL_RE.search(blob) and not re.search(r"(?i)credit\s+card", blob):
        return False
    return True
