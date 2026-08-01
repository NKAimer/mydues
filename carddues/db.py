"""SQLite storage. Everything stays on this machine."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import config
from .models import CATEGORY_USER, Card, Expense, StatementRecord, Transaction

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

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    spent_on TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT NOT NULL,
    category TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

-- Deduplicate keyed imports (Gmail message ids); manuals leave source_ref null.
CREATE UNIQUE INDEX IF NOT EXISTS expenses_source_ref
    ON expenses (source, source_ref)
    WHERE source_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS merchant_categories (
    merchant_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_phrases (
    id INTEGER PRIMARY KEY,
    phrase TEXT NOT NULL COLLATE NOCASE,
    category TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (phrase)
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
    # timeout + WAL: dashboard reloads during SSE ingest must not crash with
    # "database is locked" when another thread is writing.
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()
    # Parser once mistook an HDFC alternate account number for last4 0722 / 2476.
    retarget_card_last4(conn, issuer="hdfc", from_last4="0722", to_last4="2750")
    retarget_card_last4(conn, issuer="hdfc", from_last4="2476", to_last4="6527")
    # ICICI demat e-statement was once auto-registered as HDFC ••1189.
    purge_false_card(conn, issuer="hdfc", last4="1189")
    # ICICI savings e-statement was once auto-registered as ICICI ••0001.
    purge_false_card(conn, issuer="icici", last4="0001")
    # One-shot cleanups: only write when something still matches, so a page
    # reload during SSE ingest does not fight for a write lock every time.
    if conn.execute(
        "SELECT 1 FROM statements WHERE source_ref = 'test' LIMIT 1"
    ).fetchone():
        conn.execute("DELETE FROM statements WHERE source_ref = 'test'")
    junk = conn.execute(
        """
        SELECT 1 FROM statements
        WHERE statement_date IS NULL AND due_date IS NULL
          AND ABS(total_due) <= 500
        LIMIT 1
        """
    ).fetchone()
    if junk:
        conn.execute(
            """
            DELETE FROM transactions WHERE statement_id IN (
                SELECT id FROM statements
                WHERE statement_date IS NULL AND due_date IS NULL
                  AND ABS(total_due) <= 500
            )
            """
        )
        conn.execute(
            """
            DELETE FROM statements
            WHERE statement_date IS NULL AND due_date IS NULL
              AND ABS(total_due) <= 500
            """
        )
    seed_category_phrases_from_builtins(conn)
    conn.commit()


def seed_category_phrases_from_builtins(conn: sqlite3.Connection) -> int:
    """Insert CATEGORY_KEYWORDS into category_phrases when that table is empty.

    One row per keyword string. Returns how many rows were inserted.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM category_phrases").fetchone()
    if row and int(row["n"]) > 0:
        return 0
    # Imported here to avoid a circular import at module load.
    from .categories import CATEGORY_KEYWORDS

    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            phrase = (keyword or "").strip().lower()
            if not phrase:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO category_phrases (phrase, category, updated_at)
                VALUES (?, ?, ?)
                """,
                (phrase, category, now),
            )
            inserted += cursor.rowcount or 0
    return inserted


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


def delete_statements_for_source_ref(conn: sqlite3.Connection, source_ref: str) -> int:
    """Remove statement rows (and their transactions) for one mail attachment."""
    rows = conn.execute(
        "SELECT id FROM statements WHERE source_ref = ?", (source_ref,)
    ).fetchall()
    if not rows:
        return 0
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM transactions WHERE statement_id IN ({placeholders})", ids)
    conn.execute("DELETE FROM statements WHERE source_ref = ?", (source_ref,))
    conn.commit()
    return len(ids)


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
    attachment is removed first — across every card. Correcting a mis-read
    statement_date or last4 must rewrite the cycle, not leave the bad row on
    the wrong card beside the new one.
    """
    if record.source_ref:
        conn.execute(
            "DELETE FROM statements WHERE source = ? AND source_ref = ?",
            (record.source, record.source_ref),
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


def statements_for_source_ref(
    conn: sqlite3.Connection, source_ref: str
) -> list[StatementRecord]:
    rows = conn.execute(
        "SELECT * FROM statements WHERE source_ref = ? ORDER BY id DESC",
        (source_ref,),
    ).fetchall()
    return [_row_to_statement(row) for row in rows]


def transaction_count_for_statement(conn: sqlite3.Connection, statement_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


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


def update_transaction_category(
    conn: sqlite3.Connection, txn_id: int, category: str | None
) -> tuple[Transaction, int] | None:
    """Set category (NULL if cleared) and category_source to user / NULL.

    Returns `(txn, card_id)` so the route can teach merchant memory and redirect
    to `#card-{id}`, or None if the row is missing.
    """
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        return None
    category = (category or "").strip() or None
    source = CATEGORY_USER if category else None
    conn.execute(
        "UPDATE transactions SET category = ?, category_source = ? WHERE id = ?",
        (category, source, txn_id),
    )
    conn.commit()
    return (
        Transaction(
            id=row["id"],
            txn_date=_as_date(row["txn_date"]),
            description=row["description"],
            amount=row["amount"],
            kind=row["kind"],
            category=category,
            category_source=source,
        ),
        int(row["card_id"]),
    )


def add_payment(
    conn: sqlite3.Connection, card_id: int, amount: float, paid_on: date, note: str | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO payments (card_id, amount, paid_on, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (card_id, amount, _iso(paid_on), note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _expense_from_row(row: sqlite3.Row) -> Expense:
    return Expense(
        id=row["id"],
        spent_on=_as_date(row["spent_on"]) or date.today(),
        amount=float(row["amount"]),
        description=row["description"],
        category=row["category"],
        source=row["source"],
        source_ref=row["source_ref"],
        note=row["note"],
        created_at=_as_datetime(row["created_at"]),
    )


def add_expense(conn: sqlite3.Connection, expense: Expense) -> int | None:
    """Insert an expense. Returns id, or None when a keyed duplicate already exists."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO expenses (
                spent_on, amount, description, category, source, source_ref, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(expense.spent_on),
                expense.amount,
                expense.description.strip(),
                expense.category,
                expense.source,
                expense.source_ref,
                expense.note,
                (expense.created_at or datetime.now()).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    except sqlite3.IntegrityError:
        return None


def list_expenses(
    conn: sqlite3.Connection, *, start: date, end: date
) -> list[Expense]:
    """Expenses with spent_on in [start, end)."""
    rows = conn.execute(
        """
        SELECT * FROM expenses
        WHERE spent_on >= ? AND spent_on < ?
        ORDER BY spent_on DESC, id DESC
        """,
        (_iso(start), _iso(end)),
    ).fetchall()
    return [_expense_from_row(row) for row in rows]


def get_expense(conn: sqlite3.Connection, expense_id: int) -> Expense | None:
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    return _expense_from_row(row) if row else None


def update_expense(
    conn: sqlite3.Connection,
    expense_id: int,
    *,
    spent_on: date,
    amount: float,
    description: str,
    category: str | None,
    note: str | None = None,
) -> bool:
    """Update editable fields; leaves source / source_ref alone. True if a row changed."""
    cursor = conn.execute(
        """
        UPDATE expenses
        SET spent_on = ?, amount = ?, description = ?, category = ?, note = ?
        WHERE id = ?
        """,
        (_iso(spent_on), amount, description.strip(), category, note, expense_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def delete_expense(conn: sqlite3.Connection, expense_id: int) -> bool:
    cursor = conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    return cursor.rowcount > 0


def expense_total(conn: sqlite3.Connection, *, start: date, end: date) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total FROM expenses
        WHERE spent_on >= ? AND spent_on < ?
        """,
        (_iso(start), _iso(end)),
    ).fetchone()
    return float(row["total"])


def expense_months(conn: sqlite3.Connection) -> list[str]:
    """YYYY-MM keys that have at least one expense, newest first."""
    rows = conn.execute(
        """
        SELECT DISTINCT strftime('%Y-%m', spent_on) AS month
        FROM expenses
        WHERE spent_on IS NOT NULL
        ORDER BY month DESC
        """
    ).fetchall()
    return [row["month"] for row in rows if row["month"]]


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


def list_category_phrases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, phrase, category, updated_at FROM category_phrases "
        "ORDER BY length(phrase) DESC, phrase COLLATE NOCASE"
    ).fetchall()


def upsert_category_phrase(
    conn: sqlite3.Connection, phrase: str, category: str
) -> int | None:
    """Insert or refresh a phrase→category rule. Returns row id, or None if blank."""
    phrase = (phrase or "").strip().lower()
    category = (category or "").strip()
    if not phrase or not category:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO category_phrases (phrase, category, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (phrase) DO UPDATE SET
            category = excluded.category,
            updated_at = excluded.updated_at
        """,
        (phrase, category, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM category_phrases WHERE phrase = ? COLLATE NOCASE", (phrase,)
    ).fetchone()
    return int(row["id"]) if row else None


def update_category_phrase(
    conn: sqlite3.Connection,
    phrase_id: int,
    *,
    phrase: str,
    category: str,
) -> bool:
    """Update phrase and category by id. True if a row changed."""
    phrase = (phrase or "").strip().lower()
    category = (category or "").strip()
    if not phrase or not category:
        return False
    try:
        cursor = conn.execute(
            """
            UPDATE category_phrases
            SET phrase = ?, category = ?, updated_at = ?
            WHERE id = ?
            """,
            (phrase, category, datetime.now().isoformat(timespec="seconds"), phrase_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.IntegrityError:
        return False


def delete_category_phrase(conn: sqlite3.Connection, phrase_id: int) -> bool:
    cursor = conn.execute("DELETE FROM category_phrases WHERE id = ?", (phrase_id,))
    conn.commit()
    return cursor.rowcount > 0


def list_merchant_categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT merchant_key, category, updated_at FROM merchant_categories "
        "ORDER BY merchant_key COLLATE NOCASE"
    ).fetchall()


def upsert_merchant_category_key(
    conn: sqlite3.Connection, merchant_key: str, category: str
) -> bool:
    """Insert or refresh a learned merchant mapping by its stored key."""
    merchant_key = (merchant_key or "").strip()
    category = (category or "").strip()
    if not merchant_key or not category:
        return False
    conn.execute(
        """
        INSERT INTO merchant_categories (merchant_key, category, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (merchant_key) DO UPDATE SET
            category = excluded.category,
            updated_at = excluded.updated_at
        """,
        (merchant_key, category, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return True


def update_merchant_category(
    conn: sqlite3.Connection, merchant_key: str, category: str
) -> bool:
    """Change the category for an existing merchant key. True if a row changed."""
    category = (category or "").strip()
    if not merchant_key or not category:
        return False
    cursor = conn.execute(
        """
        UPDATE merchant_categories
        SET category = ?, updated_at = ?
        WHERE merchant_key = ?
        """,
        (category, datetime.now().isoformat(timespec="seconds"), merchant_key),
    )
    conn.commit()
    return cursor.rowcount > 0


def delete_merchant_category(conn: sqlite3.Connection, merchant_key: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM merchant_categories WHERE merchant_key = ?", (merchant_key,)
    )
    conn.commit()
    return cursor.rowcount > 0
