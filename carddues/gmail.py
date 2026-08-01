"""Read-only Gmail access for fetching statement emails.

The OAuth scope is gmail.readonly, so this can list and download but never
send, modify or delete mail.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

from . import config, issuers

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Google may return scopes beyond the ones asked for when an account has already
# granted others; without this oauthlib treats that as an error.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class GmailNotConfigured(Exception):
    """The Google OAuth client file is missing."""


class GmailAuthFailed(Exception):
    """The consent flow did not produce usable credentials."""


@dataclass
class Attachment:
    message_id: str
    attachment_id: str
    filename: str
    size: int


@dataclass
class Message:
    id: str
    sender: str
    subject: str
    internal_date: int
    attachments: list[Attachment] = field(default_factory=list)
    body: str = ""

    @property
    def issuer_key(self) -> str | None:
        return issuers.detect_from_sender(self.sender)

    @property
    def received_at(self) -> datetime | None:
        """When Gmail received the message; `internal_date` is epoch milliseconds."""
        if not self.internal_date:
            return None
        return datetime.fromtimestamp(self.internal_date / 1000)


def build_query(lookback_days: int = config.DEFAULT_LOOKBACK_DAYS) -> str:
    """Gmail search for statement mails with PDF attachments."""
    from .statement_gate import GMAIL_SUBJECT_EXCLUSIONS

    senders = " OR ".join(f"from:{domain}" for domain in issuers.all_senders())
    subjects = " OR ".join(f'subject:"{hint}"' for hint in issuers.STATEMENT_SUBJECT_HINTS)
    excluded = " ".join(f'-subject:"{token}"' for token in GMAIL_SUBJECT_EXCLUSIONS)
    return (
        f"has:attachment filename:pdf newer_than:{lookback_days}d "
        f"(({senders}) OR ({subjects})) {excluded}"
    ).strip()


def client_file() -> Path:
    """The OAuth client file, or raise if the user has not created one yet."""
    path = config.credentials_path()
    if not path.exists():
        raise GmailNotConfigured(
            f"Google OAuth client file not found at {path}. Create an OAuth client in "
            "Google Cloud Console, enable the Gmail API, and save the JSON there."
        )
    return path


def client_type() -> str | None:
    """Whether the client file is a `web` or `installed` (desktop) client."""
    path = config.credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("web", "installed"):
        if key in data:
            return key
    return None


def has_client() -> bool:
    return config.credentials_path().exists()


def is_connected() -> bool:
    """True when a token exists that can be used or refreshed without consent."""
    token_file = config.token_path()
    if not token_file.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except (OSError, ValueError):
        return False
    return bool(creds.valid or creds.refresh_token)


def save_credentials(creds: Credentials) -> None:
    config.ensure_dirs()
    token_file = config.token_path()
    token_file.write_text(creds.to_json())
    os.chmod(token_file, 0o600)


def disconnect() -> None:
    """Forget the token. The grant itself is revoked from your Google account."""
    config.token_path().unlink(missing_ok=True)


def _web_flow(redirect_uri: str, *, state: str | None = None) -> Flow:
    return Flow.from_client_secrets_file(
        str(client_file()), SCOPES, redirect_uri=redirect_uri, state=state
    )


def authorization_url(redirect_uri: str) -> tuple[str, str]:
    """Where to send the browser, plus the state to check when it comes back.

    `prompt=consent` is deliberate: without it Google omits the refresh token on
    repeat authorisations, and unattended refreshes would stop working.
    """
    flow = _web_flow(redirect_uri)
    return flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )


def exchange_code(redirect_uri: str, code: str, *, state: str | None = None) -> Credentials:
    """Trade the authorisation code for a token and store it."""
    flow = _web_flow(redirect_uri, state=state)
    # Passing the code rather than the full callback URL keeps oauthlib from
    # rejecting the http loopback redirect as insecure.
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds or not creds.refresh_token:
        raise GmailAuthFailed(
            "Google did not return a refresh token. Remove this app's access under "
            "your Google account permissions and connect again."
        )
    save_credentials(creds)
    return creds


def authorize(*, interactive: bool = True):
    """Return Gmail API credentials, running the desktop consent flow if needed."""
    config.ensure_dirs()
    token_file = config.token_path()
    creds: Credentials | None = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not interactive:
            raise GmailNotConfigured(
                "Gmail is not connected. Open the dashboard and use Connect Gmail, "
                "or run `carddues auth` from a terminal."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file()), SCOPES)
        creds = flow.run_local_server(port=0)

    save_credentials(creds)
    return creds


def service(*, interactive: bool = True):
    return build("gmail", "v1", credentials=authorize(interactive=interactive))


def _header(payload: dict, name: str) -> str:
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _walk_parts(part: dict, message_id: str, found: list[Attachment]) -> None:
    filename = part.get("filename") or ""
    body = part.get("body", {})
    if filename.lower().endswith(".pdf") and body.get("attachmentId"):
        found.append(
            Attachment(
                message_id=message_id,
                attachment_id=body["attachmentId"],
                filename=filename,
                size=int(body.get("size", 0)),
            )
        )
    for child in part.get("parts", []) or []:
        _walk_parts(child, message_id, found)


def _decode_body(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "replace")
    except (ValueError, binascii.Error):
        return ""


def _strip_html(markup: str) -> str:
    without_code = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    text = re.sub(r"(?s)<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _walk_text(part: dict, plain: list[str], markup: list[str]) -> None:
    data = part.get("body", {}).get("data")
    if data and not part.get("filename"):
        mime = (part.get("mimeType") or "").lower()
        if mime == "text/plain":
            plain.append(_decode_body(data))
        elif mime == "text/html":
            markup.append(_decode_body(data))
    for child in part.get("parts", []) or []:
        _walk_text(child, plain, markup)


def body_text(payload: dict) -> str:
    """The readable body of a message, preferring the plain text alternative."""
    plain: list[str] = []
    markup: list[str] = []
    _walk_text(payload, plain, markup)
    if any(chunk.strip() for chunk in plain):
        return "\n".join(plain)
    return _strip_html("\n".join(markup))


def search(gmail, query: str, *, max_results: int = 200) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < max_results:
        response = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=min(100, max_results - len(ids)),
            )
            .execute()
        )
        ids.extend(item["id"] for item in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids


def get_message(gmail, message_id: str) -> Message:
    raw = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = raw.get("payload", {})
    attachments: list[Attachment] = []
    _walk_parts(payload, message_id, attachments)
    return Message(
        id=message_id,
        sender=_header(payload, "From"),
        subject=_header(payload, "Subject"),
        internal_date=int(raw.get("internalDate", 0)),
        attachments=attachments,
        body=body_text(payload),
    )


def _safe_name(message_id: str, filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "statement.pdf"
    return f"{message_id}-{stem}"


def attachment_path(message_id: str, filename: str, target_dir: Path | None = None) -> Path:
    """Where a downloaded attachment lives, without needing Gmail."""
    directory = target_dir or config.attachments_dir()
    return directory / _safe_name(message_id, filename)


def download(gmail, attachment: Attachment, target_dir: Path | None = None) -> Path:
    destination = attachment_path(attachment.message_id, attachment.filename, target_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination

    blob = (
        gmail.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=attachment.message_id, id=attachment.attachment_id)
        .execute()
    )
    destination.write_bytes(base64.urlsafe_b64decode(blob["data"]))
    os.chmod(destination, 0o600)
    return destination
