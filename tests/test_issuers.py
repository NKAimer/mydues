"""Issuer sender domains used by Gmail fetch and detect_from_sender."""

import pytest

from carddues import issuers


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ("HDFC Bank Cards <emailstatements.cards@hdfcbank.bank.in>", "hdfc"),
        ("<credit_cards@icici.bank.in>", "icici"),
        ("<customernotification@icici.bank.in>", "icici"),
        ("cc.statements@axis.bank.in", "axis"),
        ("<estatement@yes.bank.in>", "yesbank"),
        ("statements@kotak.bank.in", "kotak"),
        ("estatement@idfcfirst.bank.in", "idfcfirst"),
        ("alerts@indusind.bank.in", "indusind"),
        ("statements@rbl.bank.in", "rbl"),
        ("alert@au.bank.in", "au"),
        ("estatement@federal.bank.in", "federal"),
    ],
)
def test_bank_in_senders_detect_to_issuer(sender, expected):
    assert issuers.detect_from_sender(sender) == expected


def test_bank_in_domains_are_registered_alongside_legacy():
    """New .bank.in domains must sit beside old senders, not replace them."""
    expected = {
        "hdfc": "hdfcbank.bank.in",
        "icici": "icici.bank.in",
        "axis": "axis.bank.in",
        "yesbank": "yes.bank.in",
        "kotak": "kotak.bank.in",
        "idfcfirst": "idfcfirst.bank.in",
        "indusind": "indusind.bank.in",
        "rbl": "rbl.bank.in",
        "au": "au.bank.in",
        "federal": "federal.bank.in",
    }
    for key, domain in expected.items():
        senders = issuers.get(key).senders
        assert domain in senders
        assert any(not s.endswith(".bank.in") for s in senders)


def test_all_senders_includes_bank_in_for_gmail_query():
    senders = issuers.all_senders()
    for domain in (
        "hdfcbank.bank.in",
        "icici.bank.in",
        "axis.bank.in",
        "yes.bank.in",
        "kotak.bank.in",
        "idfcfirst.bank.in",
        "indusind.bank.in",
        "rbl.bank.in",
        "au.bank.in",
        "federal.bank.in",
    ):
        assert domain in senders


def test_sbicard_keeps_legacy_domains_without_unproven_bank_in():
    senders = issuers.get("sbicard").senders
    assert "sbicard.com" in senders
    assert "sbi.co.in" in senders
    assert not any(domain.endswith(".bank.in") for domain in senders)
