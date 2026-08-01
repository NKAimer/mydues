"""SQLite storage. Everything stays on this machine."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import config
from .models import Card, StatementRecord, Transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    issuer TEXT NOT NULL,
    label TEXT NOT NULL,
    last4 TEXT NOT NULL,
    credit_limit REAL,
    dob TEXT,
    name TEXT,
    pan TEXT,
    extra_passwords TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (issuer, last4)
);

CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards (id) ON DELETE CASCADE,
    statement_date TEXT,
    due_date TEXT,
    total_due REAL NOT NULL,
    min_due REAL,
    credit_limit REAL,
    available_credit REAL,
    source TEXT NOT NULL,
    source_ref TEXT,
    parser TEXT,
    confidence REAL,
    note TEXT,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS statements_unique
    ON statements (card_id, source, IFNULL(statement_date, ''), IFNULL(source_ref, ''));

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards (id) ON DELETE CASCADE,
    statement_id INTEGER NOT NULL REFERENCES statements (id) ON DELETE CASCADE,
    txn_date TEXT,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    kind TEXT NOT NULL,
    category TEXT,
    category_source TEXT,
    created_at TEXT NOT NULL
);

-- Re-reading the same statement must not double its line items.
CREATE UNIQUE INDEX IF NOT EXISTS transactions_unique
    ON transactions (statement_id, IFNULL(txn_date, ''), description, amount);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards (id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    paid_on TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY,
    message_id TEXT,
    filename TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (message_id, filename)
);
"""

# Columns added after the first release. SQLite cannot express these with
# CREATE TABLE IF NOT EXISTS, so existing databases are patched on init.
ADDED_COLUMNS = {
    "ingest_log": {
        "sender": "TEXT",
        "subject": "TEXT",
        "received_at": "TEXT",
        "issuer": "TEXT",
        "password_rule": "TEXT",
    }
}


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    target = path or config.db_path()
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()
    # Parser once mistook an HDFC alternate account number for last4 0722.
    retarget_card_last4(conn, issuer="hdfc", from_last4="0722", to_last4="2750")
    # ICICI demat e-statement was once auto-registered as HDFC ••1189.
    purge_false_card(conn, issuer="hdfc", last4="1189")


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _as_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def add_card(conn: sqlite3.Connection, card: Card) -> int:
    cursor = conn.execute(
        """
        INSERT INTO cards (issuer, label, last4, credit_limit, dob, name, pan,
                           extra_passwords, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (issuer, last4) DO UPDATE SET
            label = excluded.label,
            credit_limit = COALESCE(excluded.credit_limit, cards.credit_limit),
            dob = COALESCE(excluded.dob, cards.dob),
            name = COALESCE(excluded.name, cards.name),
            pan = COALESCE(excluded.pan, cards.pan),
            extra_passwords = excluded.extra_passwords
        """,
        (
            card.issuer,
            card.label,
            card.last4,
            card.credit_limit,
            _iso(card.dob),
            card.name,
            card.pan,
            json.dumps(card.extra_passwords),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    if cursor.lastrowid:
        return cursor.lastrowid
    row = conn.execute(
        "SELECT id FROM cards WHERE issuer = ? AND last4 = ?", (card.issuer, card.last4)
    ).fetchone()
    return int(row["id"])


def _row_to_card(row: sqlite3.Row) -> Card:
    return Card(
        id=row["id"],
        issuer=row["issuer"],
        label=row["label"],
        last4=row["last4"],
        credit_limit=row["credit_limit"],
        dob=_as_date(row["dob"]),
        name=row["name"],
        pan=row["pan"],
        extra_passwords=json.loads(row["extra_passwords"] or "[]"),
    )


def list_cards(conn: sqlite3.Connection) -> list[Card]:
    rows = conn.execute("SELECT * FROM cards ORDER BY label").fetchall()
    return [_row_to_card(row) for row in rows]


def get_card(conn: sqlite3.Connection, card_id: int) -> Card | None:
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _row_to_card(row) if row else None


def find_card(conn: sqlite3.Connection, *, issuer: str | None, last4: str) -> Card | None:
    if issuer:
        row = conn.execute(
            "SELECT * FROM cards WHERE issuer = ? AND last4 = ?", (issuer, last4)
        ).fetchone()
        if row:
            return _row_to_card(row)
    rows = conn.execute("SELECT * FROM cards WHERE last4 = ?", (last4,)).fetchall()
    return _row_to_card(rows[0]) if len(rows) == 1 else None


def remember_password(conn: sqlite3.Connection, card_id: int, password: str) -> None:
    """Keep a password that worked, so next month's statement opens unattended."""
    row = conn.execute("SELECT extra_passwords FROM cards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        return
    known = json.loads(row["extra_passwords"] or "[]")
    if password in known:
        return
    known.append(password)
    conn.execute(
        "UPDATE cards SET extra_passwords = ? WHERE id = ?", (json.dumps(known), card_id)
    )
    conn.commit()


def delete_card(conn: sqlite3.Connection, card_id: int) -> None:
    conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()


def purge_false_card(conn: sqlite3.Connection, *, issuer: str, last4: str) -> bool:
    """Delete a card that was created from a non-statement PDF."""
    card = find_card(conn, issuer=issuer, last4=last4)
    if card is None:
        return False
    delete_card(conn, card.id)
    return True


def retarget_card_last4(
    conn: sqlite3.Connection,
    *,
    issuer: str,
    from_last4: str,
    to_last4: str,
) -> bool:
    """Rename a mis-read last4, or merge it into the real card if that exists.

    Returns True when something changed.
    """
    bogus = find_card(conn, issuer=issuer, last4=from_last4)
    if bogus is None:
        return False
    target = find_card(conn, issuer=issuer, last4=to_last4)
    if target is None:
        label = bogus.label or ""
        if from_last4 in label:
            label = label.replace(from_last4, to_last4)
        elif "••" in label:
            label = re.sub(r"••\d{4}\b", f"••{to_last4}", label)
        else:
            label = f"{label} ••{to_last4}".strip()
        conn.execute(
            "UPDATE cards SET last4 = ?, label = ? WHERE id = ?",
            (to_last4, label, bogus.id),
        )
        conn.commit()
        return True

    if target.id == bogus.id:
        return False

    # Drop attachments already present on the real card, then move the rest.
    conn.execute(
        """
        DELETE FROM statements
        WHERE card_id = ?
          AND source_ref IS NOT NULL
          AND source_ref IN (
              SELECT source_ref FROM statements
              WHERE card_id = ? AND source_ref IS NOT NULL
          )
        """,
        (bogus.id, target.id),
    )
    for table in ("statements", "payments", "transactions"):
        conn.execute(
            f"UPDATE {table} SET card_id = ? WHERE card_id = ?",
            (target.id, bogus.id),
        )
    conn.execute("DELETE FROM cards WHERE id = ?", (bogus.id,))
    conn.commit()
    return True


def save_statement(conn: sqlite3.Connection, record: StatementRecord) -> int:
    """Store or refresh one statement and return its id.

    The id is looked up rather than taken from the cursor, because an upsert
    that updates an existing cycle leaves no reliable last row id behind.

    When `source_ref` is set (a Gmail message id), any prior row for that
    attachment is removed first. Correcting a mis-read statement_date must
    rewrite the cycle, not leave the bad dates beside the new ones.
    """
    if record.source_ref:
        conn.execute(
            "DELETE FROM statements WHERE card_id = ? AND source = ? AND source_ref = ?",
            (record.card_id, record.source, record.source_ref),
        )
    conn.execute(
        """
        INSERT INTO statements (card_id, statement_date, due_date, total_due, min_due,
                                credit_limit, available_credit, source, source_ref,
                                parser, confidence, note, as_of, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (card_id, source, IFNULL(statement_date, ''), IFNULL(source_ref, ''))
        DO UPDATE SET
            due_date = excluded.due_date,
            total_due = excluded.total_due,
            min_due = excluded.min_due,
            credit_limit = excluded.credit_limit,
            available_credit = excluded.available_credit,
            parser = excluded.parser,
            confidence = excluded.confidence,
            note = excluded.note,
            as_of = excluded.as_of
        """,
        (
            record.card_id,
            _iso(record.statement_date),
            _iso(record.due_date),
            record.total_due,
            record.min_due,
            record.credit_limit,
            record.available_credit,
            record.source,
            record.source_ref,
            record.parser,
            record.confidence,
            record.note,
            _iso(record.as_of),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM statements WHERE card_id = ? AND source = ? "
        "AND IFNULL(statement_date, '') = ? AND IFNULL(source_ref, '') = ?",
        (
            record.card_id,
            record.source,
            _iso(record.statement_date) or "",
            record.source_ref or "",
        ),
    ).fetchone()
    return int(row["id"])


def _row_to_statement(row: sqlite3.Row) -> StatementRecord:
    return StatementRecord(
        id=row["id"],
        card_id=row["card_id"],
        statement_date=_as_date(row["statement_date"]),
        due_date=_as_date(row["due_date"]),
        total_due=row["total_due"],
        min_due=row["min_due"],
        credit_limit=row["credit_limit"],
        available_credit=row["available_credit"],
        source=row["source"],
        source_ref=row["source_ref"],
        parser=row["parser"],
        confidence=row["confidence"],
        note=row["note"],
        as_of=_as_datetime(row["as_of"]),
    )


def statements_for_card(conn: sqlite3.Connection, card_id: int) -> list[StatementRecord]:
    rows = conn.execute(
        "SELECT * FROM statements WHERE card_id = ? ORDER BY IFNULL(statement_date, as_of) DESC",
        (card_id,),
    ).fetchall()
    return [_row_to_statement(row) for row in rows]


def get_statement(conn: sqlite3.Connection, statement_id: int) -> StatementRecord | None:
    row = conn.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
    return _row_to_statement(row) if row else None


def save_transactions(
    conn: sqlite3.Connection,
    card_id: int,
    statement_id: int,
    rows: Iterable[Transaction],
) -> int:
    """Store a statement's line items, ignoring ones already recorded."""
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    for txn in rows:
        cursor = conn.execute(
            """
            INSERT INTO transactions (card_id, statement_id, txn_date, description, amount,
                                      kind, category, category_source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                card_id,
                statement_id,
                _iso(txn.txn_date),
                txn.description,
                txn.amount,
                txn.kind,
                txn.category,
                txn.category_source,
                now,
            ),
        )
        saved += cursor.rowcount or 0
    conn.commit()
    return saved


def transactions_for_statement(conn: sqlite3.Connection, statement_id: int) -> list[Transaction]:
    """Line items in the order they were billed, undated ones last."""
    rows = conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ? "
        "ORDER BY txn_date IS NULL, txn_date, id",
        (statement_id,),
    ).fetchall()
    return [
        Transaction(
            id=row["id"],
            txn_date=_as_date(row["txn_date"]),
            description=row["description"],
            amount=row["amount"],
            kind=row["kind"],
            category=row["category"],
            category_source=row["category_source"],
        )
        for row in rows
    ]


def add_payment(
    conn: sqlite3.Connection, card_id: int, amount: float, paid_on: date, note: str | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO payments (card_id, amount, paid_on, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (card_id, amount, _iso(paid_on), note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def payments_since(conn: sqlite3.Connection, card_id: int, since: date | None) -> float:
    if since is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE card_id = ?",
            (card_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payments "
            "WHERE card_id = ? AND paid_on >= ?",
            (card_id, _iso(since)),
        ).fetchone()
    return float(row["total"])


def log_ingest(
    conn: sqlite3.Connection,
    *,
    message_id: str | None,
    filename: str | None,
    status: str,
    detail: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    received_at: datetime | None = None,
    issuer: str | None = None,
    password_rule: str | None = None,
) -> None:
    """Record what happened to an attachment, with enough of the mail to act on it."""
    conn.execute(
        """
        INSERT INTO ingest_log (message_id, filename, status, detail, sender, subject,
                                received_at, issuer, password_rule, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (message_id, filename) DO UPDATE SET
            status = excluded.status,
            detail = excluded.detail,
            created_at = excluded.created_at,
            -- A retry knows nothing about the mail; keep what the fetch recorded.
            sender = COALESCE(excluded.sender, ingest_log.sender),
            subject = COALESCE(excluded.subject, ingest_log.subject),
            received_at = COALESCE(excluded.received_at, ingest_log.received_at),
            issuer = COALESCE(excluded.issuer, ingest_log.issuer),
            password_rule = COALESCE(excluded.password_rule, ingest_log.password_rule)
        """,
        (
            message_id,
            filename,
            status,
            detail,
            sender,
            subject,
            _iso(received_at),
            issuer,
            password_rule,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def seen_attachments(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT message_id, filename FROM ingest_log WHERE status IN ('parsed', 'skipped')"
    ).fetchall()
    return {(row["message_id"], row["filename"]) for row in rows}


def recent_ingest_log(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ingest_log ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


PENDING_STATUSES = "status IN ('locked', 'unparsed', 'error')"


def pending_ingest(
    conn: sqlite3.Connection, *, issuer: str | None = None, limit: int | None = None
) -> list[sqlite3.Row]:
    """Attachments still to be opened, by the date the mail arrived.

    Filtering by issuer keeps one card's backlog small, but rows logged before
    the issuer was recorded have to come along or they would never be retried.
    """
    where = PENDING_STATUSES
    params: list = []
    if issuer:
        where += " AND (issuer IS NULL OR issuer = ?)"
        params.append(issuer)
    sql = f"SELECT * FROM ingest_log WHERE {where} ORDER BY COALESCE(received_at, created_at) DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def count_pending_ingest(conn: sqlite3.Connection, *, issuer: str | None = None) -> int:
    where = PENDING_STATUSES
    params: list = []
    if issuer:
        where += " AND (issuer IS NULL OR issuer = ?)"
        params.append(issuer)
    return conn.execute(
        f"SELECT COUNT(*) AS total FROM ingest_log WHERE {where}", params
    ).fetchone()["total"]


def unresolved_ingest(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    return pending_ingest(conn, limit=limit)


def count_unresolved_ingest(conn: sqlite3.Connection) -> int:
    return count_pending_ingest(conn)


def parsed_ingest(
    conn: sqlite3.Connection, *, issuer: str | None = None, limit: int | None = None
) -> list[sqlite3.Row]:
    """Already-parsed mail attachments, newest first — for a forced re-read."""
    where = "status = 'parsed' AND message_id IS NOT NULL AND filename IS NOT NULL"
    params: list = []
    if issuer:
        where += " AND (issuer IS NULL OR issuer = ?)"
        params.append(issuer)
    sql = (
        f"SELECT * FROM ingest_log WHERE {where} "
        "ORDER BY COALESCE(received_at, created_at) DESC"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def count_parsed_ingest(conn: sqlite3.Connection, *, issuer: str | None = None) -> int:
    where = "status = 'parsed' AND message_id IS NOT NULL AND filename IS NOT NULL"
    params: list = []
    if issuer:
        where += " AND (issuer IS NULL OR issuer = ?)"
        params.append(issuer)
    return conn.execute(
        f"SELECT COUNT(*) AS total FROM ingest_log WHERE {where}", params
    ).fetchone()["total"]
