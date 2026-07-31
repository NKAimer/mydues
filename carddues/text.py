"""Amount and date extraction from statement text.

Indian statements format money as 1,23,456.78 and mark credit balances with a
trailing Cr, which flips the sign of what you owe.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from dateutil import parser as dateparser

_AMOUNT_CORE = r"\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_CURRENCY = r"(?:INR|Rs\.?|₹|`)"

AMOUNT_RE = re.compile(
    rf"(?P<sign>-)?\s*{_CURRENCY}?\s*(?P<num>{_AMOUNT_CORE})\s*(?P<suffix>Cr|Dr|CR|DR)?\b",
    re.IGNORECASE,
)

_DATE_PATTERNS = [
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
    r"\d{1,2}[\s\-][A-Za-z]{3,9}[\s,\-]*\d{2,4}",
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}",
    r"\d{4}-\d{2}-\d{2}",
]
DATE_RE = re.compile("|".join(f"(?:{p})" for p in _DATE_PATTERNS))


def normalize(text: str) -> str:
    """Collapse the ragged whitespace that PDF text extraction produces."""
    text = text.replace("\u00a0", " ").replace("\u20b9", "₹")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def parse_amount(raw: str) -> float | None:
    """Return a signed amount. A Cr suffix means the issuer owes you."""
    match = AMOUNT_RE.search(raw or "")
    if not match:
        return None
    try:
        value = float(match.group("num").replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group("suffix") or "").lower()
    if match.group("sign") or suffix == "cr":
        value = -value
    return value


def parse_date(raw: str, *, today: date | None = None) -> date | None:
    match = DATE_RE.search(raw or "")
    if not match:
        return None
    candidate = match.group(0).strip().rstrip(",")
    try:
        parsed = dateparser.parse(candidate, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    result = parsed.date()
    # A two-digit year can land a century off; statements are never that old.
    reference = today or date.today()
    if result.year < reference.year - 30:
        result = result.replace(year=result.year + 100)
    return result


def _label_pattern(label: str) -> re.Pattern[str]:
    # Labels wrap across lines and pick up stray punctuation between words.
    parts = [re.escape(word) for word in label.split()]
    return re.compile(r"[\s:\.\-]*".join(parts), re.IGNORECASE)


def _window_after(text: str, end: int, *, chars: int) -> str:
    return text[end : end + chars]


def find_labeled_amount(
    text: str, labels: Iterable[str], *, window: int = 120
) -> tuple[float, str] | None:
    """Find the amount that follows any of `labels`.

    Returns the value and the label that matched, so callers can record which
    wording a statement used.
    """
    for label in labels:
        for match in _label_pattern(label).finditer(text):
            window_text = _window_after(text, match.end(), chars=window)
            # Stop before the next label so a missing value cannot borrow one.
            window_text = re.split(r"\n\s*\n", window_text)[0]
            value = parse_amount(window_text)
            if value is not None:
                return value, label
    return None


def find_labeled_date(
    text: str, labels: Iterable[str], *, window: int = 120, today: date | None = None
) -> tuple[date, str] | None:
    for label in labels:
        for match in _label_pattern(label).finditer(text):
            window_text = _window_after(text, match.end(), chars=window)
            window_text = re.split(r"\n\s*\n", window_text)[0]
            value = parse_date(window_text, today=today)
            if value is not None:
                return value, label
    return None


CARD_NUMBER_RE = re.compile(
    r"(?:\d{4}|X{4}|x{4}|\*{4})[\s-]*(?:X{4}|x{4}|\*{4}|\d{4})[\s-]*(?:X{4}|x{4}|\*{4}|\d{4})[\s-]*(\d{4})"
)


def find_card_last4(text: str) -> str | None:
    match = CARD_NUMBER_RE.search(text or "")
    return match.group(1) if match else None
