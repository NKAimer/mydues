import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

from mydues import passwords, pdfdoc
from mydues.models import Card

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
    _write_pdf(path, password="ada1012")

    result = pdfdoc.extract(path, ["wrong", "alsowrong", "ada1012"])
    assert result.was_encrypted
    assert result.password == "ada1012"
    assert "Minimum Amount Due" in result.text


def test_reports_locked_when_no_candidate_works(tmp_path):
    path = tmp_path / "locked.pdf"
    _write_pdf(path, password="ada1012")

    with pytest.raises(pdfdoc.LockedPdfError) as exc:
        pdfdoc.extract(path, ["nope"])
    assert "password protected" in str(exc.value)


def test_wrong_passwords_do_not_leave_temp_files_or_open_fds(tmp_path):
    """Each wrong guess used to leak an mkstemp FD and orphan a temp PDF."""
    path = tmp_path / "locked.pdf"
    _write_pdf(path, password="ada1012")
    guesses = [f"wrong-{index}" for index in range(80)]
    before_temps = set(Path(tempfile.gettempdir()).glob("mydues-*.pdf"))
    before_fds = _open_fd_count()

    with pytest.raises(pdfdoc.LockedPdfError):
        pdfdoc.extract(path, guesses)

    after_temps = set(Path(tempfile.gettempdir()).glob("mydues-*.pdf"))
    assert after_temps <= before_temps
    # A handful of other FDs may open in the process; dozens must not.
    assert _open_fd_count() - before_fds < 10


def _open_fd_count() -> int:
    try:
        return len(os.listdir(f"/dev/fd"))
    except OSError:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def test_candidates_cover_the_common_name_and_dob_pattern():
    card = Card(
        issuer="hdfc",
        label="HDFC",
        last4="8765",
        name="Ada Lovelace",
        dob=date(1815, 12, 10),
    )
    candidates = passwords.candidates_for(card)

    assert "ada1012" in candidates
    assert "ADA10121815" in candidates
    assert "10121815" in candidates
    assert len(candidates) == len(set(candidates))


def test_explicit_password_is_tried_first():
    card = Card(issuer="hdfc", label="HDFC", last4="8765", extra_passwords=["known-one"])
    assert passwords.candidates_for(card)[0] == "known-one"


def test_candidates_for_all_cards_are_deduplicated():
    shared = dict(name="Ada Lovelace", dob=date(1815, 12, 10))
    cards = [
        Card(issuer="hdfc", label="A", last4="1111", **shared),
        Card(issuer="icici", label="B", last4="2222", **shared),
    ]
    combined = passwords.candidates_for_all(cards)
    assert len(combined) == len(set(combined))
    assert "ada1012" in combined
