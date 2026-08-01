"""Core data shapes.

The field names follow the ReBIT credit card schema (totalDueAmount,
minDueAmount, creditLimit, availableCredit) so that a future Account Aggregator
or Bharat Connect source can populate the same records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Where a due figure came from, best first. A manual entry always wins because
# the user is correcting something the parser got wrong or could not read.
SOURCE_MANUAL = "manual"
SOURCE_BBPS = "bbps"
SOURCE_STATEMENT = "statement"

SOURCE_PRIORITY = {SOURCE_MANUAL: 2, SOURCE_BBPS: 1, SOURCE_STATEMENT: 0}


@dataclass
class Card:
    issuer: str
    label: str
    last4: str
    id: int | None = None
    credit_limit: float | None = None
    dob: date | None = None
    name: str | None = None
    pan: str | None = None
    extra_passwords: list[str] = field(default_factory=list)


KIND_DEBIT = "debit"
KIND_CREDIT = "credit"

# Whether a category was printed by the issuer or worked out from the merchant.
CATEGORY_STATEMENT = "statement"
CATEGORY_GUESS = "guess"


@dataclass
class Transaction:
    """One line item on a statement."""

    description: str
    amount: float
    txn_date: date | None = None
    kind: str = KIND_DEBIT
    category: str | None = None
    category_source: str | None = None
    id: int | None = None

    @property
    def is_credit(self) -> bool:
        return self.kind == KIND_CREDIT

    @property
    def signed_amount(self) -> float:
        """Negative for a refund or payment, so a column of these can be summed."""
        return -abs(self.amount) if self.is_credit else self.amount


@dataclass
class ParsedStatement:
    """What a parser managed to pull out of one statement PDF."""

    total_due: float
    statement_date: date | None = None
    due_date: date | None = None
    min_due: float | None = None
    credit_limit: float | None = None
    available_credit: float | None = None
    last4: str | None = None
    # Trailing digits visible on partial masks (e.g. XX18); used to match a card
    # when the full last4 is not printed.
    card_tail: str | None = None
    issuer: str | None = None
    parser: str | None = None
    matched_labels: dict[str, str] = field(default_factory=dict)
    transactions: list[Transaction] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Share of the fields a complete statement should yield."""
        fields = [self.total_due, self.min_due, self.due_date, self.statement_date]
        return sum(1 for value in fields if value is not None) / len(fields)


@dataclass
class StatementRecord:
    """A stored due figure for one card at one point in time."""

    card_id: int
    total_due: float
    as_of: datetime
    source: str
    id: int | None = None
    statement_date: date | None = None
    due_date: date | None = None
    min_due: float | None = None
    credit_limit: float | None = None
    available_credit: float | None = None
    source_ref: str | None = None
    parser: str | None = None
    confidence: float | None = None
    note: str | None = None
