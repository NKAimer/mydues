"""Golden statement text dumps — one net for every issuer layout we care about."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mydues.parsers import parse_statement
from mydues.text import normalize

GOLDEN = Path(__file__).parent / "golden"


def _cases() -> list[Path]:
    return sorted(GOLDEN.glob("*.expected.json"))


@pytest.mark.parametrize("expected_path", _cases(), ids=lambda p: p.stem.replace(".expected", ""))
def test_golden_statement(expected_path: Path):
    text_path = expected_path.with_name(expected_path.name.replace(".expected.json", ".txt"))
    assert text_path.exists(), f"missing {text_path.name}"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    text = normalize(text_path.read_text(encoding="utf-8"))
    result = parse_statement(text, issuer_hint=expected.get("issuer"))

    assert result is not None, f"{text_path.name} did not parse"
    if expected.get("issuer"):
        assert result.issuer == expected["issuer"]
    if expected.get("last4") is not None:
        assert result.last4 == expected["last4"]
    if expected.get("total_due") is not None:
        assert result.total_due == pytest.approx(float(expected["total_due"]))
    if expected.get("min_due") is not None:
        assert result.min_due == pytest.approx(float(expected["min_due"]))
    if expected.get("due_date"):
        assert result.due_date == date.fromisoformat(expected["due_date"])
    if expected.get("statement_date"):
        assert result.statement_date == date.fromisoformat(expected["statement_date"])
    min_txns = int(expected.get("min_transactions") or 0)
    assert len(result.transactions) >= min_txns, (
        f"{text_path.name}: expected >= {min_txns} txns, got {len(result.transactions)}"
    )
