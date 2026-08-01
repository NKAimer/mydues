"""Turn stored records into what gets shown.

The distinction this module exists to protect: what a statement tells you is
the *billed* amount for a closed cycle, not your live outstanding balance.
Spending since the statement date is not in here, so every view carries the
statement date, the source and an as-of timestamp.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from . import config, db
from .models import SOURCE_PRIORITY, Card, StatementRecord, Transaction

STATUS_NO_DATA = "no_data"
STATUS_SETTLED = "settled"
STATUS_OVERDUE = "overdue"
STATUS_DUE_TODAY = "due_today"
STATUS_DUE_SOON = "due_soon"
STATUS_UPCOMING = "upcoming"
STATUS_UNKNOWN_DUE_DATE = "unknown_due_date"

DUE_SOON_DAYS = 3


@dataclass
class DueView:
    """Everything the dashboard and CLI need for one card."""

    card: Card
    record: StatementRecord | None
    paid_since_statement: float = 0.0
    today: date = None  # type: ignore[assignment]
    # Every stored statement for this card, newest cycle first.
    history: list[StatementRecord] = field(default_factory=list)
    # Line items of the statement being shown, when they could be read.
    transactions: list[Transaction] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.today is None:
            self.today = date.today()

    @property
    def has_data(self) -> bool:
        return self.record is not None

    @property
    def transactions_total(self) -> float:
        """What the line items add up to, refunds and payments netted off."""
        return round(sum(txn.signed_amount for txn in self.transactions), 2)

    @property
    def category_spend_totals(self) -> list[tuple[str, float]]:
        """Debit spend by category for the statement being shown."""
        from .categories import category_spend_totals

        return category_spend_totals(self.transactions)

    @property
    def transactions_missing(self) -> bool:
        """True when the bill looks active but no line items were stored."""
        from .parse_quality import transactions_missing_for_record

        if self.record is None:
            return False
        return transactions_missing_for_record(
            total_due=self.record.total_due, txn_count=len(self.transactions)
        )

    @property
    def is_latest(self) -> bool:
        """False while an older cycle is being looked at."""
        newest = latest_record(self.history)
        return self.record is None or newest is None or newest.id == self.record.id

    @property
    def billed_amount(self) -> float | None:
        """The total due on the latest statement, before any payments."""
        return self.record.total_due if self.record else None

    @property
    def outstanding(self) -> float | None:
        """Billed amount less payments recorded since that statement.

        Still not a live balance: it excludes anything spent after the
        statement date.
        """
        if self.record is None:
            return None
        return round(self.record.total_due - self.paid_since_statement, 2)

    @property
    def min_due(self) -> float | None:
        return self.record.min_due if self.record else None

    @property
    def min_due_remaining(self) -> float | None:
        if self.record is None or self.record.min_due is None:
            return None
        return max(0.0, round(self.record.min_due - self.paid_since_statement, 2))

    @property
    def due_date(self) -> date | None:
        return self.record.due_date if self.record else None

    @property
    def statement_date(self) -> date | None:
        return self.record.statement_date if self.record else None

    @property
    def credit_limit(self) -> float | None:
        if self.record and self.record.credit_limit is not None:
            return self.record.credit_limit
        return self.card.credit_limit

    @property
    def available_credit(self) -> float | None:
        if self.record and self.record.available_credit is not None:
            return self.record.available_credit
        limit = self.credit_limit
        outstanding = self.outstanding
        if limit is None or outstanding is None:
            return None
        return round(limit - outstanding, 2)

    @property
    def utilisation(self) -> float | None:
        limit = self.credit_limit
        outstanding = self.outstanding
        if not limit or outstanding is None or limit <= 0:
            return None
        return round(max(0.0, outstanding) / limit * 100, 1)

    @property
    def days_to_due(self) -> int | None:
        return (self.due_date - self.today).days if self.due_date else None

    @property
    def status(self) -> str:
        if self.record is None:
            return STATUS_NO_DATA
        outstanding = self.outstanding or 0.0
        if outstanding <= 0:
            return STATUS_SETTLED
        days = self.days_to_due
        if days is None:
            return STATUS_UNKNOWN_DUE_DATE
        if days < 0:
            return STATUS_OVERDUE
        if days == 0:
            return STATUS_DUE_TODAY
        if days <= DUE_SOON_DAYS:
            return STATUS_DUE_SOON
        return STATUS_UPCOMING

    @property
    def source(self) -> str | None:
        return self.record.source if self.record else None

    @property
    def as_of(self) -> datetime | None:
        return self.record.as_of if self.record else None

    @property
    def age_days(self) -> int | None:
        """How old the underlying statement is."""
        reference = self.statement_date or (self.as_of.date() if self.as_of else None)
        return (self.today - reference).days if reference else None

    @property
    def is_stale(self) -> bool:
        age = self.age_days
        return age is not None and age > config.STALE_AFTER_DAYS

    @property
    def freshness_note(self) -> str:
        if self.record is None:
            return "No statement parsed yet. Add the figure manually or run an ingest."
        if self.statement_date is None:
            return "Billed amount with no statement date on it, so its age is unknown."
        age = self.age_days
        if age is None:
            return "Billed amount; statement date unknown."
        if self.is_stale:
            return f"Statement is {age} days old, so a newer bill has probably been generated."
        return f"Billed amount as of the statement {age} days ago; excludes later spending."


def _rank(record: StatementRecord) -> tuple:
    # Undated junk (agreements / T&Cs mis-parsed) must not beat a real cycle
    # just because it was ingested later.
    has_cycle_date = 1 if record.statement_date is not None else 0
    anchor = record.statement_date or (record.as_of.date() if record.as_of else date.min)
    return (
        has_cycle_date,
        anchor,
        SOURCE_PRIORITY.get(record.source, 0),
        record.as_of or datetime.min,
    )


def latest_record(records: list[StatementRecord]) -> StatementRecord | None:
    """The record that should be believed: newest cycle, manual entry winning ties."""
    return max(records, key=_rank) if records else None


def view_for_card(
    conn: sqlite3.Connection,
    card: Card,
    *,
    today: date | None = None,
    record_id: int | None = None,
) -> DueView:
    """The card as it stands, or as one chosen statement left it.

    `record_id` is how the dashboard looks back at an earlier cycle; without it
    the newest statement is what the card shows.
    """
    records = db.statements_for_card(conn, card.id)
    record = next((r for r in records if r.id == record_id), None) if record_id else None
    if record is None:
        record = latest_record(records)
    paid = db.payments_since(conn, card.id, record.statement_date) if record else 0.0
    return DueView(
        card=card,
        record=record,
        paid_since_statement=paid,
        today=today or date.today(),
        history=records,
        transactions=(
            db.transactions_for_statement(conn, record.id) if record and record.id else []
        ),
    )


def all_views(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    selected: dict[int, int] | None = None,
) -> list[DueView]:
    """Every card, each showing its latest statement unless one was chosen."""
    chosen = selected or {}
    views = [
        view_for_card(conn, card, today=today, record_id=chosen.get(card.id))
        for card in db.list_cards(conn)
    ]
    return sorted(views, key=_sort_key)


def _sort_key(view: DueView) -> tuple:
    order = {
        STATUS_OVERDUE: 0,
        STATUS_DUE_TODAY: 1,
        STATUS_DUE_SOON: 2,
        STATUS_UPCOMING: 3,
        STATUS_UNKNOWN_DUE_DATE: 4,
        STATUS_SETTLED: 5,
        STATUS_NO_DATA: 6,
    }
    days = view.days_to_due
    return (order.get(view.status, 9), days if days is not None else 9999, view.card.label)


@dataclass
class Portfolio:
    views: list[DueView]

    @property
    def total_outstanding(self) -> float:
        return round(sum(v.outstanding or 0.0 for v in self.views), 2)

    @property
    def total_min_due(self) -> float:
        return round(sum(v.min_due_remaining or 0.0 for v in self.views), 2)

    @property
    def total_limit(self) -> float:
        return round(sum(v.credit_limit or 0.0 for v in self.views), 2)

    @property
    def utilisation(self) -> float | None:
        if self.total_limit <= 0:
            return None
        return round(self.total_outstanding / self.total_limit * 100, 1)

    @property
    def next_due(self) -> DueView | None:
        pending = [
            v
            for v in self.views
            if v.due_date and v.status not in (STATUS_SETTLED, STATUS_NO_DATA)
        ]
        return min(pending, key=lambda v: v.due_date) if pending else None

    @property
    def attention(self) -> list[DueView]:
        return [
            v
            for v in self.views
            if v.status in (STATUS_OVERDUE, STATUS_DUE_TODAY, STATUS_DUE_SOON) or v.is_stale
        ]


def portfolio(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    selected: dict[int, int] | None = None,
) -> Portfolio:
    return Portfolio(views=all_views(conn, today=today, selected=selected))


def format_inr(amount: float | None) -> str:
    """Indian digit grouping: 12,34,567.89."""
    if amount is None:
        return "—"
    negative = amount < 0
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    rendered = f"₹{whole}.{fraction}"
    return f"-{rendered}" if negative else rendered
