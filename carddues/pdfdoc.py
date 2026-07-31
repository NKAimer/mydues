"""Open statement PDFs, unlocking them first when they are password protected."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pikepdf

from .text import normalize

logger = logging.getLogger(__name__)


class LockedPdfError(Exception):
    """No supplied password opened the file."""


@dataclass
class ExtractedPdf:
    text: str
    page_count: int
    password: str | None
    was_encrypted: bool
    # Ruled tables as pdfplumber sees them, which is where line items live when
    # a statement draws its transactions in a grid.
    tables: list[list[list[str | None]]] = field(default_factory=list)


def is_encrypted(path: Path) -> bool:
    try:
        with pikepdf.open(path):
            return False
    except pikepdf.PasswordError:
        return True


def _decrypt_to_temp(path: Path, password: str) -> Path:
    target = Path(tempfile.mkstemp(suffix=".pdf", prefix="carddues-")[1])
    with pikepdf.open(path, password=password) as pdf:
        pdf.save(target)
    return target


def _read(path: Path) -> tuple[str, int, list[list[list[str | None]]]]:
    pages: list[str] = []
    tables: list[list[list[str | None]]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
            try:
                tables.extend(page.extract_tables())
            except Exception:  # noqa: BLE001 - text alone is still worth having
                logger.debug("Table extraction failed on a page of %s", path.name)
        count = len(pdf.pages)
    return normalize("\n".join(pages)), count, tables


def extract(path: Path, passwords: list[str] | None = None) -> ExtractedPdf:
    """Return the text of a statement, trying each password until one works."""
    path = Path(path)
    if not is_encrypted(path):
        text, count, tables = _read(path)
        return ExtractedPdf(
            text=text, page_count=count, password=None, was_encrypted=False, tables=tables
        )

    for password in passwords or []:
        try:
            decrypted = _decrypt_to_temp(path, password)
        except pikepdf.PasswordError:
            continue
        try:
            text, count, tables = _read(decrypted)
        finally:
            decrypted.unlink(missing_ok=True)
        return ExtractedPdf(
            text=text, page_count=count, password=password, was_encrypted=True, tables=tables
        )

    raise LockedPdfError(
        f"{path.name} is password protected and none of the "
        f"{len(passwords or [])} candidate passwords worked"
    )
