"""SQLite storage. Everything stays on this machine."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import config
from .charges import detect_charge_kind
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
    spent_at TEXT,
    amount REAL NOT NULL,
    description TEXT NOT NULL,
    category TEXT,
    category_source TEXT,
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

CREATE TABLE IF NOT EXISTS payee_cues (
    id INTEGER PRIMARY KEY,
    cue TEXT NOT NULL COLLATE NOCASE,
    updated_at TEXT NOT NULL,
    UNIQUE (cue)
);

CREATE TABLE IF NOT EXISTS payee_aliases (
    raw_key TEXT PRIMARY KEY,
    payee TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_budgets (
    category TEXT PRIMARY KEY COLLATE NOCASE,
    amount_limit REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monthly_income (
    month TEXT PRIMARY KEY,
    amount REAL NOT NULL,
    credited_on TEXT,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_expenses (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    amount REAL NOT NULL,
    day_of_month INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planned_expenses (
    id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,
    label TEXT NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_goals (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    target_amount REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_paid (
    recurring_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    PRIMARY KEY (recurring_id, month),
    FOREIGN KEY (recurring_id) REFERENCES recurring_expenses(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    },
    "expenses": {
        "category_source": "TEXT",
        "charge_kind": "TEXT",
        "spent_at": "TEXT",
    },
    "transactions": {
        "charge_kind": "TEXT",
    },
}

META_STATEMENTS_FETCHED = "statements_last_fetched"
META_EXPENSES_FETCHED = "expenses_last_fetched"


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
    _backfill_charge_kinds(conn)
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
    seed_payee_cues_from_builtins(conn)
    merge_categories_seed(conn)
    conn.commit()


def _backfill_charge_kinds(conn: sqlite3.Connection) -> None:
    """One-shot: classify null charge_kind on statement transactions only."""
    for row in conn.execute(
        "SELECT id, description FROM transactions WHERE charge_kind IS NULL"
    ).fetchall():
        kind = detect_charge_kind(row["description"] or "")
        if kind:
            conn.execute(
                "UPDATE transactions SET charge_kind = ? WHERE id = ?",
                (kind, row["id"]),
            )
    # Clear any legacy expense flags — charges are statement-display only.
    conn.execute("UPDATE expenses SET charge_kind = NULL WHERE charge_kind IS NOT NULL")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_meta (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def touch_meta_now(conn: sqlite3.Connection, key: str) -> str:
    stamp = datetime.now().isoformat(timespec="seconds")
    set_meta(conn, key, stamp)
    return stamp


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


def seed_payee_cues_from_builtins(conn: sqlite3.Connection) -> int:
    """Insert default payee lead-in cues when that table is empty."""
    row = conn.execute("SELECT COUNT(*) AS n FROM payee_cues").fetchone()
    if row and int(row["n"]) > 0:
        return 0
    from .expense_ingest import DEFAULT_PAYEE_CUES

    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    for cue in DEFAULT_PAYEE_CUES:
        value = (cue or "").strip().lower()
        if not value:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO payee_cues (cue, updated_at)
            VALUES (?, ?)
            """,
            (value, now),
        )
        inserted += cursor.rowcount or 0
    return inserted


def categories_seed_payload(conn: sqlite3.Connection) -> dict:
    """Snapshot phrase rules, merchant overrides, and payee rules for the seed file."""
    phrases = [
        {"phrase": row["phrase"], "category": row["category"]}
        for row in conn.execute(
            "SELECT phrase, category FROM category_phrases "
            "ORDER BY phrase COLLATE NOCASE"
        )
    ]
    merchants = [
        {"merchant_key": row["merchant_key"], "category": row["category"]}
        for row in conn.execute(
            "SELECT merchant_key, category FROM merchant_categories "
            "ORDER BY merchant_key COLLATE NOCASE"
        )
    ]
    payee_cues = [
        {"cue": row["cue"]}
        for row in conn.execute(
            "SELECT cue FROM payee_cues ORDER BY cue COLLATE NOCASE"
        )
    ]
    payee_aliases = [
        {"raw_key": row["raw_key"], "payee": row["payee"]}
        for row in conn.execute(
            "SELECT raw_key, payee FROM payee_aliases "
            "ORDER BY raw_key COLLATE NOCASE"
        )
    ]
    return {
        "phrases": phrases,
        "merchants": merchants,
        "payee_cues": payee_cues,
        "payee_aliases": payee_aliases,
    }


def write_categories_seed(
    conn: sqlite3.Connection, path: Path | None = None
) -> Path:
    """Write the live category tables to the seed JSON. Returns the path used."""
    target = path or config.categories_seed_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = categories_seed_payload(conn)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def merge_categories_seed(
    conn: sqlite3.Connection, path: Path | None = None
) -> tuple[int, int, int, int]:
    """Add missing seed rows. Local rows win on key conflicts.

    Returns ``(phrases, merchants, payee_cues, payee_aliases)`` inserted counts.
    """
    target = path or config.categories_seed_path()
    if not target.is_file():
        return 0, 0, 0, 0
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, 0, 0
    if not isinstance(payload, dict):
        return 0, 0, 0, 0

    now = datetime.now().isoformat(timespec="seconds")
    phrases_inserted = 0
    for item in payload.get("phrases") or []:
        if not isinstance(item, dict):
            continue
        phrase = (item.get("phrase") or "").strip().lower()
        category = (item.get("category") or "").strip()
        if not phrase or not category:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO category_phrases (phrase, category, updated_at)
            VALUES (?, ?, ?)
            """,
            (phrase, category, now),
        )
        phrases_inserted += cursor.rowcount or 0

    merchants_inserted = 0
    for item in payload.get("merchants") or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("merchant_key") or "").strip()
        category = (item.get("category") or "").strip()
        if not key or not category:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO merchant_categories
                (merchant_key, category, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, category, now),
        )
        merchants_inserted += cursor.rowcount or 0

    cues_inserted = 0
    for item in payload.get("payee_cues") or []:
        if not isinstance(item, dict):
            continue
        cue = (item.get("cue") or "").strip().lower()
        if not cue:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO payee_cues (cue, updated_at)
            VALUES (?, ?)
            """,
            (cue, now),
        )
        cues_inserted += cursor.rowcount or 0

    aliases_inserted = 0
    for item in payload.get("payee_aliases") or []:
        if not isinstance(item, dict):
            continue
        raw_key = (item.get("raw_key") or "").strip().lower()
        payee = (item.get("payee") or "").strip()
        if not raw_key or not payee:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO payee_aliases (raw_key, payee, updated_at)
            VALUES (?, ?, ?)
            """,
            (raw_key, payee, now),
        )
        aliases_inserted += cursor.rowcount or 0

    return phrases_inserted, merchants_inserted, cues_inserted, aliases_inserted


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
        charge_kind = txn.charge_kind or detect_charge_kind(txn.description)
        cursor = conn.execute(
            """
            INSERT INTO transactions (card_id, statement_id, txn_date, description, amount,
                                      kind, category, category_source, charge_kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                charge_kind,
                now,
            ),
        )
        saved += cursor.rowcount or 0
    conn.commit()
    return saved


def _transaction_from_row(row: sqlite3.Row) -> Transaction:
    keys = row.keys()
    return Transaction(
        id=row["id"],
        txn_date=_as_date(row["txn_date"]),
        description=row["description"],
        amount=float(row["amount"]),
        kind=row["kind"],
        category=row["category"],
        category_source=row["category_source"],
        charge_kind=row["charge_kind"] if "charge_kind" in keys else None,
    )


def transactions_for_statement(
    conn: sqlite3.Connection,
    statement_id: int,
    *,
    q: str | None = None,
    category: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    charges_only: bool = False,
) -> list[Transaction]:
    """Line items in the order they were billed, undated ones last."""
    clauses = ["statement_id = ?"]
    params: list = [statement_id]
    _append_ledger_filters(
        clauses,
        params,
        q=q,
        category=category,
        min_amount=min_amount,
        max_amount=max_amount,
        charges_only=charges_only,
        description_col="description",
        note_col=None,
        amount_col="amount",
        category_col="category",
        charge_col="charge_kind",
    )
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE {' AND '.join(clauses)} "
        "ORDER BY txn_date IS NULL, txn_date, id",
        params,
    ).fetchall()
    return [_transaction_from_row(row) for row in rows]


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
    txn = _transaction_from_row(row)
    txn.category = category
    txn.category_source = source
    return (txn, int(row["card_id"]))


def add_payment(
    conn: sqlite3.Connection, card_id: int, amount: float, paid_on: date, note: str | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO payments (card_id, amount, paid_on, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (card_id, amount, _iso(paid_on), note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _append_ledger_filters(
    clauses: list[str],
    params: list,
    *,
    q: str | None,
    category: str | None,
    min_amount: float | None,
    max_amount: float | None,
    charges_only: bool,
    description_col: str,
    note_col: str | None,
    amount_col: str,
    category_col: str,
    charge_col: str,
) -> None:
    """Shared WHERE fragments for expense / transaction list + export."""
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        if note_col:
            clauses.append(f"({description_col} LIKE ? OR IFNULL({note_col}, '') LIKE ?)")
            params.extend([like, like])
        else:
            clauses.append(f"{description_col} LIKE ?")
            params.append(like)
    cat = (category or "").strip()
    if cat:
        clauses.append(f"{category_col} = ? COLLATE NOCASE")
        params.append(cat)
    if min_amount is not None:
        clauses.append(f"{amount_col} >= ?")
        params.append(min_amount)
    if max_amount is not None:
        clauses.append(f"{amount_col} <= ?")
        params.append(max_amount)
    if charges_only:
        clauses.append(f"{charge_col} IS NOT NULL AND TRIM({charge_col}) != ''")


def _expense_from_row(row: sqlite3.Row) -> Expense:
    keys = row.keys()
    return Expense(
        id=row["id"],
        spent_on=_as_date(row["spent_on"]) or date.today(),
        amount=float(row["amount"]),
        description=row["description"],
        category=row["category"],
        category_source=row["category_source"],
        charge_kind=row["charge_kind"] if "charge_kind" in keys else None,
        source=row["source"],
        source_ref=row["source_ref"],
        note=row["note"],
        spent_at=_as_datetime(row["spent_at"]) if "spent_at" in keys else None,
        created_at=_as_datetime(row["created_at"]),
    )


def add_expense(conn: sqlite3.Connection, expense: Expense) -> int | None:
    """Insert an expense. Returns id, or None when a keyed duplicate already exists."""
    # charge_kind is for statement line items only; expenses stay unflagged.
    try:
        cursor = conn.execute(
            """
            INSERT INTO expenses (
                spent_on, spent_at, amount, description, category, category_source,
                charge_kind, source, source_ref, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(expense.spent_on),
                _iso(expense.spent_at),
                expense.amount,
                expense.description.strip(),
                expense.category,
                expense.category_source,
                None,
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
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    q: str | None = None,
    category: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    charges_only: bool = False,
) -> list[Expense]:
    """Expenses with spent_on in [start, end)."""
    clauses = ["spent_on >= ?", "spent_on < ?"]
    params: list = [_iso(start), _iso(end)]
    _append_ledger_filters(
        clauses,
        params,
        q=q,
        category=category,
        min_amount=min_amount,
        max_amount=max_amount,
        charges_only=charges_only,
        description_col="description",
        note_col="note",
        amount_col="amount",
        category_col="category",
        charge_col="charge_kind",
    )
    rows = conn.execute(
        f"""
        SELECT * FROM expenses
        WHERE {' AND '.join(clauses)}
        ORDER BY spent_on DESC,
                 CASE WHEN spent_at IS NULL THEN 1 ELSE 0 END,
                 spent_at DESC,
                 id DESC
        """,
        params,
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
    category_source: str | None = None,
    note: str | None = None,
    spent_at: datetime | None | object = ...,
    charge_kind: str | None | object = ...,
) -> bool:
    """Update editable fields; leaves source / source_ref alone. True if a row changed.

    ``charge_kind`` is ignored for expenses (statement transactions only).
    Pass ``spent_at=...`` (Ellipsis) to leave the timestamp unchanged.
    """
    if spent_at is ...:
        cursor = conn.execute(
            """
            UPDATE expenses
            SET spent_on = ?, amount = ?, description = ?, category = ?,
                category_source = ?, note = ?, charge_kind = NULL
            WHERE id = ?
            """,
            (
                _iso(spent_on),
                amount,
                description.strip(),
                category,
                category_source,
                note,
                expense_id,
            ),
        )
    else:
        cursor = conn.execute(
            """
            UPDATE expenses
            SET spent_on = ?, spent_at = ?, amount = ?, description = ?, category = ?,
                category_source = ?, note = ?, charge_kind = NULL
            WHERE id = ?
            """,
            (
                _iso(spent_on),
                _iso(spent_at) if isinstance(spent_at, datetime) else None,
                amount,
                description.strip(),
                category,
                category_source,
                note,
                expense_id,
            ),
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


def list_transactions_in_range(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    q: str | None = None,
    category: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    charges_only: bool = False,
) -> list[Transaction]:
    """Line items on statements whose statement_date falls in [start, end)."""
    clauses = [
        "s.statement_date IS NOT NULL",
        "s.statement_date >= ?",
        "s.statement_date < ?",
    ]
    params: list = [_iso(start), _iso(end)]
    _append_ledger_filters(
        clauses,
        params,
        q=q,
        category=category,
        min_amount=min_amount,
        max_amount=max_amount,
        charges_only=charges_only,
        description_col="t.description",
        note_col=None,
        amount_col="t.amount",
        category_col="t.category",
        charge_col="t.charge_kind",
    )
    rows = conn.execute(
        f"""
        SELECT t.* FROM transactions t
        JOIN statements s ON s.id = t.statement_id
        WHERE {' AND '.join(clauses)}
        ORDER BY t.txn_date IS NULL, t.txn_date, t.id
        """,
        params,
    ).fetchall()
    return [_transaction_from_row(row) for row in rows]


def charges_total_for_statement(conn: sqlite3.Connection, statement_id: int) -> float:
    """Sum of debit amounts flagged with a charge_kind on one statement."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
        WHERE statement_id = ?
          AND kind = 'debit'
          AND charge_kind IS NOT NULL
          AND TRIM(charge_kind) != ''
        """,
        (statement_id,),
    ).fetchone()
    return float(row["total"])


def latest_charge_for_card(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    kinds: tuple[str, ...] | None = None,
) -> dict | None:
    """Newest debit charge on a card, optionally limited to charge_kind values."""
    clauses = [
        "card_id = ?",
        "kind = 'debit'",
        "charge_kind IS NOT NULL",
        "TRIM(charge_kind) != ''",
    ]
    params: list = [card_id]
    if kinds:
        placeholders = ", ".join("?" for _ in kinds)
        clauses.append(f"charge_kind IN ({placeholders})")
        params.extend(kinds)
    row = conn.execute(
        f"""
        SELECT id, description, amount, txn_date, charge_kind, statement_id, card_id
        FROM transactions
        WHERE {' AND '.join(clauses)}
        ORDER BY txn_date IS NULL, txn_date DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return _charge_dict_from_row(row)


def _charge_dict_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    from .charges import charge_label

    kind = row["charge_kind"]
    return {
        "id": row["id"],
        "description": row["description"],
        "amount": float(row["amount"]),
        "txn_date": _as_date(row["txn_date"]) if row["txn_date"] else None,
        "kind": kind,
        "label": charge_label(kind) or kind,
        "statement_id": row["statement_id"],
    }


def latest_charges_by_card(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per card: ``annual`` and ``any`` latest charge dicts (may be the same row)."""
    from .charges import CHARGE_ANNUAL_FEE

    def _latest_map(*, kinds: tuple[str, ...] | None = None) -> dict[int, dict]:
        clauses = [
            "kind = 'debit'",
            "charge_kind IS NOT NULL",
            "TRIM(charge_kind) != ''",
        ]
        params: list = []
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            clauses.append(f"charge_kind IN ({placeholders})")
            params.extend(kinds)
        rows = conn.execute(
            f"""
            SELECT id, card_id, description, amount, txn_date, charge_kind, statement_id
            FROM (
              SELECT id, card_id, description, amount, txn_date, charge_kind, statement_id,
                     ROW_NUMBER() OVER (
                       PARTITION BY card_id
                       ORDER BY txn_date IS NULL, txn_date DESC, id DESC
                     ) AS rn
              FROM transactions
              WHERE {' AND '.join(clauses)}
            )
            WHERE rn = 1
            """,
            params,
        ).fetchall()
        return {int(row["card_id"]): _charge_dict_from_row(row) for row in rows}

    any_by_card = _latest_map()
    annual_by_card = _latest_map(kinds=(CHARGE_ANNUAL_FEE,))
    out: dict[int, dict] = {}
    for card_id in set(any_by_card) | set(annual_by_card):
        out[card_id] = {
            "annual": annual_by_card.get(card_id),
            "any": any_by_card.get(card_id),
        }
    return out


def list_category_budgets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT category, amount_limit, updated_at FROM category_budgets "
        "ORDER BY category COLLATE NOCASE"
    ).fetchall()


def upsert_category_budget(
    conn: sqlite3.Connection, category: str, amount_limit: float
) -> bool:
    """Insert or update a monthly category limit. False if invalid."""
    category = (category or "").strip()
    if not category or amount_limit is None or amount_limit <= 0:
        return False
    conn.execute(
        """
        INSERT INTO category_budgets (category, amount_limit, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (category) DO UPDATE SET
            amount_limit = excluded.amount_limit,
            updated_at = excluded.updated_at
        """,
        (category, float(amount_limit), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return True


def delete_category_budget(conn: sqlite3.Connection, category: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM category_budgets WHERE category = ? COLLATE NOCASE",
        ((category or "").strip(),),
    )
    conn.commit()
    return cursor.rowcount > 0


def budget_status_for_month(
    conn: sqlite3.Connection, *, start: date, end: date
) -> list[dict]:
    """Budgets joined with spend for [start, end). Includes zero-spend budget rows."""
    from .categories import category_spend_totals

    expenses = list_expenses(conn, start=start, end=end)
    spent_map = dict(category_spend_totals(expenses))
    rows: list[dict] = []
    for budget in list_category_budgets(conn):
        label = budget["category"]
        limit = float(budget["amount_limit"])
        spent = float(spent_map.get(label, 0.0))
        remaining = round(limit - spent, 2)
        rows.append(
            {
                "category": label,
                "amount_limit": limit,
                "spent": round(spent, 2),
                "remaining": remaining,
                "over": spent > limit,
                "pct": min(100.0, round(100.0 * spent / limit, 1)) if limit else 0.0,
            }
        )
    rows.sort(key=lambda r: (-r["spent"], r["category"].lower()))
    return rows


def get_monthly_income(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT month, amount, credited_on, note, updated_at FROM monthly_income WHERE month = ?",
        (month,),
    ).fetchone()


def upsert_monthly_income(
    conn: sqlite3.Connection,
    *,
    month: str,
    amount: float,
    credited_on: date | None = None,
    note: str | None = None,
) -> bool:
    month = (month or "").strip()
    if not month or amount is None or amount < 0:
        return False
    conn.execute(
        """
        INSERT INTO monthly_income (month, amount, credited_on, note, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (month) DO UPDATE SET
            amount = excluded.amount,
            credited_on = excluded.credited_on,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        (
            month,
            float(amount),
            _iso(credited_on) if credited_on else None,
            (note or "").strip() or None,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return True


def copy_monthly_income(conn: sqlite3.Connection, *, from_month: str, to_month: str) -> bool:
    """Copy salary amount (and note) from one plan month to another."""
    import calendar as _calendar

    src = get_monthly_income(conn, from_month)
    if not src:
        return False
    year, month = (int(x) for x in to_month.split("-", 1))
    if month == 1:
        credited = date(year - 1, 12, 31)
    else:
        credited = date(year, month - 1, _calendar.monthrange(year, month - 1)[1])
    return upsert_monthly_income(
        conn,
        month=to_month,
        amount=float(src["amount"]),
        credited_on=credited,
        note=src["note"],
    )


def list_recurring_expenses(
    conn: sqlite3.Connection, *, active_only: bool = False
) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, label, amount, day_of_month, active, created_at, updated_at "
        "FROM recurring_expenses"
    )
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY COALESCE(day_of_month, 99), label COLLATE NOCASE"
    return conn.execute(sql).fetchall()


def add_recurring_expense(
    conn: sqlite3.Connection,
    *,
    label: str,
    amount: float,
    day_of_month: int | None = None,
    active: bool = True,
) -> int | None:
    label = (label or "").strip()
    if not label or amount is None or amount < 0:
        return None
    if day_of_month is not None and not (1 <= int(day_of_month) <= 31):
        day_of_month = None
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO recurring_expenses
            (label, amount, day_of_month, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (label, float(amount), day_of_month, 1 if active else 0, now, now),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_recurring_expense(
    conn: sqlite3.Connection,
    recurring_id: int,
    *,
    label: str | None = None,
    amount: float | None = None,
    day_of_month: int | None = ...,
    active: bool | None = None,
) -> bool:
    row = conn.execute(
        "SELECT id, label, amount, day_of_month, active FROM recurring_expenses WHERE id = ?",
        (recurring_id,),
    ).fetchone()
    if not row:
        return False
    new_label = (label if label is not None else row["label"]).strip()
    new_amount = float(amount) if amount is not None else float(row["amount"])
    if not new_label or new_amount < 0:
        return False
    if day_of_month is ...:
        new_day = row["day_of_month"]
    elif day_of_month is None or day_of_month == "":
        new_day = None
    else:
        new_day = int(day_of_month)
        if not (1 <= new_day <= 31):
            new_day = None
    new_active = row["active"] if active is None else (1 if active else 0)
    conn.execute(
        """
        UPDATE recurring_expenses
        SET label = ?, amount = ?, day_of_month = ?, active = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            new_label,
            new_amount,
            new_day,
            new_active,
            datetime.now().isoformat(timespec="seconds"),
            recurring_id,
        ),
    )
    conn.commit()
    return True


def delete_recurring_expense(conn: sqlite3.Connection, recurring_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM recurring_expenses WHERE id = ?", (recurring_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def paid_recurring_ids(conn: sqlite3.Connection, month: str) -> set[int]:
    rows = conn.execute(
        "SELECT recurring_id FROM recurring_paid WHERE month = ?", (month,)
    ).fetchall()
    return {int(r["recurring_id"]) for r in rows}


def set_recurring_paid(
    conn: sqlite3.Connection, recurring_id: int, month: str, *, paid: bool
) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM recurring_expenses WHERE id = ?", (recurring_id,)
    ).fetchone()
    if not exists or not (month or "").strip():
        return False
    if paid:
        conn.execute(
            """
            INSERT INTO recurring_paid (recurring_id, month, paid_at)
            VALUES (?, ?, ?)
            ON CONFLICT (recurring_id, month) DO UPDATE SET paid_at = excluded.paid_at
            """,
            (recurring_id, month, datetime.now().isoformat(timespec="seconds")),
        )
    else:
        conn.execute(
            "DELETE FROM recurring_paid WHERE recurring_id = ? AND month = ?",
            (recurring_id, month),
        )
    conn.commit()
    return True


def list_planned_expenses(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, month, label, amount, created_at FROM planned_expenses
        WHERE month = ?
        ORDER BY id
        """,
        (month,),
    ).fetchall()


def add_planned_expense(
    conn: sqlite3.Connection, *, month: str, label: str, amount: float
) -> int | None:
    month = (month or "").strip()
    label = (label or "").strip()
    if not month or not label or amount is None or amount < 0:
        return None
    cursor = conn.execute(
        """
        INSERT INTO planned_expenses (month, label, amount, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (month, label, float(amount), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_planned_expense(conn: sqlite3.Connection, planned_id: int) -> bool:
    cursor = conn.execute("DELETE FROM planned_expenses WHERE id = ?", (planned_id,))
    conn.commit()
    return cursor.rowcount > 0


def list_savings_goals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, label, target_amount, created_at, updated_at FROM savings_goals "
        "ORDER BY id"
    ).fetchall()


def add_savings_goal(
    conn: sqlite3.Connection, *, label: str, target_amount: float
) -> int | None:
    label = (label or "").strip()
    if not label or target_amount is None or target_amount <= 0:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO savings_goals (label, target_amount, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (label, float(target_amount), now, now),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_savings_goal(conn: sqlite3.Connection, goal_id: int) -> bool:
    cursor = conn.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))
    conn.commit()
    return cursor.rowcount > 0


def card_spend_total(conn: sqlite3.Connection, *, start: date, end: date) -> float:
    """Sum of statement total_due with statement_date in [start, end).

    Matches the billed amount for each card's statement in that month (not
    calendar-day purchase totals, which split a billing cycle across months).
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(total_due), 0) AS total FROM statements
        WHERE statement_date IS NOT NULL
          AND statement_date >= ? AND statement_date < ?
        """,
        (_iso(start), _iso(end)),
    ).fetchone()
    return float(row["total"])


def card_spend_by_card(
    conn: sqlite3.Connection, *, start: date, end: date
) -> list[dict]:
    """Statement total_due per card for statement_date in [start, end), largest first."""
    rows = conn.execute(
        """
        SELECT c.id AS card_id, c.label AS label, c.last4 AS last4,
               COALESCE(SUM(s.total_due), 0) AS total
        FROM statements s
        JOIN cards c ON c.id = s.card_id
        WHERE s.statement_date IS NOT NULL
          AND s.statement_date >= ? AND s.statement_date < ?
        GROUP BY c.id
        HAVING total > 0
        ORDER BY total DESC, c.label COLLATE NOCASE
        """,
        (_iso(start), _iso(end)),
    ).fetchall()
    return [
        {
            "card_id": int(row["card_id"]),
            "label": row["label"],
            "last4": row["last4"],
            "total": round(float(row["total"]), 2),
        }
        for row in rows
    ]


def _billing_month_key(statement_date: date) -> str:
    """Map statement_date to the Cards-tab month label (10th–9th window).

    Dates on/after the 10th belong to that calendar month; dates before the
    10th belong to the previous month (e.g. 5 Aug → 2026-07).
    """
    if statement_date.day >= 10:
        return f"{statement_date.year:04d}-{statement_date.month:02d}"
    if statement_date.month == 1:
        return f"{statement_date.year - 1:04d}-12"
    return f"{statement_date.year:04d}-{statement_date.month - 1:02d}"


def card_spend_months(conn: sqlite3.Connection) -> list[str]:
    """YYYY-MM keys that have at least one dated statement, newest first.

    Uses the 10th–9th billing window, not calendar month of statement_date.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT statement_date
        FROM statements
        WHERE statement_date IS NOT NULL
        """
    ).fetchall()
    months: set[str] = set()
    for row in rows:
        statement_date = _as_date(row["statement_date"])
        if statement_date is not None:
            months.add(_billing_month_key(statement_date))
    return sorted(months, reverse=True)


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
    conn: sqlite3.Connection,
    merchant_key: str,
    category: str,
    *,
    new_key: str | None = None,
) -> bool:
    """Change category and optionally rename the merchant key. True if saved."""
    category = (category or "").strip()
    old_key = (merchant_key or "").strip()
    target = (new_key or old_key).strip()
    if not old_key or not category or not target:
        return False

    exists = conn.execute(
        "SELECT 1 FROM merchant_categories WHERE merchant_key = ?", (old_key,)
    ).fetchone()
    if exists is None:
        return False

    now = datetime.now().isoformat(timespec="seconds")
    if target == old_key:
        cursor = conn.execute(
            """
            UPDATE merchant_categories
            SET category = ?, updated_at = ?
            WHERE merchant_key = ?
            """,
            (category, now, old_key),
        )
        conn.commit()
        return cursor.rowcount > 0

    conn.execute(
        """
        INSERT INTO merchant_categories (merchant_key, category, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (merchant_key) DO UPDATE SET
            category = excluded.category,
            updated_at = excluded.updated_at
        """,
        (target, category, now),
    )
    conn.execute("DELETE FROM merchant_categories WHERE merchant_key = ?", (old_key,))
    conn.commit()
    return True


def delete_merchant_category(conn: sqlite3.Connection, merchant_key: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM merchant_categories WHERE merchant_key = ?", (merchant_key,)
    )
    conn.commit()
    return cursor.rowcount > 0


def list_payee_cues(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, cue, updated_at FROM payee_cues ORDER BY cue COLLATE NOCASE"
    ).fetchall()


def list_payee_cue_strings(conn: sqlite3.Connection) -> list[str]:
    """Lowercased cue strings for merchant extraction."""
    return [
        (row["cue"] or "").strip().lower()
        for row in conn.execute("SELECT cue FROM payee_cues ORDER BY length(cue) DESC")
        if (row["cue"] or "").strip()
    ]


def upsert_payee_cue(conn: sqlite3.Connection, cue: str) -> int | None:
    """Insert or refresh a payee lead-in cue. Returns row id, or None if blank."""
    cue = (cue or "").strip().lower()
    if not cue:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO payee_cues (cue, updated_at)
        VALUES (?, ?)
        ON CONFLICT (cue) DO UPDATE SET
            updated_at = excluded.updated_at
        """,
        (cue, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM payee_cues WHERE cue = ? COLLATE NOCASE", (cue,)
    ).fetchone()
    return int(row["id"]) if row else None


def update_payee_cue(conn: sqlite3.Connection, cue_id: int, *, cue: str) -> bool:
    """Update a cue by id. True if a row changed."""
    cue = (cue or "").strip().lower()
    if not cue:
        return False
    try:
        cursor = conn.execute(
            """
            UPDATE payee_cues
            SET cue = ?, updated_at = ?
            WHERE id = ?
            """,
            (cue, datetime.now().isoformat(timespec="seconds"), cue_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.IntegrityError:
        return False


def delete_payee_cue(conn: sqlite3.Connection, cue_id: int) -> bool:
    cursor = conn.execute("DELETE FROM payee_cues WHERE id = ?", (cue_id,))
    conn.commit()
    return cursor.rowcount > 0


def list_payee_aliases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT raw_key, payee, updated_at FROM payee_aliases "
        "ORDER BY raw_key COLLATE NOCASE"
    ).fetchall()


def upsert_payee_alias(
    conn: sqlite3.Connection, raw_key: str, payee: str
) -> bool:
    """Insert or refresh a raw→display payee mapping."""
    raw_key = (raw_key or "").strip().lower()
    payee = (payee or "").strip()
    if not raw_key or not payee:
        return False
    conn.execute(
        """
        INSERT INTO payee_aliases (raw_key, payee, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (raw_key) DO UPDATE SET
            payee = excluded.payee,
            updated_at = excluded.updated_at
        """,
        (raw_key, payee, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return True


def update_payee_alias(
    conn: sqlite3.Connection,
    raw_key: str,
    payee: str,
    *,
    new_key: str | None = None,
) -> bool:
    """Change display name and optionally rename the raw key. True if saved."""
    payee = (payee or "").strip()
    old_key = (raw_key or "").strip().lower()
    target = ((new_key if new_key is not None else old_key) or "").strip().lower()
    if not old_key or not payee or not target:
        return False

    exists = conn.execute(
        "SELECT 1 FROM payee_aliases WHERE raw_key = ?", (old_key,)
    ).fetchone()
    if exists is None:
        return False

    now = datetime.now().isoformat(timespec="seconds")
    if target == old_key:
        cursor = conn.execute(
            """
            UPDATE payee_aliases
            SET payee = ?, updated_at = ?
            WHERE raw_key = ?
            """,
            (payee, now, old_key),
        )
        conn.commit()
        return cursor.rowcount > 0

    conn.execute(
        """
        INSERT INTO payee_aliases (raw_key, payee, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (raw_key) DO UPDATE SET
            payee = excluded.payee,
            updated_at = excluded.updated_at
        """,
        (target, payee, now),
    )
    conn.execute("DELETE FROM payee_aliases WHERE raw_key = ?", (old_key,))
    conn.commit()
    return True


def delete_payee_alias(conn: sqlite3.Connection, raw_key: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM payee_aliases WHERE raw_key = ?", ((raw_key or "").strip().lower(),)
    )
    conn.commit()
    return cursor.rowcount > 0
