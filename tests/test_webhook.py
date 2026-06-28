"""Tests for the duitku webhook receiver.

Authn is enforced at the gateway (Envoy Gateway apiKeyAuth) and not
re-verified in the pod, so these tests exercise the pod's request
handling only — bank/extension allowlists, multipart parsing,
content-hash naming, idempotent writes, and ``/healthz``.

The ``X-Client-Id`` header is what EG's ``forwardClientIDHeader``
injects after a successful apiKeyAuth match; we assert it gets
plumbed into the sidecar JSON.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duitku.webhook import build_app, load_config


# ---- fixtures --------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = load_config(
        landing_dir=tmp_path / "landing",
        max_attachment_size=1 << 20,
    )
    app = build_app(cfg)
    c = TestClient(app)
    c.landing_dir = cfg.landing_dir  # type: ignore[attr-defined]
    return c


# ---- helpers ---------------------------------------------------------


def _post(
    client: TestClient,
    *,
    bank: str = "maybank",
    received_at: str | None = None,
    message_id: str = "<test@example.com>",
    sender: str = "user@example.com",
    attachment_filename: str = "statement.pdf",
    file_bytes: bytes = b"%PDF-1.4\n...fake pdf body...",
    file_name: str = "statement.pdf",
    client_id: str | None = "cf-worker",
):
    if received_at is None:
        received_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    data = {
        "bank": bank,
        "received_at": received_at,
        "message_id": message_id,
        "sender": sender,
        "attachment_filename": attachment_filename,
    }
    files = {"file": (file_name, file_bytes, "application/pdf")}

    headers: dict[str, str] = {}
    if client_id is not None:
        headers["X-Client-Id"] = client_id

    return client.post("/cf-inbox", data=data, files=files, headers=headers)


# ---- tests -----------------------------------------------------------


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok\n"


def test_cf_inbox_happy_path(client: TestClient) -> None:
    r = _post(client)
    assert r.status_code == 204, r.text

    inbox = client.landing_dir / "maybank" / "inbox"  # type: ignore[attr-defined]
    files = sorted(inbox.iterdir())
    pdfs = [f for f in files if f.suffix == ".pdf"]
    metas = [f for f in files if f.name.endswith(".meta.json")]
    assert len(pdfs) == 1
    assert len(metas) == 1
    # filename shape: 20260628T150000Z-<hex16>.pdf
    assert pdfs[0].stem.count("-") == 1
    # Sidecar carries the gateway-injected client id.
    meta = json.loads(metas[0].read_text())
    assert meta["client_id"] == "cf-worker"


def test_cf_inbox_no_client_id_header(client: TestClient) -> None:
    """Pod must still accept the request if EG didn't inject the header
    (e.g. local debug path); client_id in the sidecar is empty.
    """
    r = _post(client, client_id=None)
    assert r.status_code == 204
    inbox = client.landing_dir / "maybank" / "inbox"  # type: ignore[attr-defined]
    metas = [f for f in inbox.iterdir() if f.name.endswith(".meta.json")]
    assert json.loads(metas[0].read_text())["client_id"] == ""


def test_cf_inbox_bad_bank_name(client: TestClient) -> None:
    r = _post(client, bank="not a bank!")
    assert r.status_code == 400


def test_cf_inbox_unknown_bank(client: TestClient) -> None:
    r = _post(client, bank="hsbc")  # not in allowlist
    assert r.status_code == 400


def test_cf_inbox_disallowed_extension(client: TestClient) -> None:
    r = _post(client, attachment_filename="statement.exe", file_name="statement.exe")
    assert r.status_code == 400


def test_cf_inbox_idempotent(client: TestClient) -> None:
    """Re-posting identical bytes hashes to the same name; second write skipped."""
    body = b"%PDF-1.4\nidempotency test"
    r1 = _post(client, file_bytes=body, received_at="2026-06-28T15:00:00Z")
    r2 = _post(client, file_bytes=body, received_at="2026-06-28T15:00:00Z")
    assert r1.status_code == 204
    assert r2.status_code == 204
    inbox = client.landing_dir / "maybank" / "inbox"  # type: ignore[attr-defined]
    pdfs = [f for f in inbox.iterdir() if f.suffix == ".pdf"]
    assert len(pdfs) == 1
