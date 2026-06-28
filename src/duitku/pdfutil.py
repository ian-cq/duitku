"""PDF utilities: open + decrypt + extract text.

Two-step contract:

1. :func:`open_pdf` tries the supplied passwords in order, returning
   the first one that decrypts (or ``""`` if the PDF is unencrypted).
   Raises :class:`PDFDecryptError` if every password fails.
2. :func:`extract_text` returns the text content page-by-page,
   layout-preserved enough that downstream regex parsers can match
   against fixed-width columns.

This module is deliberately thin: the heavy lifting is in pdfplumber
+ pikepdf and the parsers consume plain strings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("duitku.pdfutil")


class PDFDecryptError(Exception):
    """Raised when none of the supplied passwords decrypt a PDF.

    Carries no password material; the message is safe to log.
    """


@dataclass(frozen=True)
class PDFText:
    """Result of extracting text from a PDF.

    :attr:`pages` is one string per page, layout-preserved (extra
    whitespace, no table reconstruction). :attr:`joined` is the
    convenience concatenation parsers usually want.
    """

    pages: list[str]

    @property
    def joined(self) -> str:
        return "\n".join(self.pages)


def open_pdf(path: Path, *, passwords: list[str]) -> str:
    """Return the password that successfully opens *path*, or ``""``.

    Side-effect free apart from opening the file briefly. Raises
    :class:`PDFDecryptError` if every supplied password fails.
    """
    # Imported lazily so the webhook image (which does not install
    # ``duitku[parsers]``) doesn't ImportError at module load time.
    import pikepdf  # type: ignore[import-untyped]

    # First, try unencrypted open. pikepdf raises PasswordError if the
    # file needs a password, otherwise the open succeeds.
    try:
        with pikepdf.open(str(path)):
            return ""
    except pikepdf.PasswordError:
        pass

    # Try the empty-password case before anything user-supplied. Some
    # Maybank PDFs ship "encrypted" with the empty password as the
    # owner password; pikepdf will open them with "".
    candidates = ["", *passwords]
    for pw in candidates:
        try:
            with pikepdf.open(str(path), password=pw):
                return pw
        except pikepdf.PasswordError:
            continue
    raise PDFDecryptError(
        f"none of the supplied passwords decrypted {path.name}"
    )


def extract_text(path: Path, *, password: str) -> PDFText:
    """Return the text content of *path*, page by page.

    Uses pdfplumber's layout-preserved extraction. Empty or
    image-only pages produce an empty string in the list (preserved
    so page indices stay aligned with the source).
    """
    import pdfplumber  # type: ignore[import-untyped]

    pages: list[str] = []
    open_kwargs: dict[str, object] = {}
    if password:
        open_kwargs["password"] = password
    with pdfplumber.open(str(path), **open_kwargs) as pdf:
        for page in pdf.pages:
            txt = page.extract_text(layout=True) or ""
            pages.append(txt)
    return PDFText(pages=pages)


def open_and_extract(
    path: Path, *, passwords: list[str]
) -> tuple[PDFText, str]:
    """One-shot: try passwords, then extract text. Returns (text, password used)."""
    pw = open_pdf(path, passwords=passwords)
    return extract_text(path, password=pw), pw
