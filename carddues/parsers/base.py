"""Label-driven extraction shared by every issuer parser.

Statements express the same handful of numbers in two layouts: inline
("Total Amount Due: 45,000.00") and as a table where a row of labels sits above
a row of values. Both are handled here so an issuer parser only has to say
which wording it uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .. import issuers
from ..models import ParsedStatement
from ..text import AMOUNT_RE, DATE_RE, find_card_last4, parse_amount, parse_date

AMOUNT = "amount"
DATE = "date"

FIELD_KIND: dict[str, str] = {
    "total_due": AMOUNT,
    "min_due": AMOUNT,
    "credit_limit": AMOUNT,
    "available_credit": AMOUNT,
    "due_date": DATE,
    "statement_date": DATE,
}

DEFAULT_LABELS: dict[str, tuple[str, ...]] = {
    "total_due": (
        "Total Amount Due",
        "Total Payment Due",
        "Total Amount Payable",
        "Total Dues",
        "Total Due",
        "New Balance",
        "Closing Balance",
        "Net Outstanding",
        "Total Outstanding",
    ),
    "min_due": (
        "Minimum Amount Due",
        "Minimum Payment Due",
        "Minimum Amount Payable",
        "Min Amount Due",
        "Minimum Due",
        "Min Due",
    ),
    "due_date": (
        "Payment Due Date",
        "Payment Due By",
        "Pay By Date",
        "Due Date",
    ),
    "statement_date": (
        "Statement Generation Date",
        "Statement Date",
        "Statement Period",
        "Bill Date",
    ),
    "credit_limit": (
        "Total Credit Limit",
        "Sanctioned Credit Limit",
        "Credit Limit",
    ),
    "available_credit": (
        "Available Credit Limit",
        "Available Credit",
        "Available Limit",
    ),
}

# How far past a label to look for its value when nothing else interrupts.
INLINE_WINDOW = 90


@dataclass(frozen=True)
class LabelHit:
    field: str
    label: str
    start: int
    end: int
    line: int
    column: int


@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    column: int


def _label_regex(label: str) -> re.Pattern[str]:
    parts = [re.escape(word) for word in label.split()]
    return re.compile(r"[\s:\.\-]{0,4}".join(parts), re.IGNORECASE)


def _line_bounds(text: str) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    position = 0
    for line in text.split("\n"):
        bounds.append((position, position + len(line)))
        position += len(line) + 1
    return bounds


def _locate(bounds: list[tuple[int, int]], offset: int) -> tuple[int, int]:
    for index, (start, end) in enumerate(bounds):
        if start <= offset <= end:
            return index, offset - start
    return len(bounds) - 1, 0


def _collect_hits(text: str, labels: dict[str, tuple[str, ...]]) -> list[LabelHit]:
    """Every label occurrence, with nested and overlapping ones removed.

    Dropping contained spans is what stops "Credit Limit" from matching inside
    "Available Credit Limit".
    """
    bounds = _line_bounds(text)
    raw: list[tuple[str, str, int, int]] = []
    for field, field_labels in labels.items():
        for label in field_labels:
            for match in _label_regex(label).finditer(text):
                raw.append((field, label, match.start(), match.end()))

    raw.sort(key=lambda item: (item[2], -(item[3] - item[2])))
    accepted: list[LabelHit] = []
    for field, label, start, end in raw:
        if any(start < kept.end and end > kept.start for kept in accepted):
            continue
        line, column = _locate(bounds, start)
        accepted.append(LabelHit(field, label, start, end, line, column))
    return accepted


def _tokenize(line: str) -> list[Token]:
    """Split a value row into dates and amounts, in order.

    Dates are claimed first so that 05/08/2026 is not read as the number 5.
    """
    claimed: list[tuple[int, int]] = []
    tokens: list[Token] = []

    for match in DATE_RE.finditer(line):
        value = parse_date(match.group(0))
        if value is not None:
            claimed.append((match.start(), match.end()))
            tokens.append(Token(DATE, value, match.start()))

    for match in AMOUNT_RE.finditer(line):
        if not match.group("num"):
            continue
        if any(match.start() < end and match.end() > start for start, end in claimed):
            continue
        value = parse_amount(match.group(0))
        if value is not None:
            tokens.append(Token(AMOUNT, value, match.start()))

    tokens.sort(key=lambda token: token.column)
    return tokens


def _align(hits: list[LabelHit], tokens: list[Token]) -> dict[str, tuple[object, str]]:
    """Pair a row of labels with the row of values beneath it.

    Equal counts pair up in order. Otherwise each label takes the closest
    unused token of the right kind, which handles value rows that carry extra
    columns we have no label for.
    """
    ordered = sorted(hits, key=lambda hit: hit.column)

    if len(ordered) == len(tokens):
        if all(FIELD_KIND[hit.field] == token.kind for hit, token in zip(ordered, tokens)):
            return {hit.field: (token.value, hit.label) for hit, token in zip(ordered, tokens)}
        return {}

    if len(tokens) < len(ordered):
        return {}

    result: dict[str, tuple[object, str]] = {}
    used: set[int] = set()
    last_column = -1
    for hit in ordered:
        candidates = [
            (index, token)
            for index, token in enumerate(tokens)
            if index not in used
            and token.kind == FIELD_KIND[hit.field]
            and token.column > last_column
        ]
        if not candidates:
            continue
        index, token = min(candidates, key=lambda item: abs(item[1].column - hit.column))
        used.add(index)
        last_column = token.column
        result[hit.field] = (token.value, hit.label)
    return result


def _extract_from_tables(text: str, hits: list[LabelHit]) -> dict[str, tuple[object, str]]:
    lines = text.split("\n")
    found: dict[str, tuple[object, str]] = {}

    by_line: dict[int, list[LabelHit]] = {}
    for hit in hits:
        by_line.setdefault(hit.line, []).append(hit)

    for line_index, line_hits in sorted(by_line.items()):
        if len(line_hits) < 2:
            continue
        # A label row carries no values of its own.
        if _tokenize(lines[line_index]):
            continue

        for offset in (1, 2):
            if line_index + offset >= len(lines):
                break
            tokens = _tokenize(lines[line_index + offset])
            if not tokens:
                continue
            for field, result in _align(line_hits, tokens).items():
                found.setdefault(field, result)
            break

    return found


def _extract_inline(
    text: str, hits: list[LabelHit], header_lines: set[int]
) -> dict[str, tuple[object, str]]:
    """Read the value that sits right after each label."""
    found: dict[str, tuple[object, str]] = {}
    starts = sorted(hit.start for hit in hits)

    for hit in hits:
        if hit.field in found:
            continue
        # Labels in a table header have their values a row below, not after them.
        if hit.line in header_lines:
            continue

        # Never read past the next label, or a value could be borrowed from it.
        next_label = next((start for start in starts if start > hit.start), len(text))
        window = text[hit.end : min(hit.end + INLINE_WINDOW, next_label)]
        window = re.split(r"\n\s*\n", window)[0]

        if FIELD_KIND[hit.field] == AMOUNT:
            # A date after the label must not be read as a number.
            value = parse_amount(DATE_RE.sub(" ", window))
        else:
            value = parse_date(window)

        if value is not None:
            found[hit.field] = (value, hit.label)

    return found


def extract_fields(
    text: str, labels: dict[str, tuple[str, ...]] | None = None
) -> dict[str, tuple[object, str]]:
    label_map = labels or DEFAULT_LABELS
    hits = _collect_hits(text, label_map)

    counts: dict[int, int] = {}
    for hit in hits:
        counts[hit.line] = counts.get(hit.line, 0) + 1
    header_lines = {line for line, count in counts.items() if count >= 2}

    values = _extract_inline(text, hits, header_lines)
    for field, result in _extract_from_tables(text, hits).items():
        values.setdefault(field, result)
    return values


class StatementParser:
    """Base parser. Subclasses adjust labels or post-process the result."""

    key = "generic"
    issuer_key: str | None = None
    labels: dict[str, tuple[str, ...]] = DEFAULT_LABELS

    def matches(self, text: str) -> bool:
        if not self.issuer_key:
            return False
        markers = issuers.get(self.issuer_key).markers
        lowered = text.lower()
        return any(marker in lowered for marker in markers)

    def parse(self, text: str) -> ParsedStatement | None:
        values = extract_fields(text, self.labels)
        total = values.get("total_due")
        if total is None:
            return None

        statement = ParsedStatement(
            total_due=float(total[0]),
            min_due=self._amount(values, "min_due"),
            due_date=self._date(values, "due_date"),
            statement_date=self._date(values, "statement_date"),
            credit_limit=self._amount(values, "credit_limit"),
            available_credit=self._amount(values, "available_credit"),
            last4=find_card_last4(text),
            issuer=self.issuer_key or issuers.detect(text),
            parser=self.key,
            matched_labels={field: label for field, (_, label) in values.items()},
        )
        return self.postprocess(statement, text)

    def postprocess(self, statement: ParsedStatement, text: str) -> ParsedStatement:
        return statement

    @staticmethod
    def _amount(values: dict[str, tuple[object, str]], field: str) -> float | None:
        found = values.get(field)
        return float(found[0]) if found and not isinstance(found[0], date) else None

    @staticmethod
    def _date(values: dict[str, tuple[object, str]], field: str) -> date | None:
        found = values.get(field)
        return found[0] if found and isinstance(found[0], date) else None
