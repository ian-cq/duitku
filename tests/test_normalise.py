"""Tests for normalisation + reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from duitku.models import RawTransaction, Statement
from duitku.normalise import normalise, reconcile


def _statement(
    *,
    transactions: list[RawTransaction],
    opening: Decimal | None = None,
    closing: Decimal | None = None,
) -> Statement:
    return Statement(
        bank="maybank",
        account_id="1234",
        currency="MYR",
        transactions=transactions,
        opening_balance=opening,
        closing_balance=closing,
    )


def test_normalise_uses_bank_reference_when_present():
    s = _statement(
        transactions=[
            RawTransaction(
                date=date(2024, 6, 15),
                description="DUITNOW TO ALI",
                amount=Decimal("100.00"),
                kind="withdrawal",
                bank_reference="REF12345678",
            )
        ]
    )
    [t] = normalise(s)
    assert t.external_id == "ref:maybank:1234:REF12345678"
    assert t.bank == "maybank"
    assert t.amount == Decimal("100.00")
    assert t.kind == "withdrawal"


def test_normalise_falls_back_to_hash():
    s = _statement(
        transactions=[
            RawTransaction(
                date=date(2024, 6, 15),
                description="GRAB*RIDE KL",
                amount=Decimal("42.50"),
                kind="withdrawal",
            )
        ]
    )
    [t] = normalise(s)
    assert t.external_id.startswith("h:")
    assert len(t.external_id) == len("h:") + 24


def test_normalise_external_id_is_stable_for_same_inputs():
    raw = RawTransaction(
        date=date(2024, 6, 15),
        description="GRAB*RIDE KL",
        amount=Decimal("42.50"),
        kind="withdrawal",
    )
    s1 = _statement(transactions=[raw])
    s2 = _statement(transactions=[raw])
    assert normalise(s1)[0].external_id == normalise(s2)[0].external_id


def test_reconcile_passes_within_tolerance():
    s = _statement(
        transactions=[
            RawTransaction(
                date=date(2024, 6, 15),
                description="X",
                amount=Decimal("100.00"),
                kind="deposit",
            ),
            RawTransaction(
                date=date(2024, 6, 16),
                description="Y",
                amount=Decimal("30.00"),
                kind="withdrawal",
            ),
        ],
        opening=Decimal("1000.00"),
        closing=Decimal("1070.00"),
    )
    assert reconcile(s)


def test_reconcile_fails_when_unbalanced():
    s = _statement(
        transactions=[
            RawTransaction(
                date=date(2024, 6, 15),
                description="X",
                amount=Decimal("100.00"),
                kind="deposit",
            )
        ],
        opening=Decimal("1000.00"),
        closing=Decimal("1200.00"),  # off by 100
    )
    assert not reconcile(s)


def test_reconcile_skips_when_balances_absent():
    s = _statement(
        transactions=[
            RawTransaction(
                date=date(2024, 6, 15),
                description="X",
                amount=Decimal("100.00"),
                kind="deposit",
            )
        ]
    )
    # No opening/closing -> reconcile is a no-op success.
    assert reconcile(s)


def test_normalise_marks_reconciliation_skipped_when_balances_absent():
    s = _statement(
        transactions=[
            RawTransaction(
                date=date(2024, 6, 15),
                description="X",
                amount=Decimal("100.00"),
                kind="deposit",
            )
        ]
    )
    [t] = normalise(s)
    assert t.reconciliation_skipped is True
    assert t.notes.get("reconciliation") == "skipped"
