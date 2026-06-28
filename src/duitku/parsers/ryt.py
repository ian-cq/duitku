"""Ryt Bank (MY digital bank) PDF statement parser.

Ryt Bank is a fully digital bank with no public statement-parser
references on GitHub (searched 2026-06). This parser is **generic
and best-effort**, modelled on the savings/current shapes we see at
Maybank + UOB, on the assumption that Ryt's PDFs follow the
Malaysian retail-bank house style:

    DD/MM/YYYY  DESCRIPTION                    AMOUNT[+/-]   BALANCE
    15/06/2026  DUITNOW TRANSFER FROM ALI       1,000.00+    5,432.10
    16/06/2026  GRAB*RIDE KL                       42.50-    5,389.60

Some Ryt-style layouts may use ``CR``/``DR`` suffixes instead of
``+``/``-``; both shapes are accepted, the parser picks whichever
matches more rows.

Until we see a real statement this code is a placeholder that will
parse-or-fail-noisily; the operator's first real statement will land
in ``failed/`` with the layout fingerprint logged, at which point
this module gets tightened.

``account_id`` is the last four digits of any account number we
spot; same convention as the other parsers.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path

from duitku.models import RawTransaction, Statement
from duitku.parsers.common import (
    clean_description,
    infer_statement_year,
    layout_fingerprint,
    parse_amount,
    parse_date_any,
    parse_signed_amount,
)
from duitku.pdfutil import open_and_extract

log = logging.getLogger("duitku.parsers.ryt")

BANK = "ryt"
KNOWN_LAYOUTS: set[str] = set()

# ---- account / currency ---------------------------------------------

_ACCOUNT_NUMBER_RES = [
    re.compile(r"ACCOUNT\s+(?:NUMBER|NO)\.?\s*[:\-]?\s*([X*\d\s\-]{6,})", re.I),
    re.compile(r"CARD\s+(?:NUMBER|NO)\.?\s*[:\-]?\s*([X*\d\s\-]{6,})", re.I),
    # Ryt account display format is unknown; fall back to any masked-PAN
    # pattern that looks like ``****1234``.
    re.compile(r"[*X]{3,}(\d{4})\b"),
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
    re.compile(r"\b(MYR|RM)\b"),
    re.compile(r"CURRENCY\s*[:\-]?\s*([A-Z]{3})", re.I),
]


def extract_currency(text: str) -> str:
    for pat in _CURRENCY_RES:
        m = pat.search(text)
        if m:
            code = m.group(1).upper()
            if code == "RM":
                return "MYR"
            if len(code) == 3:
                return code
    return "MYR"


# ---- transaction regexes --------------------------------------------

_DATE_TOKEN = r"(?:\d{1,2}\s+[A-Z]{3}\s+\d{2,4}|\d{2}/\d{2}/\d{2,4}|\d{2}/\d{2})"

# Shape A: DATE  DESCRIPTION  AMOUNT[+/-]  BALANCE
_RYT_A_RE = re.compile(
    r"^(" + _DATE_TOKEN + r")\s+(.+?)\s+([\d,]+\.\d{2}[+-])\s+([\d,]+\.\d{2})\s*$",
    re.IGNORECASE,
)

# Shape B: DATE  DESCRIPTION  AMOUNT  (DR|CR)  BALANCE
_RYT_B_RE = re.compile(
    r"^(" + _DATE_TOKEN + r")\s+(.+?)\s+([\d,]+\.\d{2})\s+(DR|CR)\s+([\d,]+\.\d{2})\s*$",
    re.IGNORECASE,
)

_BANK_REF_RE = re.compile(r"\b(?:REF|REFERENCE)[\s:#]+([A-Z0-9]{6,})\b", re.I)

_OPENING_BALANCE_RE = re.compile(
    r"(?:OPENING|PREVIOUS|BEGINNING)\s+BALANCE.*?([\d,]+\.\d{2})", re.I
)
_CLOSING_BALANCE_RE = re.compile(
    r"(?:CLOSING|ENDING)\s+BALANCE.*?([\d,]+\.\d{2})", re.I
)


def _balance(text: str, pat: re.Pattern[str]) -> Decimal | None:
    m = pat.search(text)
    if not m:
        return None
    try:
        return parse_amount(m.group(1))
    except ValueError:
        return None


def _parse_shape_a(text: str, *, default_year: int) -> list[RawTransaction]:
    out: list[RawTransaction] = []
    for line in text.splitlines():
        m = _RYT_A_RE.match(line.strip())
        if not m:
            continue
        date_s, desc_raw, amount_signed_s, _bal_s = m.groups()
        try:
            txn_date = parse_date_any(date_s, default_year=default_year)
            amount, kind = parse_signed_amount(amount_signed_s)
        except ValueError:
            continue
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


def _parse_shape_b(text: str, *, default_year: int) -> list[RawTransaction]:
    out: list[RawTransaction] = []
    for line in text.splitlines():
        m = _RYT_B_RE.match(line.strip())
        if not m:
            continue
        date_s, desc_raw, amount_s, sign, _bal_s = m.groups()
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


def _best_parse(text: str, *, default_year: int) -> list[RawTransaction]:
    a = _parse_shape_a(text, default_year=default_year)
    b = _parse_shape_b(text, default_year=default_year)
    if len(b) > len(a):
        log.debug("ryt: shape B wins", extra={"a_count": len(a), "b_count": len(b)})
        return b
    return a


# ---- public entrypoints ---------------------------------------------


def parse(path: Path, *, passwords: list[str]) -> Statement:
    text_doc, _pw = open_and_extract(path, passwords=passwords)
    return _build(text_doc.joined, fname=path.name, mtime_year=_mtime_year(path))


def parse_text(text: str, *, statement_year: int | None = None) -> Statement:
    return _build(text, fname="<text>", mtime_year=statement_year)


def _build(text: str, *, fname: str, mtime_year: int | None) -> Statement:
    fingerprint = layout_fingerprint(text)
    if KNOWN_LAYOUTS and fingerprint not in KNOWN_LAYOUTS:
        log.warning(
            "unknown ryt layout fingerprint",
            extra={"fingerprint": fingerprint, "file": fname},
        )

    year = infer_statement_year(text) or mtime_year
    if year is None:
        raise ValueError("could not determine statement year for Ryt statement")

    txns = _best_parse(text, default_year=year)
    return Statement(
        bank=BANK,
        account_id=extract_account_id(text),
        currency=extract_currency(text),
        transactions=txns,
        period_start=min((t.date for t in txns), default=None),
        period_end=max((t.date for t in txns), default=None),
        opening_balance=_balance(text, _OPENING_BALANCE_RE),
        closing_balance=_balance(text, _CLOSING_BALANCE_RE),
        layout_version=fingerprint,
    )


def _mtime_year(path: Path) -> int:
    from datetime import datetime as _dt

    return _dt.fromtimestamp(path.stat().st_mtime).year
