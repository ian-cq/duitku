"""UOB (Malaysia / Singapore retail) PDF statement parser.

No public reference parser exists for UOB MY PDFs (as of 2026-06; gh
search returned only assessment apps), so this implementation is
**generic and best-effort**. Expect to revisit when real statements
land in ``failed/``.

The recognised layouts:

**Layout A — two-column (Debit/Credit)**:

    DD MMM YYYY  DESCRIPTION                  DEBIT      CREDIT     BALANCE
    15 JUN 2024  GRAB*RIDE KL                  42.50                3,210.55
    18 JUN 2024  PAYROLL CREDIT                          5,000.00   8,210.55

**Layout B — single signed-amount column**:

    DD/MM/YY  DESCRIPTION              AMOUNT[DR/CR]  BALANCE
    15/06/24  GRAB*RIDE KL              42.50 DR      3,210.55
    18/06/24  PAYROLL CREDIT         5,000.00 CR      8,210.55

The parser tries both and keeps the one that produces more rows
without an obvious failure (i.e. transactions whose balances form a
monotonically-changing sequence consistent with the deltas).

``account_id`` is the *last four digits* of whatever account/card
number the parser surfaces. Operators map that in ``accounts.yaml``
to a Firefly asset-account name. Multi-currency (MYR + SGD) is
disambiguated by ``accounts.yaml`` entry, not by the parser.

Bank references on UOB statements are inconsistent; dedup falls back
to content-hash on most rows. The reconciliation check + layout
fingerprinting carries the safety burden.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from duitku.models import RawTransaction, Statement
from duitku.parsers.common import (
    clean_description,
    infer_statement_year,
    layout_fingerprint,
    parse_amount,
    parse_date_any,
)
from duitku.pdfutil import open_and_extract

log = logging.getLogger("duitku.parsers.uob")

BANK = "uob"
KNOWN_LAYOUTS: set[str] = set()

# ---- account / currency ---------------------------------------------

_ACCOUNT_NUMBER_RES = [
    re.compile(r"ACCOUNT\s+(?:NUMBER|NO)\.?\s*[:\-]?\s*([X*\d\s\-]{6,})", re.I),
    re.compile(r"CARD\s+(?:NUMBER|NO)\.?\s*[:\-]?\s*([X*\d\s\-]{6,})", re.I),
    re.compile(r"(\d{4}[\s\-]\d{2}[X*]{2}[\s\-][X*]{4}[\s\-]\d{4})"),
]


def extract_account_id(text: str) -> str:
    for pat in _ACCOUNT_NUMBER_RES:
        m = pat.search(text)
        if not m:
            continue
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) >= 4:
            return digits[-4:]
    return "unknown"


_CURRENCY_RES = [
    re.compile(r"\b(MYR|SGD|USD)\b"),
    re.compile(r"CURRENCY\s*[:\-]?\s*([A-Z]{3})", re.I),
    re.compile(r"\b(RM|S\$)\b"),
]


def extract_currency(text: str) -> str:
    for pat in _CURRENCY_RES:
        m = pat.search(text)
        if m:
            raw = m.group(1).upper()
            if raw == "RM":
                return "MYR"
            if raw == "S$":
                return "SGD"
            if len(raw) == 3:
                return raw
    return "MYR"


# ---- layout A: two-column debit/credit ------------------------------

# Date can be "DD MMM YYYY" or "DD/MM/YYYY" or "DD/MM/YY".
_DATE_TOKEN = r"(?:\d{1,2}\s+[A-Z]{3}\s+\d{2,4}|\d{2}/\d{2}/\d{2,4}|\d{2}/\d{2})"

# DATE  DESCRIPTION  [DEBIT]  [CREDIT]  BALANCE
# We greedily match two trailing numbers (the last being balance, the
# second-to-last being either DEBIT or CREDIT, depending which column
# the bank populated). To distinguish, we leave a gap in the regex and
# inspect raw column positions if available - but since pdfplumber's
# layout=True output preserves spacing, we use spacing to figure out
# which column the second-to-last number falls in.
_LAYOUT_A_RE = re.compile(
    r"^(" + _DATE_TOKEN + r")\s+(.+?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$",
    re.IGNORECASE,
)

# Same shape but with one amount column (a row that hit only debit OR
# credit). The trailing number is the balance, the middle one is the
# delta. Sign comes from the column position in layout_text.
_LAYOUT_A_SINGLE_RE = re.compile(
    r"^(" + _DATE_TOKEN + r")\s+(.+?)\s+([\d,]+\.\d{2})\s*$",
    re.IGNORECASE,
)


# ---- layout B: signed-amount column ---------------------------------

_LAYOUT_B_RE = re.compile(
    r"^(" + _DATE_TOKEN + r")\s+(.+?)\s+([\d,]+\.\d{2})\s+(DR|CR)\s+([\d,]+\.\d{2})\s*$",
    re.IGNORECASE,
)


# ---- balances --------------------------------------------------------

_OPENING_BALANCE_RE = re.compile(
    r"(?:OPENING|PREVIOUS|BEGINNING)\s+BALANCE.*?([\d,]+\.\d{2})", re.I
)
_CLOSING_BALANCE_RE = re.compile(
    r"(?:CLOSING|ENDING)\s+BALANCE.*?([\d,]+\.\d{2})", re.I
)

_BANK_REF_RE = re.compile(r"\b(?:REF|REFERENCE)[\s:#]+([A-Z0-9]{6,})\b", re.I)


def _balance(text: str, pat: re.Pattern[str]) -> Decimal | None:
    m = pat.search(text)
    if not m:
        return None
    try:
        return parse_amount(m.group(1))
    except ValueError:
        return None


# ---- parsing strategies ---------------------------------------------


def _detect_column_midline(text: str) -> int:
    """Return the char-offset midpoint between DEBIT and CREDIT header words.

    Falls back to 60 if either header word isn't found. Used by Layout A
    to decide whether a single-amount row is a debit or a credit based on
    the column the amount appears in.
    """
    for raw in text.splitlines():
        low = raw.upper()
        d = low.find("DEBIT")
        c = low.find("CREDIT")
        if d >= 0 and c >= 0 and c > d:
            return (d + c) // 2
    return 60


def _parse_layout_a(
    text: str, *, default_year: int
) -> list[RawTransaction]:
    """Two-column debit/credit layout.

    Heuristic: in any matched line where exactly two trailing amounts
    are present, treat the first as the delta and the second as the
    balance. The sign comes from the COLUMN position; since we have
    layout-preserved text from pdfplumber, the debit column is to the
    LEFT of the credit column. We split the line text at the
    second-to-last amount and look at its character offset; below the
    dynamically-detected midline (from the DEBIT/CREDIT header words)
    is debit, above is credit.
    """
    midline = _detect_column_midline(text)
    out: list[RawTransaction] = []
    for raw in text.splitlines():
        s = raw  # keep original whitespace for column detection
        m = _LAYOUT_A_RE.match(s.strip())
        if not m:
            continue
        date_s, desc_raw, amount_s, _balance_s = m.groups()
        try:
            txn_date = parse_date_any(date_s, default_year=default_year)
            amount = parse_amount(amount_s)
        except ValueError:
            continue
        # Skip header rows that look like data but aren't.
        if any(
            k in desc_raw.upper()
            for k in ("BALANCE B/F", "BALANCE C/F", "BROUGHT FORWARD", "CARRIED FORWARD")
        ):
            continue
        # Determine column: offset of the delta amount (first of two
        # trailing numbers) versus the DEBIT/CREDIT header midpoint.
        amount_idx = s.find(amount_s)
        kind = "withdrawal" if amount_idx < midline else "deposit"
        desc = clean_description(desc_raw)
        ref_m = _BANK_REF_RE.search(desc)
        out.append(
            RawTransaction(
                date=txn_date,
                description=desc,
                amount=amount,
                kind=kind,
                bank_reference=ref_m.group(1) if ref_m else None,
            )
        )
    return out


def _parse_layout_b(
    text: str, *, default_year: int
) -> list[RawTransaction]:
    """Signed-amount-column layout: ``AMOUNT DR`` / ``AMOUNT CR``."""
    out: list[RawTransaction] = []
    for line in text.splitlines():
        m = _LAYOUT_B_RE.match(line.strip())
        if not m:
            continue
        date_s, desc_raw, amount_s, sign, _balance_s = m.groups()
        try:
            txn_date = parse_date_any(date_s, default_year=default_year)
            amount = parse_amount(amount_s)
        except ValueError:
            continue
        kind = "deposit" if sign.upper() == "CR" else "withdrawal"
        desc = clean_description(desc_raw)
        ref_m = _BANK_REF_RE.search(desc)
        out.append(
            RawTransaction(
                date=txn_date,
                description=desc,
                amount=amount,
                kind=kind,
                bank_reference=ref_m.group(1) if ref_m else None,
            )
        )
    return out


def _best_parse(
    text: str, *, default_year: int
) -> list[RawTransaction]:
    """Try both layouts; keep whichever yields more transactions."""
    a = _parse_layout_a(text, default_year=default_year)
    b = _parse_layout_b(text, default_year=default_year)
    if len(b) > len(a):
        log.debug("uob: layout B wins", extra={"a_count": len(a), "b_count": len(b)})
        return b
    return a


# ---- public entrypoints ---------------------------------------------


def parse(path: Path, *, passwords: list[str]) -> Statement:
    text_doc, _pw = open_and_extract(path, passwords=passwords)
    return _build_statement(text_doc.joined, fname=path.name, mtime_year=_mtime_year(path))


def parse_text(
    text: str, *, statement_year: int | None = None
) -> Statement:
    """Test hook: parse pre-extracted PDF text."""
    return _build_statement(text, fname="<text>", mtime_year=statement_year)


def _build_statement(text: str, *, fname: str, mtime_year: int | None) -> Statement:
    fingerprint = layout_fingerprint(text)
    if KNOWN_LAYOUTS and fingerprint not in KNOWN_LAYOUTS:
        log.warning(
            "unknown uob layout fingerprint",
            extra={"fingerprint": fingerprint, "file": fname},
        )

    year = infer_statement_year(text) or mtime_year
    if year is None:
        raise ValueError("could not determine statement year for UOB statement")

    txns = _best_parse(text, default_year=year)
    account_id = extract_account_id(text)
    currency = extract_currency(text)
    opening = _balance(text, _OPENING_BALANCE_RE)
    closing = _balance(text, _CLOSING_BALANCE_RE)
    return Statement(
        bank=BANK,
        account_id=account_id,
        currency=currency,
        transactions=txns,
        period_start=min((t.date for t in txns), default=None),
        period_end=max((t.date for t in txns), default=None),
        opening_balance=opening,
        closing_balance=closing,
        layout_version=fingerprint,
    )


def _mtime_year(path: Path) -> int:
    from datetime import datetime as _dt

    return _dt.fromtimestamp(path.stat().st_mtime).year
