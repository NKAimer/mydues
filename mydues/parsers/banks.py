"""Per-issuer parsers.

Each one only records the wording that issuer prefers; the shared extractor in
base.py does the rest. Labels listed here are tried before the defaults.
"""

from __future__ import annotations

import re

from ..models import ParsedStatement
from ..text import parse_amount
from .base import DEFAULT_LABELS, StatementParser


def _labels(**overrides: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Put issuer wording ahead of the generic wording for each field."""
    merged = dict(DEFAULT_LABELS)
    for field, preferred in overrides.items():
        remaining = tuple(x for x in merged[field] if x not in preferred)
        merged[field] = preferred + remaining
    return merged


def _amount_on_following_line(text: str, label: str) -> float | None:
    """YES Bank puts the rupee amount on the line under the label row."""
    pattern = re.compile(
        rf"(?i){re.escape(label)}:[^\n]*\n\s*(Rs\.?\s*[\d,]+\.\d{{2}})"
    )
    match = pattern.search(text or "")
    if not match:
        return None
    return parse_amount(match.group(1))


class HdfcParser(StatementParser):
    key = "hdfc"
    issuer_key = "hdfc"
    labels = _labels(
        total_due=("Total Amount Due", "Total Dues"),
        min_due=("Minimum Amount Due", "Minimum Due"),
        due_date=("Payment Due Date", "Due Date"),
        statement_date=("Statement Date",),
    )

    # Tata Neu / Swiggy print a fused header, then min due + due date on the next line:
    #   MINIMUM DUE DUE DATE
    #   C220.00 21 Jul, 2026
    _MIN_DUE_DUE_DATE = re.compile(
        r"(?i)MINIMUM\s+DUE\s+DUE\s+DATE\s*\n\s*"
        r"(C?\s*[\d,]+\.\d{2})\s+"
        r"(\d{1,2}\s+[A-Za-z]{3},?\s+\d{2,4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    )

    def postprocess(self, statement: ParsedStatement, text: str) -> ParsedStatement:
        from ..text import parse_date

        match = self._MIN_DUE_DUE_DATE.search(text or "")
        if not match:
            return statement
        amount = parse_amount(match.group(1))
        due = parse_date(match.group(2))
        if amount is not None and (statement.min_due is None or statement.min_due == 0):
            statement.min_due = amount
            statement.matched_labels["min_due"] = "Minimum Due"
        if due is not None and statement.due_date is None:
            statement.due_date = due
            statement.matched_labels["due_date"] = "Due Date"
        elif due is not None and statement.statement_date and due >= statement.statement_date:
            statement.due_date = due
            statement.matched_labels["due_date"] = "Due Date"
        return statement


class IciciParser(StatementParser):
    key = "icici"
    issuer_key = "icici"
    labels = _labels(
        total_due=("Total Amount due", "Total Amount Due"),
        min_due=("Minimum Amount due", "Minimum Amount Due"),
        due_date=("Payment Due Date", "Due Date"),
        # Statement Date first; Statement Period is a last resort and its end
        # day is used (see parse_statement_date).
        statement_date=("Statement Date", "Statement Period"),
    )


class SbiCardParser(StatementParser):
    key = "sbicard"
    issuer_key = "sbicard"
    labels = _labels(
        total_due=("Total Amount Due", "Total Outstanding"),
        min_due=("Minimum Amount Due",),
        due_date=("Payment Due Date",),
        statement_date=("Statement Date",),
    )


class AxisParser(StatementParser):
    key = "axis"
    issuer_key = "axis"
    labels = _labels(
        total_due=("Total Payment Due", "Total Amount Due"),
        min_due=("Minimum Payment Due", "Minimum Amount Due"),
        due_date=("Payment Due Date",),
        # Generation Date is the bill date on Axis Flipkart/Airtel/Ace PDFs;
        # Statement Period is only the billing window (start–end).
        statement_date=(
            "Statement Generation Date",
            "Statement Date",
            "Statement Period",
        ),
    )


class KotakParser(StatementParser):
    key = "kotak"
    issuer_key = "kotak"
    labels = _labels(
        total_due=("Total Amount Due", "Total Dues"),
        min_due=("Minimum Amount Due",),
        due_date=("Payment Due Date",),
        statement_date=("Statement Date",),
    )


class IdfcFirstParser(StatementParser):
    key = "idfcfirst"
    issuer_key = "idfcfirst"
    labels = _labels(
        total_due=("Total Amount Due", "Total Dues"),
        min_due=("Minimum Amount Due",),
        due_date=("Payment Due Date",),
        statement_date=("Statement Date",),
    )


class AmexParser(StatementParser):
    key = "amex"
    issuer_key = "amex"
    labels = _labels(
        total_due=("New Balance", "Total Amount Due"),
        min_due=("Minimum Payment Due", "Minimum Amount Due"),
        due_date=("Payment Due Date", "Please Pay By"),
        statement_date=("Closing Date", "Statement Date"),
        credit_limit=("Total Credit Limit", "Credit Limit"),
    )


class HsbcParser(StatementParser):
    key = "hsbc"
    issuer_key = "hsbc"
    labels = _labels(
        total_due=("Total Payment Due", "Total Dues", "Total Amount Due"),
        min_due=("Minimal Payment Due", "Minimum Amount Due", "Minimum Payment Due"),
        due_date=("Payment Due Date",),
        statement_date=("Statement Date", "Statement Period"),
        credit_limit=("Credit Limit", "Your Credit Limit"),
        available_credit=("Available Credit Limit", "Available Credit"),
    )


# Ledger "Net Outstanding" is not the bill; never treat it as total due.
HsbcParser.labels["total_due"] = tuple(
    label for label in HsbcParser.labels["total_due"] if label != "Net Outstanding"
)


class IndusIndParser(StatementParser):
    key = "indusind"
    issuer_key = "indusind"


class RblParser(StatementParser):
    key = "rbl"
    issuer_key = "rbl"


class YesBankParser(StatementParser):
    key = "yesbank"
    issuer_key = "yesbank"
    labels = _labels(
        total_due=("Total Amount Due",),
        min_due=("Minimum Amount Due",),
        due_date=("Payment Due Date",),
        statement_date=("Statement Date", "Statement Period"),
        credit_limit=("Credit Limit",),
        available_credit=("Available Credit Limit",),
    )

    def postprocess(self, statement: ParsedStatement, text: str) -> ParsedStatement:
        # Label row mixes in Cash Limit / Points Earned; values are on the next line:
        #   Total Amount Due: Cash Limit: Points Earned : 0
        #   Rs. 12,255.00 Rs. 0.00
        total = _amount_on_following_line(text, "Total Amount Due")
        if total is not None:
            statement.total_due = total
            statement.matched_labels["total_due"] = "Total Amount Due"
        minimum = _amount_on_following_line(text, "Minimum Amount Due")
        if minimum is not None:
            statement.min_due = minimum
            statement.matched_labels["min_due"] = "Minimum Amount Due"
        return statement


class AuParser(StatementParser):
    key = "au"
    issuer_key = "au"


class FederalParser(StatementParser):
    key = "federal"
    issuer_key = "federal"


class OneCardParser(StatementParser):
    key = "onecard"
    issuer_key = "onecard"


class GenericParser(StatementParser):
    """Last resort for issuers without a dedicated parser."""

    key = "generic"
    issuer_key = None
