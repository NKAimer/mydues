"""Reading the password rule that the covering email states."""

import base64
from datetime import date

import pytest

from carddues import gmail, passwords
from carddues.models import Card


def make_card(**overrides) -> Card:
    fields = {
        "issuer": "hdfc",
        "label": "HDFC Infinia",
        "last4": "8765",
        "name": "Naveen Kumar",
        "dob": date(1990, 4, 15),
    }
    fields.update(overrides)
    return Card(**fields)


def derive(hint: str, card: Card | None = None) -> list[str]:
    return passwords.candidates_from_hint(hint, [card or make_card()])


def test_name_initials_with_date_of_birth():
    hint = (
        "Your statement is password protected. The password is the first 4 letters "
        "of your name in capital letters followed by your date of birth in DDMM "
        "format, for example NAVE0101."
    )
    assert derive(hint) == ["NAVE1504"]


def test_date_of_birth_only():
    hint = "This file is password protected. Please enter your date of birth in DDMMYYYY."
    assert derive(hint) == ["15041990"]


def test_surname_with_card_digits():
    hint = (
        "The password to open this PDF is the first four characters of your surname "
        "in CAPS and the last 4 digits of your card number."
    )
    assert derive(hint) == ["KUMA8765"]


def test_unstated_casing_tries_the_variants():
    hint = "Password: first 4 letters of your name and date of birth in DDMM."
    assert derive(hint) == ["NAVE1504", "nave1504", "Nave1504"]


def test_pan_based_rule():
    hint = "The password is your PAN in capital letters."
    assert derive(hint, make_card(pan="ABCDE1234F")) == ["ABCDE1234F"]


def test_separators_in_the_stated_format():
    hint = "The password for this file is your name in small letters and DD/MM/YYYY of birth."
    assert derive(hint) == ["naveen15041990"]


def test_two_stated_formats_are_both_tried():
    hint = "Password is your date of birth as DDMM or DDMMYYYY."
    assert derive(hint) == ["1504", "15041990"]


def test_a_rule_needing_digits_we_do_not_hold_is_refused():
    """Only the last four digits are on file, so a five digit rule is unknowable."""
    hint = "The password is the last 5 digits of your card number."
    assert derive(hint) == []


def test_hsbc_rule_uses_dob_and_last_six_digits_from_pan():
    """HSBC: DDMMYY + last 6 of the primary card (store those six on `pan`)."""
    card = make_card(dob=date(1970, 6, 2), pan="004200", last4="4200")
    hint = (
        "Password protection: Your password is a combination of your date of birth "
        "(in DDMMYY format) followed by the last 6 digits of the primary credit card number. "
        "For example: If your primary credit card number is 5120 0000 0000 4200 and your "
        "birth date is 2 June 1970, then the password is 020670004200."
    )
    assert derive(hint, card) == ["020670004200"]


def test_a_card_missing_the_details_is_skipped():
    hint = "Password is the first 4 letters of your name and date of birth in DDMM."
    assert derive(hint, make_card(name=None, dob=None)) == []


def test_prose_without_a_password_rule_is_ignored():
    hint = (
        "Dear Naveen Kumar, your statement for the card ending 8765 is attached. "
        "Total due 45,231.50 by 15 August. Date of birth on record: 15/04/1990."
    )
    assert derive(hint) == []


def test_only_password_sentences_are_read():
    """Card digits mentioned elsewhere must not become part of the rule."""
    hint = (
        "Your card ending in the last 4 digits 8765 was billed. "
        "The password is your date of birth in DDMMYYYY. "
        "Call the last 4 digits of our helpline for support."
    )
    assert derive(hint) == ["15041990"]


def test_the_word_file_name_is_not_a_name_rule():
    hint = "The attached file name is statement.pdf and the password is your date of birth DDMM."
    assert derive(hint) == ["1504"]


def test_rule_is_read_for_each_registered_card():
    hint = "Password: first 4 letters of your name in capitals and date of birth DDMM."
    other = make_card(issuer="axis", last4="1111", name="Asha Rao", dob=date(1985, 12, 2))
    assert passwords.candidates_from_hint(hint, [make_card(), other]) == ["NAVE1504", "ASHA0212"]


def test_derived_passwords_are_deduplicated():
    hint = "Password is date of birth in DDMM. The password is date of birth in DDMM."
    assert derive(hint) == ["1504"]


@pytest.mark.parametrize("spelling", ["pass word", "passcode", "PASSWORD"])
def test_common_spellings_of_the_word(spelling):
    assert derive(f"The {spelling} is your date of birth in DDMMYYYY.") == ["15041990"]


def _part(mime: str, text: str) -> dict:
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return {"mimeType": mime, "body": {"data": encoded}}


def test_body_prefers_the_plain_text_alternative():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/plain", "Password is DDMM."), _part("text/html", "<p>ignored</p>")],
    }
    assert gmail.body_text(payload) == "Password is DDMM."


def test_body_falls_back_to_stripped_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/html", "<style>p{color:red}</style><p>Password is <b>DDMM</b>&nbsp;only</p>")
        ],
    }
    assert gmail.body_text(payload) == "Password is DDMM only"


def test_body_reaches_into_nested_parts():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [_part("text/plain", "Nested rule")],
            },
            {"filename": "statement.pdf", "body": {"attachmentId": "a1"}},
        ],
    }
    assert gmail.body_text(payload) == "Nested rule"


def test_attachment_text_is_not_treated_as_body():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [_part("text/plain", "Real body"), {"filename": "note.txt", **_part("text/plain", "attached")}],
    }
    assert gmail.body_text(payload) == "Real body"


def test_a_message_without_a_body_is_empty():
    assert gmail.body_text({"mimeType": "text/plain", "body": {}}) == ""
