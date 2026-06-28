"""Normalise :class:`RawTransaction` → :class:`Transaction`.

The normaliser is the only legal path between the two shapes. It:

- Computes the ``external_id`` dedup key (see :func:`models.make_external_id`).
- Cleans the description string (collapses whitespace, single-line).
- Preserves the original description in ``raw_description`` for audit.
- Carries the ``reconciliation_skipped`` flag through from the statement.
- Records the source statement's layout fingerprint + period under
  ``notes`` so a Firefly row can be traced back to its source.

The normaliser is intentionally minimal: no FX-rate lookup, no
category inference, no merchant-name cleanup. Those are downstream
concerns and Firefly itself does some of them.
"""

from __future__ import annotations

import logging
from typing import Iterable

from duitku.models import RawTransaction, Statement, Transaction, make_external_id
from duitku.parsers.common import clean_description

log = logging.getLogger("duitku.normalise")


def normalise(
    statement: Statement,
    *,
    reconciliation_skipped: bool | None = None,
) -> list[Transaction]:
    """Convert every :class:`RawTransaction` in *statement* to canonical form.

    ``reconciliation_skipped`` overrides auto-detection: pass ``True``
    when the caller deliberately bypassed reconciliation (e.g. the
    statement had no opening/closing balances and reconciliation was
    skipped instead of failed). Pass ``False`` when reconciliation
    passed. ``None`` (the default) means "infer from the statement"
    — skip iff opening/closing are both absent.
    """
    auto_skip = (
        statement.opening_balance is None or statement.closing_balance is None
    )
    skip = auto_skip if reconciliation_skipped is None else reconciliation_skipped

    out: list[Transaction] = []
    for raw in statement.transactions:
        out.append(_to_transaction(raw, statement, reconciliation_skipped=skip))
    return out


def _to_transaction(
    raw: RawTransaction,
    statement: Statement,
    *,
    reconciliation_skipped: bool,
) -> Transaction:
    desc_clean = clean_description(raw.description)
    notes: dict[str, str] = {
        "raw_description": raw.description,
        "source_layout": statement.layout_version,
    }
    if statement.period_start and statement.period_end:
        notes["statement_period"] = (
            f"{statement.period_start.isoformat()}..{statement.period_end.isoformat()}"
        )
    if reconciliation_skipped:
        notes["reconciliation"] = "skipped"

    if raw.foreign_amount is not None and not raw.foreign_currency:
        raise ValueError(
            f"foreign_amount set without foreign_currency on {raw.description!r}"
        )

    external_id = make_external_id(
        bank=statement.bank,
        account_id=statement.account_id,
        txn_date=raw.date,
        amount=raw.amount,
        kind=raw.kind,
        description=desc_clean,
        bank_reference=raw.bank_reference,
    )

    return Transaction(
        bank=statement.bank,
        account_id=statement.account_id,
        date=raw.date,
        amount=raw.amount,
        currency=statement.currency,
        kind=raw.kind,
        description=desc_clean,
        raw_description=raw.description,
        external_id=external_id,
        bank_reference=raw.bank_reference,
        foreign_amount=raw.foreign_amount,
        foreign_currency=raw.foreign_currency,
        reconciliation_skipped=reconciliation_skipped,
        notes=notes,
    )


# ---- reconciliation --------------------------------------------------


def reconcile(statement: Statement, *, tolerance_cents: int = 1) -> bool:
    """Verify that opening + signed-sum(txns) == closing.

    Returns ``True`` when the equation holds (or when reconciliation is
    skipped because the statement lacks balances). Returns ``False``
    when the equation fails by more than *tolerance_cents*; caller
    decides whether to abort the import or continue.

    A 1-cent tolerance covers the common rounding case where the bank
    prints amounts rounded to 2dp but reconciles internally with more
    precision (interest accrual on savings accounts being the usual
    culprit).
    """
    if statement.opening_balance is None or statement.closing_balance is None:
        return True
    signed_sum = sum(
        (t.amount if t.kind == "deposit" else -t.amount)
        for t in statement.transactions
    )
    expected = statement.closing_balance - statement.opening_balance
    delta = abs(signed_sum - expected)
    delta_cents = int((delta * 100).to_integral_value())
    ok = delta_cents <= tolerance_cents
    if not ok:
        log.warning(
            "reconciliation failed",
            extra={
                "bank": statement.bank,
                "account_id": statement.account_id,
                "opening": str(statement.opening_balance),
                "closing": str(statement.closing_balance),
                "signed_sum": str(signed_sum),
                "delta_cents": delta_cents,
                "txn_count": len(statement.transactions),
            },
        )
    return ok


__all__ = ["normalise", "reconcile"]
