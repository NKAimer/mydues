from datetime import date

import pytest

from carddues import passwords, pdfdoc
from carddues.models import Card

from .fixtures import write_pdf as _write_pdf


def test_reads_an_unlocked_statement(tmp_path):
    path = tmp_path / "plain.pdf"
    _write_pdf(path)

    result = pdfdoc.extract(path)
    assert not result.was_encrypted
    assert result.password is None
    assert "Total Amount Due" in result.text
    assert "45,231.50" in result.text


def test_unlocks_with_the_right_candidate(tmp_path):
    path = tmp_path / "locked.pdf"
    _write_pdf(path, password="nave0107")

    result = pdfdoc.extract(path, ["wrong", "alsowrong", "nave0107"])
    assert result.was_encrypted
    assert result.password == "nave0107"
    assert "Minimum Amount Due" in result.text


def test_reports_locked_when_no_candidate_works(tmp_path):
    path = tmp_path / "locked.pdf"
    _write_pdf(path, password="nave0107")

    with pytest.raises(pdfdoc.LockedPdfError) as exc:
        pdfdoc.extract(path, ["nope"])
    assert "password protected" in str(exc.value)


def test_candidates_cover_the_common_name_and_dob_pattern():
    card = Card(
        issuer="hdfc",
        label="HDFC",
        last4="8765",
        name="Naveen Kumar",
        dob=date(1990, 7, 1),
    )
    candidates = passwords.candidates_for(card)

    assert "nave0107" in candidates
    assert "NAVE01071990" in candidates
    assert "01071990" in candidates
    assert len(candidates) == len(set(candidates))


def test_explicit_password_is_tried_first():
    card = Card(issuer="hdfc", label="HDFC", last4="8765", extra_passwords=["known-one"])
    assert passwords.candidates_for(card)[0] == "known-one"


def test_candidates_for_all_cards_are_deduplicated():
    shared = dict(name="Naveen Kumar", dob=date(1990, 7, 1))
    cards = [
        Card(issuer="hdfc", label="A", last4="1111", **shared),
        Card(issuer="icici", label="B", last4="2222", **shared),
    ]
    combined = passwords.candidates_for_all(cards)
    assert len(combined) == len(set(combined))
    assert "nave0107" in combined
