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
from ..text import (
    AMOUNT_RE,
    DATE_RE,
    find_card_last4,
    find_card_tail,
    parse_amount,
    parse_amounts,
    parse_date,
    parse_statement_date,
)

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
        # Closing Balance is a savings-account field; never treat it as card dues.
        "Net Outstanding",
        "Total Outstanding",
    ),
    "min_due": (
        "Minimum Amount Due",
        "Minimum Payment Due",
        "Minimal Payment Due",
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
    "Available Credit Limit". Labels glued to a leading "=" (Axis reconciliation
    formulae like "=Total Payment Due") are ignored — they annotate a formula,
    they are not field headers.
    """
    bounds = _line_bounds(text)
    raw: list[tuple[str, str, int, int]] = []
    for field, field_labels in labels.items():
        for label in field_labels:
            for match in _label_regex(label).finditer(text):
                if match.start() > 0 and text[match.start() - 1] == "=":
                    continue
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

    When a Statement Period column is present (start + end dates), leftover
    date labels such as Payment Due Date and Statement Generation Date are
    zipped onto the leftover date tokens in order. Closest-column matching
    mis-fires on Axis rows where the value columns sit denser than the labels.
    """
    ordered = sorted(hits, key=lambda hit: hit.column)
    period_hits = [
        hit
        for hit in ordered
        if hit.field == "statement_date" and _is_period_label(hit.label)
    ]
    # Statement Period rows print start + end; never zip 1:1 onto the start date.
    if (
        not period_hits
        and len(ordered) == len(tokens)
        and all(FIELD_KIND[hit.field] == token.kind for hit, token in zip(ordered, tokens))
    ):
        return {hit.field: (token.value, hit.label) for hit, token in zip(ordered, tokens)}

    if len(tokens) < len(ordered):
        return {}

    result: dict[str, tuple[object, str]] = {}
    used: set[int] = set()
    last_column = -1
    deferred_dates: list[LabelHit] = []
    for hit in ordered:
        # Defer non-period dates until after period consumes start + end.
        if (
            period_hits
            and FIELD_KIND[hit.field] == DATE
            and not (hit.field == "statement_date" and _is_period_label(hit.label))
        ):
            deferred_dates.append(hit)
            continue
        candidates = [
            (index, token)
            for index, token in enumerate(tokens)
            if index not in used
            and token.kind == FIELD_KIND[hit.field]
            and token.column > last_column
        ]
        if not candidates:
            continue
        # Statement Period rows carry start + end; the bill date is the end.
        if hit.field == "statement_date" and _is_period_label(hit.label):
            date_tokens = [
                (index, token)
                for index, token in enumerate(tokens)
                if index not in used
                and token.kind == DATE
                and token.column > last_column
            ]
            if len(date_tokens) >= 2:
                used.add(date_tokens[0][0])
                index, token = date_tokens[1]
                used.add(index)
                last_column = token.column
                result[hit.field] = (token.value, hit.label)
                continue
        index, token = min(candidates, key=lambda item: abs(item[1].column - hit.column))
        used.add(index)
        last_column = token.column
        result[hit.field] = (token.value, hit.label)

    if deferred_dates:
        remaining_dates = [
            (index, token)
            for index, token in enumerate(tokens)
            if index not in used and token.kind == DATE
        ]
        if len(remaining_dates) >= len(deferred_dates):
            for hit, (index, token) in zip(deferred_dates, remaining_dates):
                used.add(index)
                result[hit.field] = (token.value, hit.label)
        else:
            for hit in deferred_dates:
                candidates = [
                    (index, token)
                    for index, token in remaining_dates
                    if index not in used
                ]
                if not candidates:
                    break
                index, token = min(
                    candidates, key=lambda item: abs(item[1].column - hit.column)
                )
                used.add(index)
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


def _is_period_label(label: str) -> bool:
    return "period" in label.lower()


def _extract_inline(
    text: str, hits: list[LabelHit], header_lines: set[int]
) -> dict[str, tuple[object, str]]:
    """Read the value that sits right after each label."""
    found: dict[str, tuple[object, str]] = {}
    starts = sorted(hit.start for hit in hits)
    # Prefer a real Statement Date / Closing Date over Statement Period.
    ordered_hits = sorted(
        hits,
        key=lambda hit: (
            hit.field == "statement_date" and _is_period_label(hit.label),
            hit.start,
        ),
    )

    for hit in ordered_hits:
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
        elif hit.field == "statement_date":
            value = parse_statement_date(window, label=hit.label)
        else:
            value = parse_date(window)

        if value is not None:
            found[hit.field] = (value, hit.label)

    return found


def _label_rank(labels: dict[str, tuple[str, ...]], field: str, label: str) -> int:
    preferred = labels.get(field, ())
    try:
        return preferred.index(label)
    except ValueError:
        return len(preferred) + 1


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
        existing = values.get(field)
        if existing is None:
            values[field] = result
            continue
        # A header Statement Date must beat an inline Statement Period fallback.
        if (
            field == "statement_date"
            and _is_period_label(existing[1])
            and not _is_period_label(result[1])
        ):
            values[field] = result
            continue
        # Prefer earlier wording in the issuer label list (Total Payment Due
        # over a later Net Outstanding hit). Table values win ties: Axis and
        # similar layouts put the real figures under a multi-label header, while
        # later inline hits are often formula lines or T&C examples.
        if _label_rank(label_map, field, result[1]) <= _label_rank(
            label_map, field, existing[1]
        ):
            values[field] = result
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
            card_tail=find_card_tail(text),
            issuer=self.issuer_key or issuers.detect(text),
            parser=self.key,
            matched_labels={field: label for field, (_, label) in values.items()},
        )
        self._repair_min_due(statement, text)
        self._repair_due_date(statement, text, values)
        return self.postprocess(statement, text)

    def postprocess(self, statement: ParsedStatement, text: str) -> ParsedStatement:
        return statement

    def _repair_min_due(self, statement: ParsedStatement, text: str) -> None:
        """Drop a minimum due that is clearly not money for this cycle.

        SBI Cashback puts `STMT No. : A25122229867` between the MAD label and
        the real amount; a leftover id larger than total due must not stick.
        """
        min_due = statement.min_due
        total = statement.total_due
        if min_due is None or total <= 0 or min_due <= total:
            return

        for hit in _collect_hits(text, self.labels):
            if hit.field != "min_due":
                continue
            window = text[hit.end : hit.end + INLINE_WINDOW]
            window = re.split(r"\n\s*\n", window)[0]
            for amount in parse_amounts(DATE_RE.sub(" ", window)):
                if 0 <= amount <= total:
                    statement.min_due = amount
                    statement.matched_labels["min_due"] = hit.label
                    return
        statement.min_due = None
        statement.matched_labels.pop("min_due", None)

    def _repair_due_date(
        self,
        statement: ParsedStatement,
        text: str,
        values: dict[str, tuple[object, str]],
    ) -> None:
        """Drop a due date that is clearly not for this cycle.

        ICICI statements repeat sample lines like "Payment due date - Oct 26, 2023"
        in the interest illustrations. Those must not beat the header date.
        """
        due = statement.due_date
        statement_date = statement.statement_date
        if due is None:
            return
        if statement_date is None or due >= statement_date:
            return

        for hit in _collect_hits(text, self.labels):
            if hit.field != "due_date":
                continue
            window = text[hit.end : hit.end + INLINE_WINDOW]
            window = re.split(r"\n\s*\n", window)[0]
            candidate = parse_date(window)
            if candidate is not None and candidate >= statement_date:
                statement.due_date = candidate
                statement.matched_labels["due_date"] = hit.label
                return
        # Nothing plausible; better blank than a years-old example date.
        statement.due_date = None
        values.pop("due_date", None)
        statement.matched_labels.pop("due_date", None)

    @staticmethod
    def _amount(values: dict[str, tuple[object, str]], field: str) -> float | None:
        found = values.get(field)
        return float(found[0]) if found and not isinstance(found[0], date) else None

    @staticmethod
    def _date(values: dict[str, tuple[object, str]], field: str) -> date | None:
        found = values.get(field)
        return found[0] if found and isinstance(found[0], date) else None
