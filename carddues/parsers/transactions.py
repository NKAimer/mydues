"""Line items from a statement.

Two layouts cover almost every Indian issuer. Some draw the transactions as a
ruled table with a header row, which pdfplumber hands over as cells; the rest
print one transaction per line, always opening with a date and closing with an
amount. Tables are read first because they name their columns, including the
spend category on the few statements that print one.
"""

from __future__ import annotations

import re

from ..models import (
    CATEGORY_GUESS,
    CATEGORY_STATEMENT,
    KIND_CREDIT,
    KIND_DEBIT,
    Transaction,
)
from ..text import AMOUNT_RE, DATE_RE, parse_amount, parse_date

# Merchants an Indian card sees, grouped the way a spend summary would group
# them. Only consulted when the statement prints no category of its own, and
# what it produces is recorded as a guess.
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
        "upi",
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

# What a column header has to contain for its column to be recognised.
_COLUMN_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("category", ("category",)),
    ("date", ("date",)),
    ("amount", ("amount", "value")),
    (
        "description",
        ("detail", "description", "particular", "merchant", "narration", "transaction"),
    ),
)

# A description has to read like one, or a row of summary figures would pass.
_LETTERS = re.compile(r"[A-Za-z]{2,}")

# Totals and balances sit in the same grid as the transactions but are not any.
_SUMMARY = re.compile(
    r"^(sub\s*)?total\b|^grand\s+total|^(opening|closing|previous|net)\s+balance"
    r"|^net\s+outstanding|^total\s+(?:purchase|cash|loan|balance\s+transfer)\s+outstanding"
    r"|^balance\b|^amount\s+due",
    re.IGNORECASE,
)


def categorise(description: str) -> str | None:
    """A coarse category worked out from the merchant name."""
    lowered = f" {description.lower()} "
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def _with_category(
    description: str, printed: str | None
) -> tuple[str | None, str | None]:
    """The category to store, and whether the statement or we came up with it."""
    if printed and _LETTERS.search(printed):
        return printed.strip(), CATEGORY_STATEMENT
    guess = categorise(description)
    return (guess, CATEGORY_GUESS) if guess else (None, None)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,-|\u2022")


def _transaction(
    *,
    raw_date: str,
    description: str,
    raw_amount: str,
    printed_category: str | None,
    require_date: bool = False,
) -> Transaction | None:
    txn_date = parse_date(raw_date)
    amount = parse_amount(raw_amount)
    description = _clean(description)
    if amount is None or not _LETTERS.search(description) or _SUMMARY.match(description):
        return None
    # Period lines like "13/06/2026 To 12/07/2026 …" leave a useless "To".
    if re.fullmatch(r"(?i)to|from|dr|cr", description or ""):
        return None
    if require_date and txn_date is None:
        return None

    credit = amount < 0 or bool(re.search(r"\bcr\b", raw_amount, re.IGNORECASE))
    category, source = _with_category(description, printed_category)
    return Transaction(
        description=description,
        amount=abs(amount),
        txn_date=txn_date,
        kind=KIND_CREDIT if credit else KIND_DEBIT,
        category=category,
        category_source=source,
    )


def _header(cells: list[str | None]) -> dict[str, int] | None:
    """Column positions, if this row names the columns of a transaction table."""
    found: dict[str, int] = {}
    for position, cell in enumerate(cells):
        value = _clean((cell or "").lower())
        if not value:
            continue
        for column, markers in _COLUMN_MARKERS:
            if column not in found and any(marker in value for marker in markers):
                found[column] = position
                break
    if {"date", "amount", "description"} <= found.keys():
        return found
    return None


def from_tables(tables: list[list[list[str | None]]]) -> list[Transaction]:
    """Read the transaction grids, keeping the categories they print."""
    rows: list[Transaction] = []
    for table in tables:
        columns: dict[str, int] | None = None
        for cells in table:
            if columns is None:
                columns = _header(cells)
                continue
            if max(columns.values()) >= len(cells):
                continue
            txn = _transaction(
                raw_date=cells[columns["date"]] or "",
                description=cells[columns["description"]] or "",
                raw_amount=cells[columns["amount"]] or "",
                printed_category=(
                    cells[columns["category"]] if "category" in columns else None
                ),
                # A footer row carries a total but no date of its own.
                require_date=True,
            )
            if txn:
                rows.append(txn)
    return rows


def _money(masked: str) -> re.Match[str] | None:
    """The amount at the end of a line, ignoring reward points and card digits.

    A figure written like money wins: it carries decimals, digit grouping or a
    Cr/Dr marker. Only when none does is the last number taken.
    """
    matches = [match for match in AMOUNT_RE.finditer(masked) if match.group("num")]
    if not matches:
        return None
    money_like = [
        match
        for match in matches
        if match.group("suffix") or "." in match.group("num") or "," in match.group("num")
    ]
    return (money_like or matches)[-1]


def from_lines(text: str) -> list[Transaction]:
    """Read one-transaction-per-line statements.

    A line qualifies by opening with a date and closing with an amount, which
    is what separates a transaction from the summary rows above it.
    """
    rows: list[Transaction] = []
    for line in text.split("\n"):
        line = line.strip()
        opening = DATE_RE.match(line)
        if not opening:
            continue

        rest = line[opening.end() :].lstrip()
        # A posting date often follows the transaction date; it is not part of
        # the description.
        posting = DATE_RE.match(rest)
        if posting:
            rest = rest[posting.end() :].lstrip()

        # Dates inside the line must not be mistaken for the amount, so they are
        # blanked out at their original width to keep the offsets true.
        masked = DATE_RE.sub(lambda m: " " * len(m.group(0)), rest)
        amount = _money(masked)
        if amount is None:
            continue

        txn = _transaction(
            raw_date=opening.group(0),
            description=rest[: amount.start()],
            raw_amount=rest[amount.start() : amount.end()],
            printed_category=None,
        )
        if txn:
            rows.append(txn)
    return rows


def dedupe(rows: list[Transaction]) -> list[Transaction]:
    """Drop repeats, which pages that restate a row produce."""
    seen: set[tuple] = set()
    kept: list[Transaction] = []
    for txn in rows:
        key = (txn.txn_date, txn.description.lower(), txn.amount, txn.kind)
        if key in seen:
            continue
        seen.add(key)
        kept.append(txn)
    return kept


def _score(rows: list[Transaction]) -> tuple[int, float]:
    """Prefer more line items, then a larger absolute spend sum."""
    total = sum(abs(txn.amount) for txn in rows)
    return (len(rows), total)


def extract_transactions(
    text: str, tables: list[list[list[str | None]]] | None = None
) -> list[Transaction]:
    """Every line item the statement gives up.

    Tables and lines are both tried; the richer parse wins. Blindly preferring
    a sparse pdfplumber table used to wipe out good line extracts.
    """
    from_table = dedupe(from_tables(tables or []))
    from_line = dedupe(from_lines(text))
    if not from_table:
        return from_line
    if not from_line:
        return from_table
    return from_table if _score(from_table) >= _score(from_line) else from_line
