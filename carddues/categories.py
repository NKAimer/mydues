"""Shared spend categories for statement line items and the expenses ledger."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .models import CATEGORY_GUESS, CATEGORY_STATEMENT

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
        "annual fee",
        "joining fee",
        "membership fee",
        "surcharge",
        "markup",
        "penalty",
        "gst",
        "igst",
        "cgst",
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


def merchant_key(description: str) -> str:
    """Stable key for remembering a category against a merchant string."""
    text = re.sub(r"[^a-z0-9]+", " ", (description or "").lower()).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:48]


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
    return row["category"] if row else None


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
    """Pick a category: issuer print → merchant memory → phrase rules.

    With a connection, phrase rules come from ``category_phrases`` (seeded from
    builtins on init). Without one, ``categorise`` uses the built-in map.

    Returns `(category, source)` where source is statement / memory / guess.
    """
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
    return (guess, CATEGORY_GUESS) if guess else (None, None)
