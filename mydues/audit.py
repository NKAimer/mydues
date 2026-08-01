"""Cross-card checks so weak parses surface in one place."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db, dues
from .parse_quality import transactions_missing_for_record


@dataclass(frozen=True)
class AuditFinding:
    card_id: int
    card_label: str
    kind: str
    detail: str


def audit_cards(conn: sqlite3.Connection) -> list[AuditFinding]:
    """Findings for every registered card's latest statement plus ingest backlog."""
    findings: list[AuditFinding] = []
    for view in dues.all_views(conn):
        label = view.card.label
        card_id = view.card.id or 0
        if not view.has_data:
            findings.append(
                AuditFinding(card_id, label, "no_data", "No statement stored yet")
            )
            continue
        record = view.record
        assert record is not None
        if record.due_date is None:
            findings.append(
                AuditFinding(card_id, label, "missing_due_date", "Payment due date missing")
            )
        if record.min_due is None:
            findings.append(
                AuditFinding(card_id, label, "missing_min_due", "Minimum due missing")
            )
        if transactions_missing_for_record(
            total_due=record.total_due, txn_count=len(view.transactions)
        ):
            findings.append(
                AuditFinding(
                    card_id,
                    label,
                    "transactions_missing",
                    f"Non-zero bill ({record.total_due:.2f}) has no line items",
                )
            )
        if record.note and "transaction" in record.note.lower():
            findings.append(
                AuditFinding(card_id, label, "parse_note", record.note)
            )

    pending = db.count_pending_ingest(conn)
    if pending:
        findings.append(
            AuditFinding(
                0,
                "(ingest)",
                "pending_ingest",
                f"{pending} attachment(s) waiting (locked/unparsed/error)",
            )
        )
    return findings
