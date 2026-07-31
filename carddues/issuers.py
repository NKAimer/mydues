"""Issuer registry.

One place to describe each issuer so Gmail search, PDF password guessing and
parser selection all agree on what "hdfc" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Issuer:
    key: str
    name: str
    senders: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    password_hint: str = ""
    extra_templates: tuple[str, ...] = field(default=())


ISSUERS: dict[str, Issuer] = {
    "hdfc": Issuer(
        key="hdfc",
        name="HDFC Bank",
        senders=("hdfcbank.net", "hdfcbank.com", "hdfcbank.bank.in"),
        markers=("hdfc bank", "hdfc credit card"),
        password_hint="Usually the first letters of your name combined with your date of birth.",
    ),
    "icici": Issuer(
        key="icici",
        name="ICICI Bank",
        senders=("icicibank.com", "icicibank.net"),
        markers=("icici bank",),
        password_hint="Usually name initials combined with date of birth.",
    ),
    "sbicard": Issuer(
        key="sbicard",
        name="SBI Card",
        senders=("sbicard.com", "sbi.co.in"),
        markers=("sbi card", "sbi cards"),
        password_hint="Often your date of birth in DDMMYYYY.",
    ),
    "axis": Issuer(
        key="axis",
        name="Axis Bank",
        senders=("axisbank.com", "axisbank.co.in"),
        markers=("axis bank",),
        password_hint="Usually name initials combined with date of birth.",
    ),
    "kotak": Issuer(
        key="kotak",
        name="Kotak Mahindra Bank",
        senders=("kotak.com", "kotakbank.com"),
        markers=("kotak mahindra", "kotak bank"),
        password_hint="Often your date of birth combined with card digits.",
    ),
    "idfcfirst": Issuer(
        key="idfcfirst",
        name="IDFC FIRST Bank",
        senders=("idfcfirstbank.com", "idfcbank.com"),
        markers=("idfc first", "idfc bank"),
        password_hint="Often your date of birth in DDMMYYYY.",
    ),
    "amex": Issuer(
        key="amex",
        name="American Express",
        senders=("americanexpress.com", "aexp.com"),
        markers=("american express", "amex"),
        password_hint="Often card digits combined with date of birth.",
    ),
    "indusind": Issuer(
        key="indusind",
        name="IndusInd Bank",
        senders=("indusind.com",),
        markers=("indusind",),
    ),
    "rbl": Issuer(
        key="rbl",
        name="RBL Bank",
        senders=("rblbank.com",),
        markers=("rbl bank",),
    ),
    "yesbank": Issuer(
        key="yesbank",
        name="YES Bank",
        senders=("yesbank.in",),
        markers=("yes bank",),
    ),
    "au": Issuer(
        key="au",
        name="AU Small Finance Bank",
        senders=("aubank.in",),
        markers=("au small finance", "au bank"),
    ),
    "federal": Issuer(
        key="federal",
        name="Federal Bank",
        senders=("federalbank.co.in",),
        markers=("federal bank",),
    ),
    "onecard": Issuer(
        key="onecard",
        name="OneCard",
        senders=("getonecard.app", "onecard.app"),
        markers=("onecard",),
    ),
    "other": Issuer(key="other", name="Other issuer"),
}

STATEMENT_SUBJECT_HINTS = (
    "credit card statement",
    "card statement",
    "e-statement",
    "estatement",
    "statement of account",
    "monthly statement",
)


def get(key: str) -> Issuer:
    return ISSUERS.get(key.lower(), ISSUERS["other"])


def detect(text: str) -> str | None:
    """Identify the issuer from statement text."""
    lowered = (text or "").lower()
    for issuer in ISSUERS.values():
        if any(marker in lowered for marker in issuer.markers):
            return issuer.key
    return None


def detect_from_sender(sender: str) -> str | None:
    lowered = (sender or "").lower()
    for issuer in ISSUERS.values():
        if any(domain in lowered for domain in issuer.senders):
            return issuer.key
    return None


def all_senders() -> list[str]:
    return sorted({domain for issuer in ISSUERS.values() for domain in issuer.senders})
