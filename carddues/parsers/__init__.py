"""Parser selection."""

from __future__ import annotations

from ..models import ParsedStatement
from ..statement_gate import looks_like_credit_card_statement
from .banks import (
    AmexParser,
    AuParser,
    AxisParser,
    FederalParser,
    GenericParser,
    HdfcParser,
    IciciParser,
    IdfcFirstParser,
    IndusIndParser,
    KotakParser,
    OneCardParser,
    RblParser,
    SbiCardParser,
    YesBankParser,
)
from .base import StatementParser, extract_fields
from .transactions import categorise, extract_transactions

PARSERS: list[StatementParser] = [
    HdfcParser(),
    IciciParser(),
    SbiCardParser(),
    AxisParser(),
    KotakParser(),
    IdfcFirstParser(),
    AmexParser(),
    IndusIndParser(),
    RblParser(),
    YesBankParser(),
    AuParser(),
    FederalParser(),
    OneCardParser(),
]

GENERIC = GenericParser()

BY_KEY = {parser.key: parser for parser in PARSERS}


def parse_statement(
    text: str,
    *,
    issuer_hint: str | None = None,
    tables: list[list[list[str | None]]] | None = None,
) -> ParsedStatement | None:
    """Parse statement text, preferring the issuer the email came from.

    `tables` are the ruled grids from the same PDF, which is where the line
    items are when a statement draws them.
    """
    if not text:
        return None
    if not looks_like_credit_card_statement(text):
        return None

    statement = _summary(text, issuer_hint=issuer_hint)
    if statement is not None:
        statement.transactions = extract_transactions(text, tables)
    return statement


def _summary(text: str, *, issuer_hint: str | None) -> ParsedStatement | None:
    if issuer_hint and issuer_hint in BY_KEY:
        result = BY_KEY[issuer_hint].parse(text)
        if result:
            return result

    for parser in PARSERS:
        if parser.matches(text):
            result = parser.parse(text)
            if result:
                return result

    return GENERIC.parse(text)


__all__ = [
    "PARSERS",
    "BY_KEY",
    "GENERIC",
    "parse_statement",
    "extract_fields",
    "extract_transactions",
    "categorise",
    "StatementParser",
]
