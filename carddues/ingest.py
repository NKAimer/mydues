"""Gmail to stored dues: download statements, unlock, parse, save."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, db, gmail, issuers, passwords, pdfdoc
from .models import SOURCE_STATEMENT, Card, ParsedStatement, StatementRecord
from .parsers import parse_statement

logger = logging.getLogger(__name__)

STATUS_PARSED = "parsed"
STATUS_LOCKED = "locked"
STATUS_UNPARSED = "unparsed"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

# How many waiting attachments one reprocess pass works through. Each one that
# is no longer on disk costs a Gmail round trip, so a pass stays bounded and
# reports what is left.
REPROCESS_BATCH = 40


@dataclass
class IngestResult:
    status: str
    detail: str
    filename: str
    message_id: str | None = None
    statement: ParsedStatement | None = None
    card_id: int | None = None


@dataclass
class IngestSummary:
    results: list[IngestResult] = field(default_factory=list)
    # Waiting attachments a bounded pass did not reach.
    remaining: int = 0

    def count(self, status: str) -> int:
        return sum(1 for result in self.results if result.status == status)

    @property
    def parsed(self) -> int:
        return self.count(STATUS_PARSED)

    @property
    def needs_attention(self) -> list[IngestResult]:
        return [r for r in self.results if r.status in (STATUS_LOCKED, STATUS_UNPARSED, STATUS_ERROR)]


def _resolve_card(
    conn: sqlite3.Connection, statement: ParsedStatement, *, create_missing: bool
) -> Card | None:
    """Find the card this statement belongs to, registering it if it is new."""
    cards = db.list_cards(conn)

    if statement.last4:
        found = db.find_card(conn, issuer=statement.issuer, last4=statement.last4)
        if found:
            return found

    if not statement.last4:
        # Without card digits, only an unambiguous single card for the issuer works.
        same_issuer = [c for c in cards if statement.issuer and c.issuer == statement.issuer]
        return same_issuer[0] if len(same_issuer) == 1 else None

    if not create_missing:
        return None

    issuer_key = statement.issuer or "other"
    label = f"{issuers.get(issuer_key).name} ••{statement.last4}"
    card = Card(issuer=issuer_key, label=label, last4=statement.last4)
    card.id = db.add_card(conn, card)
    logger.info("Registered new card %s", label)
    return card


def store(
    conn: sqlite3.Connection,
    statement: ParsedStatement,
    *,
    source_ref: str | None,
    create_missing: bool = True,
) -> tuple[str, str, int | None]:
    card = _resolve_card(conn, statement, create_missing=create_missing)
    if card is None:
        return (
            STATUS_UNPARSED,
            "Could not tell which card this statement belongs to; add the card first",
            None,
        )

    record = StatementRecord(
        card_id=card.id,
        total_due=statement.total_due,
        min_due=statement.min_due,
        due_date=statement.due_date,
        statement_date=statement.statement_date,
        credit_limit=statement.credit_limit,
        available_credit=statement.available_credit,
        source=SOURCE_STATEMENT,
        source_ref=source_ref,
        parser=statement.parser,
        confidence=statement.confidence,
        as_of=datetime.now(),
    )
    statement_id = db.save_statement(conn, record)
    db.save_transactions(conn, card.id, statement_id, statement.transactions)

    detail = f"{card.label}: total due {statement.total_due:.2f}"
    if statement.transactions:
        detail += f", {len(statement.transactions)} transaction(s)"
    return STATUS_PARSED, detail, card.id


def _locked_detail(reason: str, cards: list[Card], issuer_hint: str | None) -> str:
    """Explain a locked file in terms of what is missing, not just the failure."""
    if not cards:
        return "No cards are registered yet, so no password could be worked out"
    if issuer_hint and not any(card.issuer == issuer_hint for card in cards):
        name = issuers.get(issuer_hint).name
        return f"No {name} card is registered, so no password could be worked out"
    known = [card for card in cards if not card.name or not card.dob]
    if len(known) == len(cards):
        return f"{reason}. No card has both a cardholder name and date of birth on it"
    return reason


def ingest_pdf(
    conn: sqlite3.Connection,
    path: Path,
    *,
    issuer_hint: str | None = None,
    source_ref: str | None = None,
    extra_passwords: list[str] | None = None,
    password_hint: str | None = None,
) -> IngestResult:
    """Parse one statement PDF and store what it yields.

    `password_hint` is the covering email's text. Issuers state their password
    rule there, so a password derived from it is tried before any guesswork.
    """
    path = Path(path)
    cards = db.list_cards(conn)
    candidates = list(extra_passwords or [])
    if password_hint:
        candidates += passwords.candidates_from_hint(password_hint, cards)
    candidates += passwords.candidates_for_all(cards)
    candidates = passwords.dedupe(candidates)

    try:
        extracted = pdfdoc.extract(path, candidates)
    except pdfdoc.LockedPdfError as exc:
        return IngestResult(
            STATUS_LOCKED, _locked_detail(str(exc), cards, issuer_hint), path.name, source_ref
        )
    except Exception as exc:  # noqa: BLE001 - a bad PDF must not stop the run
        logger.exception("Failed to read %s", path)
        return IngestResult(STATUS_ERROR, f"{type(exc).__name__}: {exc}", path.name, source_ref)

    statement = parse_statement(
        extracted.text, issuer_hint=issuer_hint, tables=extracted.tables
    )
    if statement is None:
        return IngestResult(
            STATUS_UNPARSED,
            "No total due found; this may not be a statement",
            path.name,
            source_ref,
        )

    status, detail, card_id = store(conn, statement, source_ref=source_ref)
    return IngestResult(status, detail, path.name, source_ref, statement, card_id)


class AttachmentGone(Exception):
    """The mail no longer carries the attachment that was logged."""


def _local_copy(
    message_id: str, filename: str, *, service_for
) -> tuple[Path, gmail.Message | None]:
    """The attachment on disk, fetched again when it is no longer kept.

    Returns the message too when one had to be opened, since its body carries
    the password rule and its sender identifies the issuer.
    """
    path = gmail.attachment_path(message_id, filename)
    if path.exists():
        return path, None

    message = gmail.get_message(service_for(), message_id)
    attachment = next((a for a in message.attachments if a.filename == filename), None)
    if attachment is None:
        raise AttachmentGone("That attachment is no longer in the mail")
    return gmail.download(service_for(), attachment), message


def _lazy_service(interactive: bool):
    """Open the Gmail service at most once, and only if something needs it."""
    held: list = []

    def get():
        if not held:
            held.append(gmail.service(interactive=interactive))
        return held[0]

    return get


def retry_locked(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    filename: str,
    password: str,
    interactive: bool = False,
) -> IngestResult:
    """Open a statement that stayed locked, using a password you supply."""
    hint: str | None = None
    issuer_hint: str | None = None

    try:
        path, message = _local_copy(
            message_id, filename, service_for=_lazy_service(interactive)
        )
    except AttachmentGone as exc:
        return IngestResult(STATUS_ERROR, str(exc), filename, message_id)
    except gmail.GmailNotConfigured as exc:
        return IngestResult(STATUS_ERROR, str(exc), filename, message_id)
    except Exception as exc:  # noqa: BLE001 - report and keep the log usable
        logger.exception("Could not re-download %s", filename)
        return IngestResult(STATUS_ERROR, f"Re-download failed: {exc}", filename, message_id)

    if message is not None:
        hint = message.body
        issuer_hint = message.issuer_key

    result = ingest_pdf(
        conn,
        path,
        issuer_hint=issuer_hint,
        source_ref=message_id,
        extra_passwords=[password],
        password_hint=hint,
    )
    result.filename = filename
    result.message_id = message_id
    _log(conn, message_id=message_id, filename=filename, result=result, message=message)

    if result.status == STATUS_PARSED:
        if result.card_id:
            db.remember_password(conn, result.card_id, password)
        path.unlink(missing_ok=True)
    return result


def _log(
    conn: sqlite3.Connection,
    *,
    message_id: str | None,
    filename: str,
    result: IngestResult,
    message: gmail.Message | None,
) -> None:
    """Record the outcome, filling in the mail context whenever it is at hand."""
    try:
        db.log_ingest(
            conn,
            message_id=message_id,
            filename=filename,
            status=result.status,
            detail=result.detail,
            sender=message.sender if message else None,
            subject=message.subject if message else None,
            received_at=message.received_at if message else None,
            issuer=message.issuer_key if message else None,
            password_rule=passwords.stated_rule(message.body) if message else None,
        )
    except sqlite3.Error:
        # A full descriptor table can make SQLite fail to reopen the DB; the
        # parse result is still useful, so the pass must keep going.
        logger.exception("Could not record ingest status for %s", filename)


def reprocess_pending(
    conn: sqlite3.Connection,
    *,
    issuer: str | None = None,
    limit: int = REPROCESS_BATCH,
    interactive: bool = False,
) -> IngestSummary:
    """Try the waiting attachments again with every card now on file.

    Driven by the ingest log rather than a Gmail search, so it reaches
    statements older than the search lookback. A card added today carries a
    name, a date of birth or a password that may open a file which failed
    months ago.
    """
    summary = IngestSummary()
    service_for = _lazy_service(interactive)

    for row in db.pending_ingest(conn, issuer=issuer, limit=limit):
        message_id = row["message_id"]
        filename = row["filename"]
        # A local import has no mail to go back to; `carddues import` re-runs it.
        if not (message_id and filename):
            continue

        try:
            path, message = _local_copy(message_id, filename, service_for=service_for)
        except gmail.GmailNotConfigured as exc:
            # Nothing else in this batch can be fetched either.
            summary.results.append(IngestResult(STATUS_ERROR, str(exc), filename, message_id))
            break
        except AttachmentGone as exc:
            result = IngestResult(STATUS_ERROR, str(exc), filename, message_id)
            summary.results.append(result)
            _log(conn, message_id=message_id, filename=filename, result=result, message=None)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad mail must not stop the pass
            logger.exception("Could not re-download %s", filename)
            result = IngestResult(STATUS_ERROR, f"Re-download failed: {exc}", filename, message_id)
            summary.results.append(result)
            _log(conn, message_id=message_id, filename=filename, result=result, message=None)
            continue

        result = ingest_pdf(
            conn,
            path,
            issuer_hint=message.issuer_key if message else row["issuer"],
            source_ref=message_id,
            password_hint=message.body if message else None,
        )
        result.filename = filename
        result.message_id = message_id
        summary.results.append(result)
        _log(conn, message_id=message_id, filename=filename, result=result, message=message)

        # Keep a locked file so that a typed password can retry it at once.
        if result.status != STATUS_LOCKED:
            path.unlink(missing_ok=True)

    summary.remaining = db.count_pending_ingest(conn, issuer=issuer)
    return summary


def ingest_gmail(
    conn: sqlite3.Connection,
    *,
    lookback_days: int = config.DEFAULT_LOOKBACK_DAYS,
    limit: int = 100,
    interactive: bool = True,
    keep_files: bool = False,
    query: str | None = None,
) -> IngestSummary:
    """Search Gmail for statement mails and ingest every PDF attachment."""
    summary = IngestSummary()
    service = gmail.service(interactive=interactive)
    search_query = query or gmail.build_query(lookback_days)
    logger.info("Gmail query: %s", search_query)

    message_ids = gmail.search(service, search_query, max_results=limit)
    already_seen = db.seen_attachments(conn)

    for message_id in message_ids:
        message = gmail.get_message(service, message_id)
        for attachment in message.attachments:
            if (message_id, attachment.filename) in already_seen:
                continue

            try:
                path = gmail.download(service, attachment)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Download failed for %s", attachment.filename)
                result = IngestResult(
                    STATUS_ERROR, f"Download failed: {exc}", attachment.filename, message_id
                )
                summary.results.append(result)
                _log(
                    conn,
                    message_id=message_id,
                    filename=attachment.filename,
                    result=result,
                    message=message,
                )
                continue

            result = ingest_pdf(
                conn,
                path,
                issuer_hint=message.issuer_key,
                source_ref=message_id,
                password_hint=message.body,
            )
            result.message_id = message_id
            summary.results.append(result)
            _log(
                conn,
                message_id=message_id,
                filename=attachment.filename,
                result=result,
                message=message,
            )

            # A locked file is kept so that supplying its password can retry
            # immediately instead of waiting for the next fetch.
            if not keep_files and result.status != STATUS_LOCKED:
                path.unlink(missing_ok=True)

    summary.remaining = db.count_pending_ingest(conn)
    return summary
