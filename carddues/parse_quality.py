"""Shared completeness checks for parsed statements.

Ingest, reparse, audit, and golden tests all use the same rules so a weak
parse cannot look successful on one card while another card is held to a
higher bar.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ParsedStatement, StatementRecord

# Non-zero bills should usually carry line items; below this, an empty txn
# list is fine (settled / credit-only cycles).
_NONEMPTY_BILL = 1.0
_TOTAL_DRIFT = 1.0  # rupees


@dataclass(frozen=True)
class QualityReport:
    """Hard blockers prevent auto-create / trusting a new last4."""

    hard_ok: bool
    issues: tuple[str, ...]
    transactions_incomplete: bool

    @property
    def detail(self) -> str:
        return "; ".join(self.issues) if self.issues else "ok"


def assess(statement: ParsedStatement) -> QualityReport:
    """Score a parse for completeness. Does not decide whether to store."""
    issues: list[str] = []
    if statement.due_date is None:
        issues.append("missing due date")
    if statement.min_due is None:
        issues.append("missing minimum due")
    if not (statement.last4 or statement.card_tail):
        issues.append("missing card digits")
    if statement.statement_date is None:
        issues.append("missing statement date")

    hard_ok = (
        statement.due_date is not None
        and statement.min_due is not None
        and bool(statement.last4 or statement.card_tail)
    )

    bill = abs(statement.total_due or 0.0)
    transactions_incomplete = bill >= _NONEMPTY_BILL and not statement.transactions
    if transactions_incomplete:
        issues.append("no transactions on a non-zero bill")

    return QualityReport(
        hard_ok=hard_ok,
        issues=tuple(issues),
        transactions_incomplete=transactions_incomplete,
    )


def confidence(statement: ParsedStatement) -> float:
    """Share of fields a complete statement should yield, including last4/txns."""
    checks = [
        statement.total_due is not None,
        statement.min_due is not None,
        statement.due_date is not None,
        statement.statement_date is not None,
        bool(statement.last4 or statement.card_tail),
        bool(statement.transactions) or abs(statement.total_due or 0.0) < _NONEMPTY_BILL,
    ]
    return sum(1 for ok in checks if ok) / len(checks)


def reparse_regression(
    previous: StatementRecord,
    *,
    previous_txn_count: int,
    new: ParsedStatement,
) -> str | None:
    """Why a reparse must not overwrite the stored cycle, or None if acceptable."""
    if abs((previous.total_due or 0.0) - (new.total_due or 0.0)) > _TOTAL_DRIFT:
        # Refuse collapsing a large bill into a tiny previous-balance style total.
        old = abs(previous.total_due or 0.0)
        new_abs = abs(new.total_due or 0.0)
        if old >= 500 and new_abs < old * 0.25:
            return (
                f"total due collapsed {previous.total_due:.2f} → {new.total_due:.2f}"
            )

    if previous_txn_count >= 3 and len(new.transactions) < max(1, previous_txn_count // 3):
        return (
            f"transaction count collapsed {previous_txn_count} → {len(new.transactions)}"
        )
    return None


def transactions_missing_for_record(
    *, total_due: float | None, txn_count: int
) -> bool:
    """UI/audit helper: bill looks active but no line items were stored."""
    return abs(total_due or 0.0) >= _NONEMPTY_BILL and txn_count == 0
