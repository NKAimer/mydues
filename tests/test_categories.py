"""Shared spend categorisation: keywords, merchant memory, expense alerts."""

from datetime import datetime

from carddues.categories import (
    CATEGORY_MEMORY,
    categorise,
    remember_merchant_category,
    resolve_category,
)
from carddues import expense_ingest
from carddues.models import CATEGORY_GUESS, CATEGORY_STATEMENT


def test_keyword_guesses_common_merchants():
    assert categorise("SWIGGY BANGALORE") == "Food & dining"
    assert categorise("IOCL FUEL STATION") == "Fuel"
    assert categorise("PAYMENT - THANK YOU") == "Payment received"
    assert categorise("MYNTRA FASHION") == "Shopping"


def test_resolve_prefers_printed_over_keywords():
    category, source = resolve_category("SWIGGY", printed="Food & Beverages")
    assert category == "Food & Beverages"
    assert source == CATEGORY_STATEMENT


def test_resolve_falls_back_to_keyword_guess():
    category, source = resolve_category("ZOMATO ORDER")
    assert category == "Food & dining"
    assert source == CATEGORY_GUESS


def test_unrecognised_merchant_stays_uncategorized():
    category, source = resolve_category("SUNRISE TRADERS PVT LTD")
    assert category is None
    assert source is None


def test_memory_overrides_keywords(conn):
    remember_merchant_category(conn, "SWIGGY BANGALORE", "Travel")
    category, source = resolve_category("SWIGGY BANGALORE", conn=conn)
    assert category == "Travel"
    assert source == CATEGORY_MEMORY


def test_memory_for_unknown_merchant(conn):
    assert categorise("ZZZNOVELMERCHANT99") is None
    remember_merchant_category(conn, "ZZZNOVELMERCHANT99", "Shopping")
    category, source = resolve_category("ZZZNOVELMERCHANT99", conn=conn)
    assert category == "Shopping"
    assert source == CATEGORY_MEMORY


def test_printed_beats_memory(conn):
    remember_merchant_category(conn, "SWIGGY", "Travel")
    category, source = resolve_category(
        "SWIGGY", printed="Food & Beverages", conn=conn
    )
    assert category == "Food & Beverages"
    assert source == CATEGORY_STATEMENT


def test_parse_alert_assigns_keyword_category():
    expense = expense_ingest.parse_alert_email(
        subject="Transaction Alert: INR 420.00 spent",
        body="Rs. 420.00 spent at SWIGGY BANGALORE on 01-08-2026 using your card.",
        received_at=datetime(2026, 8, 1, 12, 0),
    )
    assert expense is not None
    assert "SWIGGY" in expense.description.upper()
    assert expense.category == "Food & dining"
