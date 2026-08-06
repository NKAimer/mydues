"""Detect bank charges / EMI / interest on statement lines and expense text."""

from __future__ import annotations

import re

# Stable labels stored on transactions.charge_kind (statement display only).
CHARGE_ANNUAL_FEE = "annual_fee"
CHARGE_INTEREST = "interest"
CHARGE_LATE_FEE = "late_fee"
CHARGE_EMI = "emi"
CHARGE_GST_FEE = "gst_fee"
CHARGE_FEE = "fee"

CHARGE_LABELS = {
    CHARGE_ANNUAL_FEE: "Annual fee",
    CHARGE_INTEREST: "Interest",
    CHARGE_LATE_FEE: "Late fee",
    CHARGE_EMI: "EMI",
    CHARGE_GST_FEE: "GST on fee",
    CHARGE_FEE: "Fee",
}

# Preferred spend category when charge_kind is detected on the guess path.
CHARGE_CATEGORIES = {
    CHARGE_ANNUAL_FEE: "Fees & interest",
    CHARGE_INTEREST: "Fees & interest",
    CHARGE_LATE_FEE: "Fees & interest",
    CHARGE_EMI: "EMI",
    CHARGE_GST_FEE: "Fees & interest",
    CHARGE_FEE: "Fees & interest",
}

# Ordered: first match wins. Prefer bank phrasing over bare tokens.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        CHARGE_ANNUAL_FEE,
        re.compile(
            r"\b("
            r"annual\s+fee|membership\s+fee|joining\s+fee|"
            r"renewal\s+fee|card\s+fee"
            r")\b",
            re.I,
        ),
    ),
    (
        CHARGE_LATE_FEE,
        re.compile(
            r"\b("
            r"late\s+payment|late\s+fee|overdue\s+charge|overdue\s+fee|"
            r"payment\s+overdue"
            r")\b",
            re.I,
        ),
    ),
    (
        CHARGE_INTEREST,
        re.compile(
            r"\b("
            r"finance\s+charge|interest\s+charge|revolving\s+interest|"
            r"interest\b|cash\s+advance\s+interest"
            r")\b",
            re.I,
        ),
    ),
    (
        CHARGE_EMI,
        re.compile(
            r"\b("
            r"easy\s+emi|loan\s+install?ments?|emi\s+install?ments?|"
            r"emi|install?ments?"
            r")\b",
            re.I,
        ),
    ),
    (
        CHARGE_GST_FEE,
        re.compile(
            r"\b("
            r"gst\s+on\s+(?:annual\s+)?fee|gst\s+on\s+membership|"
            r"igst\s+on\s+fee|cgst\s+on\s+fee|sgst\s+on\s+fee|"
            r"fee[- ]related\s+gst|gst\s+on\s+finance"
            r")\b",
            re.I,
        ),
    ),
    (
        CHARGE_FEE,
        re.compile(
            r"\b("
            r"service\s+fee|processing\s+fee|convenience\s+fee|"
            r"surcharge|markup\s+fee|penalty\s+fee|"
            r"bank\s+charges?|fee\s+charges?|"
            r"fees?\b"
            r")",
            re.I,
        ),
    ),
]


def detect_charge_kind(description: str, *, note: str | None = None) -> str | None:
    """Return a charge_kind for bank fee / EMI / interest wording, else None.

    Uses word-boundary-ish patterns so merchant names that merely contain the
    letters of ``fee`` / ``emi`` (e.g. coffee, premium) stay unflagged.
    """
    blob = f"{description or ''} {note or ''}".strip()
    if not blob:
        return None
    for kind, pattern in _PATTERNS:
        if pattern.search(blob):
            return kind
    return None


def charge_label(kind: str | None) -> str | None:
    if not kind:
        return None
    return CHARGE_LABELS.get(kind, kind.replace("_", " ").title())


def category_for_charge(kind: str | None) -> str | None:
    if not kind:
        return None
    return CHARGE_CATEGORIES.get(kind)
