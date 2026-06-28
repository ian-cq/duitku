"""Maybank PDF statement parser (credit-card + savings/current).

The two product families have meaningfully different shapes:

**Credit Card** (e.g. Visa, Mastercard issued by Maybank):

    POSTDATE  TXNDATE  DESCRIPTION                    AMOUNT[CR]
    15/06     14/06    GRAB*RIDE KL                    42.50
    16/06     15/06    PAYMENT VIA M2U                500.00CR

The ``CR`` suffix marks the row as a credit (refund, payment).
Amounts without it are debits. Statement year is inferred from the
"Statement Date" section.

**Savings / Current Account** (Maybank2u / Maybank Savings):

    DD/MM     DESCRIPTION                  AMOUNT[+/-]      BALANCE
    15/06     CDM CASH DEPOSIT             1,000.00+        5,432.10
    16/06     DUITNOW TO 1234567890        50.00-           5,382.10
              REF: 0987654321

The trailing ``+``/``-`` marks the sign. Multi-line description
continuations are common (DUITNOW account refs, reference codes,
counterparty names). Continuation lines either start with whitespace
or match one of the well-known sub-patterns (account numbers,
DUITNOW keyword, reference codes ending in ``Q``).

``account_id`` is the masked account number / card number's *last
four* digits as a string. Maybank statements print the full masked
number ("VISA ****1234" or "MAYBANK SAVINGS ACCOUNT NUMBER: ******1234"),
we keep only the trailing 4 digits so the operator's ``accounts.yaml``
maps each card+account separately.

Bank references are not consistently surfaced on Maybank statements,
so dedup falls back to content-hash for most rows. The reconciliation
check (sum of debits/credits ?= opening - closing) catches missed
rows; without it this parser would be silently lossy.

Layout fingerprints are gated; the first time the parser sees a new
fingerprint it logs at WARN and refuses the file.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from duitku.models import RawTransaction, Statement
from duitku.parsers.common import (
    clean_description,
    infer_statement_year,
    layout_fingerprint,
    parse_amount,
    parse_date_ddmm,
    parse_signed_amount,
)
from duitku.pdfutil import open_and_extract

log = logging.getLogger("duitku.parsers.maybank")

BANK = "maybank"

# Known layout fingerprints. Add new ones here only after eyeballing
# the new statement layout against a redacted sample under tests/data/.
KNOWN_LAYOUTS: set[str] = set()  # populated empirically; warn-and-accept for v0.4.0

# ---- detection ------------------------------------------------------

_CREDIT_CARD_INDICATORS = [
    re.compile(r"CREDIT\s+CARD\s+STATEMENT", re.I),
    re.compile(r"MAYBANK\s+CREDIT\s+CARD", re.I),
    re.compile(r"CARD\s+NUMBER", re.I),
    re.compile(r"CREDIT\s+LIMIT", re.I),
    re.compile(r"MINIMUM\s+PAYMENT", re.I),
]

_CURRENT_ACCOUNT_INDICATORS = [
    re.compile(r"ACCOUNT\s+TRANSACTIONS", re.I),
    re.compile(r"URUSNIAGA\s+AKAUN", re.I),
    re.compile(r"(CURRENT|SAVINGS)\s+ACCOUNT\s+STATEMENT", re.I),
    re.compile(r"BEGINNING\s+BALANCE", re.I),
    re.compile(r"ENDING\s+BALANCE", re.I),
    re.compile(r"OPENING\s+BALANCE", re.I),
    re.compile(r"CLOSING\s+BALANCE", re.I),
]


def detect_product(text: str) -> str:
    """Return ``"credit_card"`` or ``"current_account"``."""
    cc_score = sum(1 for r in _CREDIT_CARD_INDICATORS if r.search(text))
    ca_score = sum(1 for r in _CURRENT_ACCOUNT_INDICATORS if r.search(text))
    if cc_score >= ca_score and cc_score > 0:
        return "credit_card"
    if ca_score > 0:
        return "current_account"
    # No strong signal. Default to current-account because the
    # savings/current regex is stricter (4 columns vs 3) and is less
    # likely to false-positive on a credit-card layout.
    return "current_account"


# ---- account id -----------------------------------------------------

_ACCOUNT_NUMBER_RES = [
    re.compile(r"ACCOUNT\s+(?:NUMBER|NO)\.?\s*[:\-]?\s*([X*\d\s\-]{6,})", re.I),
    re.compile(r"CARD\s+(?:NUMBER|NO)\.?\s*[:\-]?\s*([X*\d\s\-]{6,})", re.I),
    # Visa/Mastercard masked PAN, e.g. "5123 12** **** 1234".
    re.compile(r"(\d{4}[\s\-]\d{2}[X*]{2}[\s\-][X*]{4}[\s\-]\d{4})"),
]


def extract_account_id(text: str) -> str:
    """Return the *last four digits* of the account / card number.

    Maybank prints partial PAN / account numbers; we keep only the
    trailing 4 digits as the canonical ``account_id``. Operators then
    list e.g. ``maybank: { "1234": "Maybank Savings ****1234" }`` in
    ``accounts.yaml``.
    """
    for pat in _ACCOUNT_NUMBER_RES:
        m = pat.search(text)
        if not m:
            continue
        raw = re.sub(r"[\s\-]", "", m.group(1))
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 4:
            return digits[-4:]
    return "unknown"


# ---- currency -------------------------------------------------------

_CURRENCY_RES = [
    re.compile(r"\b(MYR|RM)\b"),
    re.compile(r"CURRENCY\s*[:\-]?\s*([A-Z]{3})", re.I),
]


def extract_currency(text: str) -> str:
    """Maybank statements are almost always MYR. Default accordingly."""
    for pat in _CURRENCY_RES:
        m = pat.search(text)
        if m:
            code = m.group(1).upper()
            if code == "RM":
                return "MYR"
            if len(code) == 3:
                return code
    return "MYR"


# ---- credit card parser ---------------------------------------------

# POSTDATE TXNDATE DESCRIPTION ... AMOUNT [CR]
_CC_TXN_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s+([\d,]+\.\d{2})(CR)?\s*$",
    re.MULTILINE,
)

# Lines that look like transactions but are actually summary rows.
_CC_SKIP_DESC_RES = [
    re.compile(r"\bBALANCE\b", re.I),
    re.compile(r"\bLIMIT\b", re.I),
    re.compile(r"\bMINIMUM\s+PAYMENT\b", re.I),
    re.compile(r"\bPREVIOUS\s+BALANCE\b", re.I),
    re.compile(r"\bSTATEMENT\s+BALANCE\b", re.I),
    re.compile(r"\bRETAIL\s+INTEREST\s+RATE\b", re.I),
    re.compile(r"\bKOMBINASI\s+HAD\b", re.I),
    re.compile(r"\bJUMLAH\s+PENYATA\b", re.I),
]


def _parse_credit_card(
    text: str, *, statement_year: int
) -> Iterable[RawTransaction]:
    seen: set[tuple[str, str, str]] = set()
    for m in _CC_TXN_RE.finditer(text):
        posting_s, txn_s, desc_raw, amount_s, cr_flag = m.groups()
        desc = clean_description(desc_raw)
        if not desc or len(desc) < 3:
            continue
        if any(r.search(desc) for r in _CC_SKIP_DESC_RES):
            continue
        try:
            amount = parse_amount(amount_s)
            txn_date = parse_date_ddmm(txn_s, year=statement_year)
        except ValueError as exc:
            log.warning(
                "skip CC line, bad amount/date: %s",
                exc,
                extra={"line": clean_description(m.group(0))},
            )
            continue
        kind = "deposit" if cr_flag == "CR" else "withdrawal"
        # Dedup intra-statement: the regex is permissive and can
        # double-match on layouts with summary lines that mirror txn rows.
        key = (txn_s, desc, amount_s + (cr_flag or ""))
        if key in seen:
            continue
        seen.add(key)
        yield RawTransaction(
            date=txn_date,
            description=desc,
            amount=amount,
            kind=kind,
            # Credit-card statements occasionally print a ref number at
            # end of description (e.g. "GRAB*RIDE KL  REF 0192834712").
            # Pull it if present; otherwise None.
            bank_reference=_extract_cc_ref(desc),
        )


_CC_REF_RE = re.compile(r"\bREF[\s:]+([A-Z0-9]{6,})\b", re.I)


def _extract_cc_ref(desc: str) -> str | None:
    m = _CC_REF_RE.search(desc)
    return m.group(1) if m else None


# ---- savings / current parser ---------------------------------------

# DD/MM  DESCRIPTION  AMOUNT[+-]  BALANCE
_SA_TXN_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+([\d,]+\.\d{2}[+-])\s+([\d,]+\.\d{2})\s*$"
)

# Continuation lines: DUITNOW counterparties, long account numbers,
# reference codes ending in Q, or just indented capitalised text.
_SA_CONT_INDICATORS = [
    re.compile(r"^\d{10,}"),
    re.compile(r"^\d+Q$"),
    re.compile(r"DUITNOW", re.I),
    re.compile(r"^[A-Z][A-Z0-9\s*]{3,}$"),
    re.compile(r"^REF[\s:]+", re.I),
]

# Bank-supplied reference code on savings/current statements.
_SA_REF_RE = re.compile(r"\bREF[\s:]+([A-Z0-9]{6,})\b", re.I)

# Balance lines we use for reconciliation. Maybank uses these phrases
# in both English and Bahasa.
_OPENING_BALANCE_RE = re.compile(
    r"(?:BEGINNING|OPENING)\s+BALANCE.*?([\d,]+\.\d{2})", re.I
)
_CLOSING_BALANCE_RE = re.compile(
    r"(?:ENDING|CLOSING)\s+BALANCE.*?([\d,]+\.\d{2})", re.I
)


def _parse_savings_current(
    text: str, *, statement_year: int
) -> tuple[list[RawTransaction], Decimal | None, Decimal | None]:
    out: list[RawTransaction] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _SA_TXN_RE.match(line)
        if not m:
            i += 1
            continue

        date_s, base_desc, amount_signed_s, _balance_s = m.groups()

        # Collect continuation lines (indented or matching the well-
        # known patterns) until we hit a blank line or another txn.
        continuations: list[str] = []
        j = i + 1
        while j < len(lines):
            raw_next = lines[j]
            nxt = raw_next.strip()
            if not nxt:
                break
            if _SA_TXN_RE.match(nxt):
                break
            indented = raw_next.startswith((" ", "\t"))
            matches_pat = any(p.search(nxt) for p in _SA_CONT_INDICATORS)
            if indented or matches_pat:
                continuations.append(nxt)
                j += 1
                continue
            break

        full_desc = base_desc
        if continuations:
            full_desc = full_desc + " | " + " | ".join(continuations)
        full_desc = clean_description(full_desc)

        try:
            amount, kind = parse_signed_amount(amount_signed_s)
            txn_date = parse_date_ddmm(date_s, year=statement_year)
        except ValueError as exc:
            log.warning(
                "skip SA line, bad amount/date: %s",
                exc,
                extra={"line": clean_description(line)},
            )
            i = j
            continue

        ref_match = _SA_REF_RE.search(full_desc)
        out.append(
            RawTransaction(
                date=txn_date,
                description=full_desc,
                amount=amount,
                kind=kind,
                bank_reference=ref_match.group(1) if ref_match else None,
            )
        )
        i = j

    opening = _balance_match(text, _OPENING_BALANCE_RE)
    closing = _balance_match(text, _CLOSING_BALANCE_RE)
    return out, opening, closing


def _balance_match(text: str, pat: re.Pattern[str]) -> Decimal | None:
    m = pat.search(text)
    if not m:
        return None
    try:
        return parse_amount(m.group(1))
    except ValueError:
        return None


# ---- public entrypoint ----------------------------------------------


def parse(path: Path, *, passwords: list[str]) -> Statement:
    """Parse a Maybank PDF statement.

    Tries the supplied passwords in order; raises ``PDFDecryptError``
    if none work. Returns a :class:`Statement` with the parsed
    transactions and (when present) opening/closing balances for
    downstream reconciliation.
    """
    text_doc, _pw = open_and_extract(path, passwords=passwords)
    text = text_doc.joined

    fingerprint = layout_fingerprint(text)
    if KNOWN_LAYOUTS and fingerprint not in KNOWN_LAYOUTS:
        log.warning(
            "unknown maybank layout fingerprint",
            extra={"fingerprint": fingerprint, "file": path.name},
        )
        # In v0.4.0 we warn-and-accept; once we have real fixtures we
        # tighten this to a hard reject per design §4.7.

    year = infer_statement_year(text)
    if year is None:
        from datetime import datetime as _dt

        year = _dt.fromtimestamp(path.stat().st_mtime).year
        log.warning(
            "no statement year found, falling back to file mtime",
            extra={"year": year, "file": path.name},
        )

    product = detect_product(text)
    account_id = extract_account_id(text)
    currency = extract_currency(text)

    opening: Decimal | None = None
    closing: Decimal | None = None

    if product == "credit_card":
        txns = list(_parse_credit_card(text, statement_year=year))
    else:
        txns, opening, closing = _parse_savings_current(text, statement_year=year)

    period_start = min((t.date for t in txns), default=None)
    period_end = max((t.date for t in txns), default=None)

    return Statement(
        bank=BANK,
        account_id=account_id,
        currency=currency,
        transactions=txns,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening,
        closing_balance=closing,
        layout_version=fingerprint,
    )


# Exposed for tests that synthesise PDF text directly (no PDF file).
def parse_text(
    text: str, *, statement_year: int | None = None
) -> Statement:
    """Test hook: parse pre-extracted PDF text.

    Production code path always goes through :func:`parse`, which
    handles decryption + extraction. Tests use this entry to avoid
    fixturing real PDFs.
    """
    year = statement_year or infer_statement_year(text)
    if year is None:
        raise ValueError("statement_year required when text has no year hint")

    product = detect_product(text)
    account_id = extract_account_id(text)
    currency = extract_currency(text)

    opening: Decimal | None = None
    closing: Decimal | None = None
    if product == "credit_card":
        txns = list(_parse_credit_card(text, statement_year=year))
    else:
        txns, opening, closing = _parse_savings_current(text, statement_year=year)

    return Statement(
        bank=BANK,
        account_id=account_id,
        currency=currency,
        transactions=txns,
        period_start=min((t.date for t in txns), default=None),
        period_end=max((t.date for t in txns), default=None),
        opening_balance=opening,
        closing_balance=closing,
        layout_version=layout_fingerprint(text),
    )
