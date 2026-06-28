"""Tests for the duitku webhook receiver.

Mirrors the Go test coverage:

- happy path (200/204 + file + sidecar)
- bad signature
- stale timestamp
- /healthz returns 200

Plus extra cases that were implicit in Go:

- missing timestamp header
- bad bank name (regex fail)
- unknown bank (allowlist fail)
- disallowed extension
- idempotent re-POST of identical bytes
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from duitku.webhook import build_app, load_config

KEY = b"super-secret-test-key-32-chars"


# ---- fixtures --------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    key_file = tmp_path / "key"
    key_file.write_bytes(KEY)
    cfg = load_config(
        landing_dir=tmp_path / "landing",
        hmac_key_file=key_file,
        max_replay_skew_seconds=300,
        max_attachment_size=1 << 20,
    )
    app = build_app(cfg)
    c = TestClient(app)
    c.landing_dir = cfg.landing_dir  # type: ignore[attr-defined]
    return c


# ---- helpers ---------------------------------------------------------


def _sign(body: bytes, ts: str, key: bytes = KEY) -> str:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(ts.encode())
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


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
    ts_override: str | None = None,
    sig_override: str | None = None,
    skip_ts_header: bool = False,
    skip_sig_header: bool = False,
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

    req = client.build_request("POST", "/cf-inbox", data=data, files=files)
    body = req.read()
    ct = req.headers["content-type"]

    ts = ts_override if ts_override is not None else str(int(time.time()))
    sig = sig_override if sig_override is not None else "sha256=" + _sign(body, ts)

    headers = {"Content-Type": ct}
    if not skip_ts_header:
        headers["X-Duitku-Timestamp"] = ts
    if not skip_sig_header:
        headers["X-Duitku-Signature"] = sig

    return client.post("/cf-inbox", content=body, headers=headers)


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


def test_cf_inbox_bad_signature(client: TestClient) -> None:
    r = _post(client, sig_override="sha256=deadbeef")
    assert r.status_code == 401


def test_cf_inbox_missing_signature_header(client: TestClient) -> None:
    r = _post(client, skip_sig_header=True)
    assert r.status_code == 401


def test_cf_inbox_missing_timestamp_header(client: TestClient) -> None:
    r = _post(client, skip_ts_header=True)
    assert r.status_code == 400


def test_cf_inbox_stale_timestamp(client: TestClient) -> None:
    stale = str(int(time.time()) - 3600)
    r = _post(client, ts_override=stale)
    # Signature recomputed in helper with the same stale ts, so HMAC
    # passes and we get rejected on drift.
    assert r.status_code == 401
    assert "drift" in r.json()["detail"]


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
