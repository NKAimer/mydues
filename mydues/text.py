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
_CURRENCY = r"(?:INR|Rs\.?|₹|`|C(?=\d))"


AMOUNT_RE = re.compile(
    rf"(?P<sign>-)?\s*{_CURRENCY}?\s*(?P<num>{_AMOUNT_CORE})\s*(?P<suffix>Cr|Dr|CR|DR)?\b",
    re.IGNORECASE,
)

# HSBC prints posting dates as 25JUN / 02JUL with no separator or year.
_MONTH_ABBR = (
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
)
_DATE_PATTERNS = [
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
    r"\d{1,2}[\s\-][A-Za-z]{3,9}[\s,\-]*\d{2,4}",
    rf"\d{{1,2}}(?:{_MONTH_ABBR})(?:\s*\d{{2,4}})?",
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}",
    r"\d{4}-\d{2}-\d{2}",
]
DATE_RE = re.compile("|".join(f"(?:{p})" for p in _DATE_PATTERNS))

# Some ICICI (Amazon Pay) PDFs paint each header letter twice, so extraction
# yields "PPAAYYMMEENNTT DDUUEE DDAATTEE" instead of "PAYMENT DUE DATE".
_DOUBLED_WORD = re.compile(r"[A-Za-z]{4,}")


def undouble_glyphs(text: str) -> str:
    """Collapse doubled letter runs: PPAAYYMMEENNTT → PAYMENT.

    Card masks like XXXX are left alone — every letter is the same, which is
    masking, not the doubled-glyph artefact.
    """

    def collapse(match: re.Match[str]) -> str:
        word = match.group(0)
        if len(word) % 2:
            return word
        chars = list(word)
        if not all(chars[i] == chars[i + 1] for i in range(0, len(chars), 2)):
            return word
        collapsed = "".join(chars[::2])
        if len(set(collapsed)) == 1:
            return word
        return collapsed

    return _DOUBLED_WORD.sub(collapse, text)


def normalize(text: str) -> str:
    """Collapse the ragged whitespace that PDF text extraction produces."""
    text = text.replace("\u00a0", " ").replace("\u20b9", "₹")
    text = undouble_glyphs(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


_PHONE_CONTEXT_RE = re.compile(
    r"(?i)toll\s*free|\bphone\b|\bcall\b|\+\s*80\d|\bivr\b|\bhelpline\b"
)

# Dial-ish integers that are not formatted as Indian currency.
_PHONE_LIKE_DIGITS = frozenset(range(7, 9))  # 7–8 digits


def _is_reference_id(match: re.Match[str], raw: str) -> bool:
    """True for statement numbers mistaken for money (e.g. STMT No. A25122229867)."""
    num = match.group("num") or ""
    if "," in num or "." in num:
        return False
    if len(num) >= 10:
        return True
    prefix = raw[max(0, match.start() - 40) : match.start()]
    return bool(re.search(r"(?i)(?:STMT|Statement)\s*No\.?", prefix))


def is_money_shaped_match(match: re.Match[str]) -> bool:
    """True when the match looks like printed money, not a bare dial string."""
    num = match.group("num") or ""
    if "," in num or "." in num:
        return True
    # Currency marker on the match (C / Rs / ₹ / INR).
    span = match.group(0) or ""
    return bool(re.search(_CURRENCY, span, re.IGNORECASE))


def _is_phone_or_padded_id(match: re.Match[str], raw: str) -> bool:
    """True for dial strings like 08250825 / Toll Free 8250825, not rupees."""
    num = match.group("num") or ""
    if "," in num or "." in num or len(num) <= 1:
        return False
    if num.startswith("0") and not num.startswith("0."):
        return True
    if len(num) in _PHONE_LIKE_DIGITS and not is_money_shaped_match(match):
        return True
    start, end = match.start(), match.end()
    window = raw[max(0, start - 60) : min(len(raw), end + 60)]
    return bool(_PHONE_CONTEXT_RE.search(window))


def looks_phone_like_amount(value: float | None) -> bool:
    """True for integer-valued totals in the dial-string ballpark (e.g. 8250825)."""
    if value is None:
        return False
    magnitude = abs(float(value))
    if magnitude != int(magnitude):
        return False
    digits = len(str(int(magnitude)))
    return digits in _PHONE_LIKE_DIGITS and 1_000_000 <= magnitude <= 99_999_999


def _amount_from_match(match: re.Match[str]) -> float | None:
    try:
        value = float(match.group("num").replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group("suffix") or "").lower()
    if match.group("sign") or suffix == "cr":
        value = -value
    return value


def parse_amount(raw: str) -> float | None:
    """Return a signed amount. A Cr suffix means the issuer owes you."""
    text = raw or ""
    for match in AMOUNT_RE.finditer(text):
        if _is_reference_id(match, text) or _is_phone_or_padded_id(match, text):
            continue
        value = _amount_from_match(match)
        if value is not None:
            return value
    return None


def parse_amount_meta(raw: str) -> tuple[float, bool] | None:
    """Like parse_amount, plus whether the winning match was money-shaped."""
    text = raw or ""
    for match in AMOUNT_RE.finditer(text):
        if _is_reference_id(match, text) or _is_phone_or_padded_id(match, text):
            continue
        value = _amount_from_match(match)
        if value is not None:
            return value, is_money_shaped_match(match)
    return None


def parse_amounts(raw: str) -> list[float]:
    """Every plausible money amount in `raw`, skipping statement/reference ids."""
    text = raw or ""
    found: list[float] = []
    for match in AMOUNT_RE.finditer(text):
        if _is_reference_id(match, text) or _is_phone_or_padded_id(match, text):
            continue
        value = _amount_from_match(match)
        if value is not None:
            found.append(value)
    return found


def _coerce_date(candidate: str, *, today: date | None = None) -> date | None:
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


def parse_date(raw: str, *, today: date | None = None) -> date | None:
    match = DATE_RE.search(raw or "")
    if not match:
        return None
    return _coerce_date(match.group(0).strip().rstrip(","), today=today)


def parse_dates(raw: str, *, today: date | None = None) -> list[date]:
    """Every date in `raw`, in order of appearance."""
    found: list[date] = []
    for match in DATE_RE.finditer(raw or ""):
        value = _coerce_date(match.group(0).strip().rstrip(","), today=today)
        if value is not None:
            found.append(value)
    return found


def parse_statement_date(raw: str, *, label: str = "", today: date | None = None) -> date | None:
    """A statement date, or the closing day of a statement period.

    "Statement Period: June 29, 2026 to July 28, 2026" must yield July 28 (the
    bill date), not the period start. Use the second date of the range only —
    later transaction dates in the same window must not win.
    """
    dates = parse_dates(raw, today=today)
    if not dates:
        return None
    if "period" in label.lower() and len(dates) >= 2:
        return dates[1]
    return dates[0]


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


# Grouped masks (4321 XXXX XXXX 8765), compact (652926XXXXXX2750),
# HSBC-style (43xx xxxx xxxx 7672), and SBI partial tails (XXXX XXXX XXXX XX18).
_CARD_GROUPED_RE = re.compile(
    r"(?<!\d)(?:\d{4}|[Xx*]{4}|\d{2}[Xx*]{2})[\s-]*"
    r"[Xx*]{4}[\s-]*"
    r"(?:[Xx*]{4}|\d{4})[\s-]*"
    r"(\d{4})(?!\d)"
)
_CARD_COMPACT_RE = re.compile(r"(?<!\d)\d{4,6}[Xx*]{4,8}(\d{4})(?!\d)")
_CARD_LABELED_RE = re.compile(
    r"(?i)(?:credit\s*card\s*no\.?|card\s*(?:no\.?|number)|primary\s+card\s+number)"
    r"\s*[:.]?\s*([0-9Xx* ]{12,30})"
)
# SBI often prints only the last two digits: XXXX XXXX XXXX XX18
_CARD_PARTIAL_TAIL_RE = re.compile(
    r"(?<!\d)(?:[Xx*]{4}[\s-]*){3}[Xx*]{2}(\d{2})(?!\d)"
)
_ALTERNATE_ACCOUNT_RE = re.compile(
    r"(?i)alternate\s+(?:account|a/?c)\s*(?:number|no\.?)?\s*:?\s*\d+"
)

# Kept for callers/tests that still import the old name.
CARD_NUMBER_RE = _CARD_GROUPED_RE


def _last4_from_labeled_token(token: str) -> str | None:
    if not re.search(r"[Xx*]", token):
        return None
    digits = re.sub(r"\D", "", token)
    if len(digits) >= 4:
        return digits[-4:]
    return None


def find_all_card_last4s(text: str) -> list[str]:
    """Every distinct last4 from masked card numbers in `text`, best-first."""
    cleaned = _ALTERNATE_ACCOUNT_RE.sub(" ", text or "")
    seen: list[str] = []

    def add(last4: str | None) -> None:
        if last4 and last4 not in seen:
            seen.append(last4)

    for match in _CARD_LABELED_RE.finditer(cleaned):
        add(_last4_from_labeled_token(match.group(1)))

    for pattern in (_CARD_COMPACT_RE, _CARD_GROUPED_RE):
        for match in pattern.finditer(cleaned):
            add(match.group(1))

    return seen


def find_card_last4(text: str) -> str | None:
    """Last four digits of a masked credit-card number in `text`."""
    all_last4s = find_all_card_last4s(text)
    return all_last4s[0] if all_last4s else None


def find_card_tail(text: str) -> str | None:
    """Visible trailing card digits when the PDF only prints a partial mask.

    SBI Cashback / PhonePe often show `XXXX XXXX XXXX XX18` — two digits that
    still uniquely identify a registered card ending in 18.
    """
    full = find_card_last4(text)
    if full:
        return full
    cleaned = _ALTERNATE_ACCOUNT_RE.sub(" ", text or "")
    match = _CARD_PARTIAL_TAIL_RE.search(cleaned)
    return match.group(1) if match else None
