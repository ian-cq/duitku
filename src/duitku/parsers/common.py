"""Shared helpers for per-bank parsers.

Date parsing, amount cleaning, year inference. Each helper is small
enough to be obvious from the type signature; the per-bank parsers
combine these with bank-specific regexes.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

# ---- amount ---------------------------------------------------------

# Matches "1,234.56" or "1234.56" (no leading sign).
_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")


def parse_amount(raw: str) -> Decimal:
    """Convert a statement-formatted amount string to ``Decimal``.

    Strips thousands separators. Raises :class:`ValueError` on
    anything that doesn't look like a money amount; callers must
    handle the exception (don't catch and silently drop a transaction
    line, you'll miss a parser bug).
    """
    raw = raw.strip().replace(",", "")
    if not raw:
        raise ValueError("empty amount")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"bad amount: {raw!r}") from exc


def parse_signed_amount(raw: str) -> tuple[Decimal, str]:
    """Parse an amount with trailing ``+`` or ``-`` (Maybank format).

    Returns ``(magnitude, "deposit" | "withdrawal")``. The sign char
    is consumed; the returned magnitude is always positive.

    Maybank's savings/current statements format amounts as
    ``1,234.56+`` (credit) or ``1,234.56-`` (debit), so this is the
    common shape.
    """
    raw = raw.strip()
    if raw.endswith("+"):
        return parse_amount(raw[:-1]), "deposit"
    if raw.endswith("-"):
        return parse_amount(raw[:-1]), "withdrawal"
    raise ValueError(f"signed-amount missing sign suffix: {raw!r}")


# ---- date -----------------------------------------------------------

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# DD/MM (no year). Statement year is inferred separately.
DDMM_RE = re.compile(r"^(\d{2})/(\d{2})$")

# DD/MM/YY or DD/MM/YYYY.
DDMMYY_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{2}|\d{4})$")

# DD MMM YYYY or DD MMM YY (e.g. "15 JUN 2024").
DD_MMM_YYYY_RE = re.compile(
    r"^(\d{1,2})\s+([A-Z]{3})\s+(\d{2}|\d{4})$", re.IGNORECASE
)


def parse_date_ddmm(raw: str, *, year: int) -> date:
    """Parse ``DD/MM`` against a known statement *year*. ValueError on bad input."""
    m = DDMM_RE.match(raw.strip())
    if not m:
        raise ValueError(f"not a DD/MM date: {raw!r}")
    d, mo = int(m.group(1)), int(m.group(2))
    return date(year, mo, d)


def parse_date_any(raw: str, *, default_year: int | None = None) -> date:
    """Best-effort parse of the date formats we see across banks.

    Tries DD/MM/YYYY, DD/MM/YY, DD MMM YYYY, DD MMM YY, then DD/MM
    (requires ``default_year``). Raises :class:`ValueError` if none
    match.
    """
    raw = raw.strip().upper()

    m = DDMMYY_RE.match(raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y = 2000 + y
        return date(y, mo, d)

    m = DD_MMM_YYYY_RE.match(raw)
    if m:
        d, mo_name, y = int(m.group(1)), m.group(2).upper(), int(m.group(3))
        if y < 100:
            y = 2000 + y
        if mo_name not in _MONTHS:
            raise ValueError(f"bad month name: {mo_name!r}")
        return date(y, _MONTHS[mo_name], d)

    if DDMM_RE.match(raw):
        if default_year is None:
            raise ValueError(f"DD/MM date {raw!r} needs a default_year")
        return parse_date_ddmm(raw, year=default_year)

    raise ValueError(f"unrecognised date format: {raw!r}")


# ---- statement-year inference ---------------------------------------

# "Statement Date" or "STATEMENT DATE" or "TARIKH PENYATA" lines often
# carry the most reliable year reference. Fall back to any 4-digit year
# in a "JANUARY 2024" or "20XX Year End Summary" context. Last resort:
# the file's mtime year.

_YEAR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Statement\s+Date.*?(\d{1,2})\s+([A-Z]{3,9})\s+(\d{4}|\d{2})", re.I | re.S),
    re.compile(r"Payment\s+Due\s+Date.*?(\d{1,2})\s+([A-Z]{3,9})\s+(\d{4}|\d{2})", re.I | re.S),
    re.compile(r"Statement\s+Period.*?(20\d{2})", re.I | re.S),
    re.compile(r"(20\d{2})\s+Year\s+End\s+Summary", re.I),
    re.compile(r"\b(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:TEMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)\s+(20\d{2})\b", re.I),
]


def infer_statement_year(text: str) -> int | None:
    """Return the most plausible statement year from PDF text.

    Tries a series of patterns from most-specific (Statement Date
    section) to least-specific (any month-year pair). Returns ``None``
    if nothing matches; the caller should fall back to the file mtime
    year and log a warning.
    """
    for pat in _YEAR_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        for g in reversed(groups):
            if g is None:
                continue
            if g.isdigit():
                y = int(g)
                if y < 100:
                    return 2000 + y
                if 2000 <= y <= 2099:
                    return y
    return None


# ---- description cleanup --------------------------------------------

_WS_RUN_RE = re.compile(r"\s+")


def clean_description(s: str) -> str:
    """Collapse whitespace runs, strip. Single-line invariant."""
    return _WS_RUN_RE.sub(" ", s.replace("\n", " ").replace("\r", " ")).strip()


# ---- layout fingerprinting ------------------------------------------


def layout_fingerprint(text: str, *, header_chars: int = 200) -> str:
    """Hash the first ``header_chars`` of page 1 + total page count.

    Cheap structural fingerprint. New statement layouts will move the
    hash; parsers gate on a known-set and refuse unknowns (logs the
    new fingerprint so a human can add it to the known-set after
    eyeballing a sample).
    """
    head = text[:header_chars]
    page_marker_count = text.count("\x0c")  # form-feed = page break in pdfplumber
    payload = f"{page_marker_count}|{head}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---- iteration helpers ----------------------------------------------


def lines(text: str) -> Iterable[str]:
    """Yield non-empty stripped lines of *text*.

    Convenience because every parser does this dance.
    """
    for line in text.splitlines():
        s = line.strip()
        if s:
            yield s
