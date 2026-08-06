"""Shared spend categories for statement line items and the expenses ledger."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .models import CATEGORY_GUESS, CATEGORY_STATEMENT, CATEGORY_USER

UNCATEGORIZED = "Uncategorized"

# Merchants an Indian card / UPI alert sees, grouped like a spend summary.
# Only used when the issuer prints no category and merchant memory has no hit.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Payment received": (
        "payment received",
        "payment - thank",
        "thank you",
        "payment credit",
        "auto debit received",
        "neft cr",
    ),
    "Fees & interest": (
        "interest",
        "finance charge",
        "late payment",
        "late fee",
        "overdue charge",
        "annual fee",
        "joining fee",
        "membership fee",
        "surcharge",
        "markup",
        "penalty",
        "gst on fee",
        "gst",
        "igst",
        "cgst",
        "service fee",
        "processing fee",
        "convenience fee",
    ),
    "EMI": (
        "easy emi",
        "loan installment",
        "loan instalment",
        "emi installment",
        "emi instalment",
        " emi ",
        "installment",
        "instalment",
    ),
    "Cash & transfers": (
        "atm",
        "cash withdrawal",
        "cash advance",
        "imps",
        "neft",
        "rtgs",
    ),
    "Food & dining": (
        "swiggy",
        "zomato",
        "restaurant",
        "cafe",
        "coffee",
        "starbucks",
        "domino",
        "pizza",
        "mcdonald",
        "kfc",
        "burger",
        "barbeque",
        "biryani",
        "bakery",
        "eatclub",
        "eatfit",
    ),
    "Groceries": (
        "bigbasket",
        "blinkit",
        "zepto",
        "instamart",
        "dmart",
        "d-mart",
        "reliance fresh",
        "reliance retail",
        "supermarket",
        "grocer",
        "kirana",
        "licious",
        "country delight",
    ),
    "Travel": (
        "uber",
        "ola ",
        "olacabs",
        "rapido",
        "irctc",
        "indigo",
        "air india",
        "vistara",
        "spicejet",
        "akasa",
        "makemytrip",
        "goibibo",
        "yatra",
        "cleartrip",
        "redbus",
        "ixigo",
        "oyo",
        "airbnb",
        "hotel",
        "resort",
        "railway",
        "metro",
        "airport",
        "airlines",
        "travel",
    ),
    "Fuel": (
        "petrol",
        "fuel",
        "hpcl",
        "iocl",
        "bpcl",
        "indian oil",
        "bharat petroleum",
        "hp pay",
        "shell ",
        "nayara",
        "jio-bp",
    ),
    "Bills & utilities": (
        "electricity",
        "recharge",
        "airtel",
        "jio ",
        "vodafone",
        "bsnl",
        "broadband",
        "fibernet",
        "tata power",
        "bescom",
        "gas bill",
        "water bill",
        "bill payment",
        "billdesk",
        "insurance",
        "premium",
        "policybazaar",
    ),
    "Entertainment": (
        "netflix",
        "prime video",
        "hotstar",
        "spotify",
        "youtube",
        "bookmyshow",
        "pvr",
        "inox",
        "sonyliv",
        "zee5",
        "jiocinema",
        "google play",
        "apple.com/bill",
        "steam",
        "playstation",
    ),
    "Health": (
        "pharmacy",
        "pharmeasy",
        "apollo",
        "1mg",
        "netmeds",
        "hospital",
        "clinic",
        "diagnostic",
        "practo",
        "cult.fit",
    ),
    "Shopping": (
        "amazon",
        "flipkart",
        "myntra",
        "ajio",
        "nykaa",
        "meesho",
        "tatacliq",
        "tata cliq",
        "croma",
        "reliance digital",
        "decathlon",
        "ikea",
        "lifestyle",
        "westside",
        "zara",
        "uniqlo",
        "shoppers stop",
        "pantaloons",
    ),
}

# UPI alone is too broad for Cash & transfers; keep merchant cues here instead.
CATEGORY_KEYWORDS["Cash & transfers"] = CATEGORY_KEYWORDS["Cash & transfers"] + (
    "upi/",
    "@ybl",
    "@oksbi",
    "@okaxis",
    "@paytm",
    "@ibl",
)

_LETTERS = re.compile(r"[A-Za-z]{2,}")
CATEGORY_MEMORY = "memory"

# Sources that may be overwritten when phrase/merchant rules change.
_REAPPLY_SOURCES = frozenset({None, CATEGORY_GUESS, CATEGORY_MEMORY})


def merchant_key(description: str) -> str:
    """Stable key for remembering a category against a merchant string."""
    text = re.sub(r"[^a-z0-9]+", " ", (description or "").lower()).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:48]


def _stem_token(token: str) -> str:
    """Light plural stemming so pharmacies ↔ pharmacy."""
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 2 and token.endswith("s"):
        return token[:-1]
    return token


def _stemmed_tokens(key: str) -> set[str]:
    return {_stem_token(part) for part in key.split() if part}


def _merchant_key_matches(stored: str, description_key: str) -> bool:
    if stored in description_key or description_key in stored:
        return True
    stored_tokens = _stemmed_tokens(stored)
    if stored_tokens and stored_tokens <= _stemmed_tokens(description_key):
        return True
    # Compact form: "bharatconnectutiliti" ↔ "upi bharat connect uties"
    skip = {"upi", "www", "http", "https", "com"}
    compact_stored = "".join(p for p in stored.split() if p not in skip)
    compact_desc = "".join(p for p in description_key.split() if p not in skip)
    if len(compact_stored) >= 8 and len(compact_desc) >= 8:
        if compact_stored in compact_desc or compact_desc in compact_stored:
            return True
        shared = 0
        for left, right in zip(compact_stored, compact_desc):
            if left != right:
                break
            shared += 1
        if shared >= 12:
            return True
    return False


def categorise(
    description: str, *, phrases: list[tuple[str, str]] | None = None
) -> str | None:
    """A coarse category worked out from the merchant / narration text.

    When ``phrases`` is provided (including an empty list), only those rules
    are used — longest first — so deleting a seeded phrase actually stops
    matching. When ``phrases`` is None, the built-in keyword map is used.
    """
    if not description:
        return None
    lowered = f" {description.lower()} "
    if phrases is not None:
        ordered = sorted(
            ((p.strip(), c) for p, c in phrases if (p or "").strip()),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        for phrase, category in ordered:
            if phrase.lower() in lowered:
                return category
        return None
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def lookup_merchant_category(conn: sqlite3.Connection, description: str) -> str | None:
    key = merchant_key(description)
    if not key:
        return None
    row = conn.execute(
        "SELECT category FROM merchant_categories WHERE merchant_key = ?", (key,)
    ).fetchone()
    if row:
        return row["category"]

    best_key = ""
    best_category: str | None = None
    for stored in conn.execute(
        "SELECT merchant_key, category FROM merchant_categories"
    ).fetchall():
        stored_key = stored["merchant_key"] or ""
        if not stored_key or not _merchant_key_matches(stored_key, key):
            continue
        if len(stored_key) > len(best_key):
            best_key = stored_key
            best_category = stored["category"]
    return best_category


def remember_merchant_category(
    conn: sqlite3.Connection, description: str, category: str
) -> None:
    """Remember the user's category choice for this merchant string."""
    key = merchant_key(description)
    category = (category or "").strip()
    if not key or not category:
        return
    conn.execute(
        """
        INSERT INTO merchant_categories (merchant_key, category, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (merchant_key) DO UPDATE SET
            category = excluded.category,
            updated_at = excluded.updated_at
        """,
        (key, category, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def apply_payee_alias(conn: sqlite3.Connection, description: str) -> str:
    """Replace a raw extracted payee with a remembered display name when known."""
    key = merchant_key(description)
    if not key:
        return description
    row = conn.execute(
        "SELECT payee FROM payee_aliases WHERE raw_key = ?", (key,)
    ).fetchone()
    if row and (row["payee"] or "").strip():
        return (row["payee"] or "").strip()

    best_key = ""
    best_payee: str | None = None
    for stored in conn.execute("SELECT raw_key, payee FROM payee_aliases"):
        stored_key = stored["raw_key"] or ""
        payee = (stored["payee"] or "").strip()
        if not stored_key or not payee:
            continue
        if not _merchant_key_matches(stored_key, key):
            continue
        if len(stored_key) > len(best_key):
            best_key = stored_key
            best_payee = payee
    return best_payee if best_payee else description


def remember_payee_alias(
    conn: sqlite3.Connection, old_description: str, new_payee: str
) -> None:
    """Remember that ``old_description`` should display as ``new_payee``."""
    key = merchant_key(old_description)
    payee = (new_payee or "").strip()
    if not key or not payee:
        return
    if merchant_key(payee) == key:
        return
    conn.execute(
        """
        INSERT INTO payee_aliases (raw_key, payee, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (raw_key) DO UPDATE SET
            payee = excluded.payee,
            updated_at = excluded.updated_at
        """,
        (key, payee, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def category_spend_totals(items: Iterable) -> list[tuple[str, float]]:
    """Debit spend by category, largest first. Blank category → Uncategorized.

    Credits are ignored. The Uncategorized label is display-only and is never
    written back to storage.
    """
    totals: dict[str, float] = defaultdict(float)
    for item in items:
        if getattr(item, "is_credit", False):
            continue
        label = (getattr(item, "category", None) or "").strip() or UNCATEGORIZED
        totals[label] += float(item.amount)
    return sorted(
        ((label, round(amount, 2)) for label, amount in totals.items()),
        key=lambda pair: (-pair[1], pair[0]),
    )


def resolve_category(
    description: str,
    *,
    printed: str | None = None,
    note: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[str | None, str | None]:
    """Pick a category: issuer print → merchant memory → phrase rules / charge.

    With a connection, phrase rules come from ``category_phrases`` (seeded from
    builtins on init). Without one, ``categorise`` uses the built-in map.

    Returns `(category, source)` where source is statement / memory / guess.
    Charge kinds map to Fees & interest / EMI on the guess path only when no
    stronger match already won.
    """
    from .charges import category_for_charge, detect_charge_kind

    if printed and _LETTERS.search(printed):
        return printed.strip(), CATEGORY_STATEMENT

    phrases: list[tuple[str, str]] | None = None
    if conn is not None:
        remembered = lookup_merchant_category(conn, description)
        if remembered:
            return remembered, CATEGORY_MEMORY
        if note:
            remembered = lookup_merchant_category(conn, note)
            if remembered:
                return remembered, CATEGORY_MEMORY
        phrases = [
            (row["phrase"], row["category"])
            for row in conn.execute(
                "SELECT phrase, category FROM category_phrases"
            ).fetchall()
        ]

    blob = description or ""
    if note and note.strip() and note.strip().lower() != blob.strip().lower():
        blob = f"{blob} {note}".strip()
    guess = categorise(blob, phrases=phrases)
    if guess:
        return guess, CATEGORY_GUESS

    charge_cat = category_for_charge(detect_charge_kind(description, note=note))
    if charge_cat:
        return charge_cat, CATEGORY_GUESS
    return (None, None)


def _may_reapply(old_source: str | None, new_source: str | None) -> bool:
    """Whether bulk re-apply may overwrite this row's category.

    Manual ``user`` edits are never touched. Issuer-printed ``statement``
    categories stay put unless a learned merchant (``memory``) matches — that
    is how users correct noisy bank labels like "Miscellaneous Stores".
    """
    if old_source == CATEGORY_USER:
        return False
    if old_source == CATEGORY_STATEMENT:
        return new_source == CATEGORY_MEMORY
    return old_source in _REAPPLY_SOURCES


def apply_category_rules(conn: sqlite3.Connection) -> dict[str, int]:
    """Re-resolve blank/auto categories on expenses and statement transactions.

    Skips manual ``user`` edits. Issuer-printed ``statement`` rows are only
    updated when a learned merchant matches. Refreshes null charge_kind on
    statement transactions only. Does not teach merchant memory.
    Returns counts of rows whose category or source changed.
    """
    from .charges import detect_charge_kind

    expenses_updated = 0
    for row in conn.execute(
        "SELECT id, description, note, category, category_source FROM expenses"
    ).fetchall():
        category, source = resolve_category(
            row["description"], note=row["note"], conn=conn
        )
        if not _may_reapply(row["category_source"], source):
            continue
        if category != row["category"] or source != row["category_source"]:
            conn.execute(
                "UPDATE expenses SET category = ?, category_source = ?, charge_kind = NULL "
                "WHERE id = ?",
                (category, source, row["id"]),
            )
            expenses_updated += 1

    transactions_updated = 0
    for row in conn.execute(
        "SELECT id, description, category, category_source, charge_kind FROM transactions"
    ).fetchall():
        category, source = resolve_category(row["description"], conn=conn)
        kind = row["charge_kind"] or detect_charge_kind(row["description"] or "")
        cat_change = False
        if _may_reapply(row["category_source"], source):
            if category != row["category"] or source != row["category_source"]:
                cat_change = True
        kind_change = kind != row["charge_kind"]
        if not cat_change and not kind_change:
            continue
        if cat_change:
            conn.execute(
                "UPDATE transactions SET category = ?, category_source = ?, charge_kind = ? "
                "WHERE id = ?",
                (category, source, kind, row["id"]),
            )
            transactions_updated += 1
        elif kind_change:
            conn.execute(
                "UPDATE transactions SET charge_kind = ? WHERE id = ?",
                (kind, row["id"]),
            )

    conn.commit()
    return {
        "expenses_updated": expenses_updated,
        "transactions_updated": transactions_updated,
    }
