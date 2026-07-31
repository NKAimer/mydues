"""Password candidates for locked statement PDFs.

Issuers derive statement passwords from details you already know: your name,
date of birth, PAN and the last digits of the card. The covering email usually
states the rule, so `candidates_from_hint` reads it and builds the exact
password. When there is no hint, or it cannot be satisfied, fall back to
generating the common permutations and trying them.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date

from .models import Card

MAX_CANDIDATES = 200
MAX_DERIVED_PER_CARD = 24


def _name_fragments(name: str | None) -> list[str]:
    if not name:
        return []
    cleaned = re.sub(r"[^A-Za-z ]", "", name).strip()
    if not cleaned:
        return []
    parts = cleaned.split()
    first, last = parts[0], parts[-1]
    fragments = {first[:4], first[:5], first, last[:4], last}
    result: list[str] = []
    for fragment in fragments:
        if len(fragment) < 3:
            continue
        result.extend([fragment.lower(), fragment.upper(), fragment.capitalize()])
    return result


def _dob_fragments(dob: date | None) -> list[str]:
    if not dob:
        return []
    return [
        dob.strftime("%d%m%Y"),
        dob.strftime("%d%m%y"),
        dob.strftime("%d%m"),
        dob.strftime("%m%d%Y"),
        dob.strftime("%Y%m%d"),
        dob.strftime("%d-%m-%Y"),
        dob.strftime("%d/%m/%Y"),
        dob.strftime("%Y"),
    ]


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_COUNT = r"(?:\d+|" + "|".join(_NUMBER_WORDS) + ")"
_SURNAME = r"sur\s?name|last\s+name|family\s+name"
_NAME_PART = rf"first\s+name|given\s+name|{_SURNAME}|name"

_DOB_FORMATS = {
    "ddmmyyyy": "%d%m%Y",
    "ddmmyy": "%d%m%y",
    "ddmm": "%d%m",
    "mmddyyyy": "%m%d%Y",
    "mmdd": "%m%d",
    "yyyymmdd": "%Y%m%d",
    "yyyy": "%Y",
    "yy": "%y",
}

_PASSWORD_WORDS = re.compile(r"pass\s?word|passcode|pass\s?key")
_RULE_WORDS = re.compile(
    r"letters?|characters?|alphabets?|digits?|name|birth|\bdob\b|\bpan\b|format|"
    r"d{2}[\s/.-]?m{2}|y{2,4}|capital|upper|lower|small|caps"
)
_NAME_COUNTED = re.compile(
    rf"first\s+(?P<count>{_COUNT})\s+(?:letters?|characters?|alphabets?|chars?)"
    rf"(?:\s+in\s+\w+(?:\s+\w+)?)?\s*(?:of\s+)?(?:the\s+|your\s+)?(?P<which>{_NAME_PART})"
)
_NAME_PLAIN = re.compile(rf"(?:your\s+|the\s+)?(?P<which>first\s+name|given\s+name|{_SURNAME})\b")
# Bare "name" needs the possessive, or it also matches things like "file name".
_NAME_BARE = re.compile(r"your\s+(?P<which>name)\b")
_CARD_DIGITS = re.compile(
    rf"(?P<end>last|first)\s+(?P<count>{_COUNT})\s+digits?\s*(?:of\s+)?(?:the\s+|your\s+)?"
    r"(?:credit\s+|debit\s+)?(?:card|account)"
)
_DOB_WORDS = re.compile(r"date\s+of\s+birth|birth\s*date|\bdob\b|birthday")
_FORMAT_TOKEN = re.compile(r"\b(?:d{2}|m{2}|y{2,4})(?:[\s/.-]?(?:d{2}|m{2}|y{2,4}))*\b")
_PAN_WORDS = re.compile(r"\bpan\b")
_UPPER_WORDS = re.compile(r"capital|upper\s?case|\bcaps\b|block\s+letters")
_LOWER_WORDS = re.compile(r"lower\s?case|small\s+letters")


@dataclass(frozen=True)
class _Slot:
    """One component of a stated password rule, e.g. "first 4 letters of name"."""

    kind: str
    position: int
    count: int | None = None
    which: str = "first"
    formats: tuple[str, ...] = ()


def _count_of(word: str) -> int | None:
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word)


def stated_rule(hint: str, *, limit: int = 400) -> str:
    """The sentences of a mail that describe the password, as written.

    Statement mails mention names, dates and card digits all over the place, so
    reading the whole body would invent rules that were never stated.
    """
    # Plain text mails break the rule onto its own line, so a newline ends a
    # sentence just as a full stop does.
    parts = re.split(r"(?<=[.!?;:])\s+|\n+", (hint or "").strip())
    sentences = [re.sub(r"\s+", " ", part).strip() for part in parts if part.strip()]
    keep: list[int] = []
    for index, sentence in enumerate(sentences):
        if not _PASSWORD_WORDS.search(sentence.lower()):
            continue
        keep.append(index)
        # The rule is sometimes in the sentence after the announcement, but the
        # sign-off after it is not part of the rule.
        follower = sentences[index + 1].lower() if index + 1 < len(sentences) else ""
        if _RULE_WORDS.search(follower):
            keep.append(index + 1)
    wanted = sorted({i for i in keep if i < len(sentences)})
    rule = " ".join(sentences[i] for i in wanted).strip()
    return rule[: limit - 1] + "…" if len(rule) > limit else rule


def _slots(text: str) -> list[_Slot]:
    found: list[_Slot] = []

    for match in _NAME_COUNTED.finditer(text):
        which = "last" if re.search(_SURNAME, match.group("which")) else "first"
        found.append(
            _Slot("name", match.start(), count=_count_of(match.group("count")), which=which)
        )
    if not found:
        for pattern in (_NAME_PLAIN, _NAME_BARE):
            for match in pattern.finditer(text):
                which = "last" if re.search(_SURNAME, match.group("which")) else "first"
                found.append(_Slot("name", match.start(), which=which))
            if found:
                break

    for match in _CARD_DIGITS.finditer(text):
        found.append(
            _Slot(
                "digits",
                match.start(),
                count=_count_of(match.group("count")),
                which=match.group("end"),
            )
        )

    formats = []
    for match in _FORMAT_TOKEN.finditer(text):
        key = re.sub(r"[^a-z]", "", match.group(0))
        if key in _DOB_FORMATS:
            formats.append((match.start(), _DOB_FORMATS[key]))
    spelled_out = [match.start() for match in _DOB_WORDS.finditer(text)]
    if formats:
        # "date of birth in DDMM" is one component; anchor it at whichever came first.
        position = min([start for start, _ in formats] + spelled_out)
        found.append(_Slot("dob", position, formats=tuple(fmt for _, fmt in formats)))
    elif spelled_out:
        found.append(_Slot("dob", spelled_out[0], formats=("%d%m%Y", "%d%m", "%d%m%y")))

    for match in _PAN_WORDS.finditer(text):
        found.append(_Slot("pan", match.start()))

    # A mail often restates the same rule; one slot per component is enough.
    first_of_kind: dict[str, _Slot] = {}
    for slot in sorted(found, key=lambda s: s.position):
        first_of_kind.setdefault(slot.kind, slot)
    return sorted(first_of_kind.values(), key=lambda s: s.position)


def _casings(text: str) -> list:
    if _UPPER_WORDS.search(text):
        return [str.upper]
    if _LOWER_WORDS.search(text):
        return [str.lower]
    return [str.upper, str.lower, str.capitalize]


def _options(slot: _Slot, card: Card, casings: list) -> list[str]:
    """The values this component could take for a card, or [] if unknowable."""
    if slot.kind == "name":
        parts = re.sub(r"[^A-Za-z ]", "", card.name or "").split()
        if not parts:
            return []
        word = parts[-1] if slot.which == "last" else parts[0]
        fragment = word[: slot.count] if slot.count else word
        return [case(fragment) for case in casings] if fragment else []

    if slot.kind == "digits":
        # Only the last four are on file, so a rule needing more is unknowable.
        if not card.last4 or slot.which != "last" or not slot.count or slot.count > 4:
            return []
        return [card.last4[-slot.count :]]

    if slot.kind == "dob":
        if not card.dob:
            return []
        return [card.dob.strftime(fmt) for fmt in slot.formats]

    if slot.kind == "pan":
        pan = (card.pan or "").strip()
        return [case(pan) for case in casings] if pan else []

    return []


def candidates_from_hint(hint: str, cards: list[Card]) -> list[str]:
    """Build passwords from the rule the covering email states.

    Returns nothing when no rule is stated or a card lacks the details it needs,
    leaving `candidates_for_all` to guess.
    """
    context = stated_rule(hint, limit=2000).lower()
    if not context:
        return []
    slots = _slots(context)
    if not slots:
        return []

    casings = _casings(context)
    derived: list[str] = []
    for card in cards:
        per_slot = [_options(slot, card, casings) for slot in slots]
        if not all(per_slot):
            continue
        for combination in itertools.islice(
            itertools.product(*per_slot), MAX_DERIVED_PER_CARD
        ):
            derived.append("".join(combination))
    return dedupe(derived)


def dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication of password candidates."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def candidates_for(card: Card) -> list[str]:
    """Ordered password guesses, most likely first."""
    names = _name_fragments(card.name)
    dobs = _dob_fragments(card.dob)
    last4 = card.last4 or ""
    pan = (card.pan or "").strip()

    ordered: list[str] = list(card.extra_passwords)

    # Name plus date of birth is the most common pattern across issuers.
    for name in names:
        for dob in dobs:
            ordered.append(f"{name}{dob}")

    ordered.extend(dobs)

    if last4:
        ordered.extend([f"{name}{last4}" for name in names])
        ordered.extend([f"{last4}{dob}" for dob in dobs])
        ordered.extend([f"{dob}{last4}" for dob in dobs])
        ordered.append(last4)

    if pan:
        ordered.extend([pan.upper(), pan.lower()])
        ordered.extend([f"{pan.upper()}{dob}" for dob in dobs])
        if card.dob:
            ordered.append(f"{pan[:5].upper()}{card.dob.strftime('%d%m%Y')}")

    ordered.extend(names)

    return dedupe(ordered)[:MAX_CANDIDATES]


def candidates_for_all(cards: list[Card]) -> list[str]:
    """Try every registered card's passwords, since we do not yet know whose statement this is."""
    return dedupe([candidate for card in cards for candidate in candidates_for(card)])
