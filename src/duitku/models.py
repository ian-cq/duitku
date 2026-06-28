"""Data models for duitku.

Plain ``dataclasses`` rather than pydantic - the data is hand-built
inside the process boundary, so the validation cost of pydantic isn't
buying us anything we can't get with type hints + a couple of
``__post_init__`` invariants.

The three core types:

- :class:`RawTransaction` — bank-specific shape that the parser surfaces.
- :class:`Statement` — what a parser returns for a single file.
- :class:`Transaction` — the canonical, bank-independent shape that
  the emitters consume.

The normaliser in ``duitku.normalise`` is the only legal path from
``RawTransaction`` to ``Transaction``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal


Kind = Literal["withdrawal", "deposit"]


@dataclass(frozen=True)
class RawTransaction:
    """Bank-specific transaction as it comes off the statement.

    The parser is allowed (encouraged, even) to leave fields in the
    bank's own units and conventions; the normaliser does the
    translation. The only invariant the parser MUST uphold is that
    every field below is set or explicitly ``None``.
    """

    date: date
    """Transaction date (the date the customer made the spend), not
    posting date. Parsers must pick the txn date when the statement
    shows both."""

    description: str
    """Raw description string, as extracted. Whitespace-normalised
    by the parser but otherwise untouched."""

    amount: Decimal
    """Magnitude of the transaction in the account's currency. MUST be
    a positive Decimal. Sign / kind comes from :attr:`kind`."""

    kind: Kind
    """``"withdrawal"`` if money left the account, ``"deposit"`` if
    money entered. Parsers translate bank-specific sign conventions
    here."""

    bank_reference: str | None = None
    """The bank's natural key (transaction reference number) for this
    row, if surfaced by the statement. Strongly preferred over a
    content hash for dedup; see ``duitku.normalise`` for the key
    construction."""

    foreign_amount: Decimal | None = None
    """Amount in the original foreign currency, when the row is an FX
    transaction. ``None`` for same-currency transactions."""

    foreign_currency: str | None = None
    """ISO-4217 code of :attr:`foreign_amount`. Required when
    ``foreign_amount`` is set; rejected by the normaliser otherwise."""


@dataclass(frozen=True)
class Statement:
    """What a parser returns for a single statement file."""

    bank: str
    """Bank slug, lowercased. e.g. ``"maybank"``."""

    account_id: str
    """The parser's idea of the account identifier. Format is
    parser-specific and documented in the parser module docstring; the
    only contract is that the operator's ``accounts.yaml`` uses the
    same string."""

    currency: str
    """ISO-4217 code of the account itself."""

    transactions: list[RawTransaction]

    period_start: date | None = None
    """First date covered by the statement. Optional because some CSV
    exports don't carry a period header; reconciliation falls back to
    inferring from min/max transaction dates."""

    period_end: date | None = None

    opening_balance: Decimal | None = None
    """For reconciliation. ``None`` means reconciliation is skipped
    and emitted Transactions carry ``reconciliation_skipped=true``."""

    closing_balance: Decimal | None = None

    layout_version: str = "unknown"
    """Structural fingerprint of the statement layout; see §4.7 of
    the design doc. ``"unknown"`` means the parser has not implemented
    fingerprinting and the file should be accepted on best-effort
    basis."""


@dataclass(frozen=True)
class Transaction:
    """Canonical, bank-independent transaction. What emitters consume."""

    bank: str
    account_id: str
    date: date
    amount: Decimal  # always positive
    currency: str
    kind: Kind
    description: str  # cleaned, single-line
    raw_description: str  # original, audit only
    external_id: str  # dedup key; see :func:`make_external_id`
    bank_reference: str | None = None
    foreign_amount: Decimal | None = None
    foreign_currency: str | None = None
    reconciliation_skipped: bool = False
    """Set to True when the source statement lacked opening/closing
    balance and the parser couldn't reconcile. The emitter must
    surface this so the Firefly note is visibly distinguishable."""

    notes: dict[str, str] = field(default_factory=dict)
    """Free-form audit notes that the emitter renders into Firefly's
    ``notes`` field. Currently includes ``raw_description`` and any
    ``reconciliation_skipped`` flag."""


# ---- external_id construction ---------------------------------------

_DESCRIPTION_SIG_RE = re.compile(r"[^A-Z0-9]")


def description_signature(description: str) -> str:
    """Stable, normalised signature for a description string.

    Used as the entropy source for content-hash external IDs. The
    output is uppercased, alphanumerics-only, capped at 24 characters,
    so that minor pdfplumber extraction-version drift (whitespace,
    punctuation) does not change the dedup key.
    """
    return _DESCRIPTION_SIG_RE.sub("", description.upper())[:24]


def make_external_id(
    *,
    bank: str,
    account_id: str,
    txn_date: date,
    amount: Decimal,
    kind: Kind,
    description: str,
    bank_reference: str | None,
) -> str:
    """Compute the dedup key for one transaction.

    Strategy (per design doc §4.4):

    1. If the parser surfaced ``bank_reference``, the key is
       ``ref:{bank}:{account_id}:{bank_reference}``. Restatements
       (refund-of-fee, reversal) keep the same key, so re-running the
       same statement after a fix lands on the same Firefly row and
       updates instead of duplicating.
    2. Otherwise, hash a tuple of (bank, account_id, date,
       amount-as-cents, kind, description-signature). The signature is
       *post-normalisation* so pdfplumber drift does not move the key.

    Prefix (``ref:`` vs ``h:``) is part of the key on purpose - it
    lets us know which dedup strategy a given Firefly row was imported
    with, which matters when a future migration re-keys data.
    """
    if bank_reference:
        return f"ref:{bank}:{account_id}:{bank_reference}"
    amount_cents = int((amount * 100).to_integral_value())
    payload = "|".join(
        [
            bank,
            account_id,
            txn_date.strftime("%Y%m%d"),
            str(amount_cents),
            kind,
            description_signature(description),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"h:{digest}"
