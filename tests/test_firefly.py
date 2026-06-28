"""Tests for the Firefly emitter — HTTP mocked, no network calls."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from duitku.emitters.firefly import (
    EmitResult,
    FireflyClient,
    FireflyConfig,
    FireflyDuplicate,
    FireflyError,
    _to_firefly_body,
    emit,
)
from duitku.models import Transaction


def _txn(**overrides) -> Transaction:
    defaults: dict = dict(
        bank="maybank",
        account_id="1234",
        date=date(2024, 6, 15),
        amount=Decimal("42.50"),
        currency="MYR",
        kind="withdrawal",
        description="GRAB*RIDE KL",
        raw_description="GRAB*RIDE KL    ",
        external_id="h:abc123",
        notes={"raw_description": "GRAB*RIDE KL", "source_layout": "deadbeef"},
    )
    defaults.update(overrides)
    return Transaction(**defaults)


def _cfg() -> FireflyConfig:
    return FireflyConfig(base_url="https://firefly.test", token="t")


def test_to_firefly_body_withdrawal_uses_source_id():
    body = _to_firefly_body(_txn(), firefly_account_id=42)
    assert body["error_if_duplicate_hash"] is True
    txn = body["transactions"][0]
    assert txn["type"] == "withdrawal"
    assert txn["source_id"] == "42"
    assert "destination_id" not in txn
    assert txn["amount"] == "42.50"
    assert txn["external_id"] == "h:abc123"
    assert "duitku" in txn["tags"]
    assert "bank:maybank" in txn["tags"]


def test_to_firefly_body_deposit_uses_destination_id():
    body = _to_firefly_body(_txn(kind="deposit"), firefly_account_id=42)
    txn = body["transactions"][0]
    assert txn["type"] == "deposit"
    assert txn["destination_id"] == "42"
    assert "source_id" not in txn


def test_to_firefly_body_foreign_currency():
    body = _to_firefly_body(
        _txn(foreign_amount=Decimal("10.00"), foreign_currency="USD"),
        firefly_account_id=42,
    )
    txn = body["transactions"][0]
    assert txn["foreign_amount"] == "10.00"
    assert txn["foreign_currency_code"] == "USD"


def test_client_probe_extracts_version():
    client = FireflyClient(_cfg())
    with patch.object(client, "_request", return_value=(200, {"data": {"version": "6.1.0"}})):
        assert client.probe() == "6.1.0"


def test_client_probe_raises_on_non_200():
    client = FireflyClient(_cfg())
    with patch.object(client, "_request", return_value=(401, {"message": "bad token"})):
        with pytest.raises(FireflyError):
            client.probe()


def test_resolve_account_id_caches():
    client = FireflyClient(_cfg())
    resp = (
        200,
        {
            "data": [
                {"id": "7", "attributes": {"name": "Maybank Savings ****1234"}},
                {"id": "8", "attributes": {"name": "UOB One ****5678"}},
            ]
        },
    )
    with patch.object(client, "_request", return_value=resp) as m:
        assert client.resolve_account_id("Maybank Savings ****1234") == 7
        assert client.resolve_account_id("Maybank Savings ****1234") == 7
        assert m.call_count == 1


def test_post_transaction_treats_422_duplicate_as_duplicate():
    client = FireflyClient(_cfg())
    dup = (422, {"message": "Duplicate of transaction #99"})
    with patch.object(client, "_request", return_value=dup):
        with pytest.raises(FireflyDuplicate):
            client.post_transaction(_txn(), firefly_account_id=1)


def test_post_transaction_treats_other_422_as_error():
    client = FireflyClient(_cfg())
    with patch.object(client, "_request", return_value=(422, {"message": "bad currency"})):
        with pytest.raises(FireflyError):
            client.post_transaction(_txn(), firefly_account_id=1)


def test_emit_counts_inserts_duplicates_and_errors():
    client = FireflyClient(_cfg())
    accounts = {"maybank": {"1234": "Maybank Savings ****1234"}}
    txns = [
        _txn(external_id="h:a"),
        _txn(external_id="h:b"),
        _txn(external_id="h:c"),
    ]

    with patch.object(client, "resolve_account_id", return_value=42), patch.object(
        client, "post_transaction"
    ) as post:
        post.side_effect = [None, FireflyDuplicate("h:b"), FireflyError("nope")]
        result = emit(txns, client=client, account_map=accounts)
    assert result == EmitResult(inserted=1, duplicates=1, errors=1)


def test_emit_raises_when_account_map_missing():
    client = FireflyClient(_cfg())
    accounts: dict = {"maybank": {}}
    with patch.object(client, "resolve_account_id", return_value=42):
        with pytest.raises(FireflyError):
            emit([_txn()], client=client, account_map=accounts)
