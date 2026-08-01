"""Keep demat / loan / MF PDFs from being stored as credit-card dues.

Bank senders and subject hints are broad on purpose so real card mail is not
missed. This gate is the precision layer: reject known non-card documents, and
require bill-like wording before a parse result is trusted.
"""

from __future__ import annotations

import re

# Strong negatives: if any match, this is not a credit-card statement.
_NON_CARD_RE = re.compile(
    r"(?i)\b(?:"
    r"demat(?:\s+account)?"
    r"|depository\s+participant"
    r"|mutual\s+fund\s+statement"
    r"|consolidated\s+account\s+statement"
    r"|loan\s+account\s+statement"
    r"|home\s+loan\s+statement"
    r")\b"
)

# At least one of these appears on every issuer layout we support.
_CARD_EVIDENCE = (
    "credit card",
    "minimum amount due",
    "minimum payment due",
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


def looks_like_credit_card_statement(text: str) -> bool:
    """True when `text` looks like a credit-card bill, not another bank PDF."""
    if not (text or "").strip():
        return False
    if _NON_CARD_RE.search(text):
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _CARD_EVIDENCE)


def is_credit_card_mail(*, subject: str = "", filename: str = "") -> bool:
    """False for covering emails / filenames that are clearly not card bills."""
    blob = f"{subject}\n{filename}"
    if not blob.strip():
        return True
    return _NON_CARD_RE.search(blob) is None
