"""HTTP receiver invoked by the Cloudflare Email Worker.

The Worker POSTs each extracted attachment to ``/cf-inbox`` with an
HMAC-SHA256 signature over ``timestamp || "." || body``. This module
verifies the signature, enforces a tight allowlist on bank names and
file extensions, and writes the attachment plus a JSON sidecar to the
landing directory.

The receiver deliberately does **not** parse, normalise, or talk to
Firefly. That separation is load-bearing: a parser bug must never be
able to make the Worker retry (because retries cause duplicate inbox
files, and even though we hash-name files, retry storms are noisy).
The sweep CronJob picks up files from the landing directory
asynchronously.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from fastapi import FastAPI, HTTPException, Request, Response, status
from starlette.datastructures import UploadFile

log = logging.getLogger("duitku.webhook")

# ---- constants -------------------------------------------------------

#: Strict whitelist of banks. Extend when a new parser lands.
ALLOWED_BANKS: Final[frozenset[str]] = frozenset({"maybank", "uob", "ryt"})

#: Strict whitelist of acceptable attachment extensions.
ALLOWED_EXT: Final[frozenset[str]] = frozenset({".pdf", ".csv", ".xls", ".xlsx"})

#: Extra paranoid filter on the bank form field.
_BANK_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]{2,32}$")

#: 25 MiB matches Gmail's outbound limit.
DEFAULT_MAX_ATTACHMENT_SIZE: Final[int] = 25 * 1024 * 1024

#: 5 minutes is comfortable given Workers + cluster are both NTP-synced.
DEFAULT_MAX_REPLAY_SKEW_SECONDS: Final[int] = 5 * 60


# ---- config ----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Webhook handler configuration. Validated by :func:`build_app`."""

    landing_dir: Path
    hmac_key: bytes
    max_replay_skew_seconds: int = DEFAULT_MAX_REPLAY_SKEW_SECONDS
    max_attachment_size: int = DEFAULT_MAX_ATTACHMENT_SIZE


def load_config(
    *,
    landing_dir: str | os.PathLike[str],
    hmac_key_file: str | os.PathLike[str],
    max_replay_skew_seconds: int = DEFAULT_MAX_REPLAY_SKEW_SECONDS,
    max_attachment_size: int = DEFAULT_MAX_ATTACHMENT_SIZE,
) -> Config:
    """Read the HMAC key from disk and validate everything at boot.

    A misconfiguration here fails fast instead of producing 500s on
    every request.
    """
    landing = Path(landing_dir)
    landing.mkdir(parents=True, exist_ok=True)

    key_path = Path(hmac_key_file)
    key = key_path.read_text(encoding="utf-8").strip()
    if len(key) < 16:
        raise ValueError(
            f"HMAC key in {key_path!s} is too short ({len(key)} chars); want >=16"
        )

    return Config(
        landing_dir=landing,
        hmac_key=key.encode("utf-8"),
        max_replay_skew_seconds=max_replay_skew_seconds,
        max_attachment_size=max_attachment_size,
    )


# ---- HMAC helpers ----------------------------------------------------


def compute_hmac(key: bytes, body: bytes, ts: str) -> str:
    """Return ``hex(HMAC-SHA256(key, timestamp || "." || body))``.

    Including the timestamp inside the MAC binds the body and the
    timestamp together so an attacker cannot replay a captured request
    with a fresh timestamp.
    """
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(ts.encode("utf-8"))
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


def _sender_domain(sender: str) -> str:
    """Return the part after ``@`` lowercased, or ``""`` if absent.

    Used for log fields; we deliberately do not log the local-part.
    """
    at = sender.rfind("@")
    if at == -1:
        return ""
    return sender[at + 1 :].lower()


# ---- app -------------------------------------------------------------


def build_app(cfg: Config) -> FastAPI:
    """Construct the FastAPI app bound to ``cfg``.

    The app exposes:

    - ``GET  /healthz``  — liveness, returns ``200 ok\\n``.
    - ``POST /cf-inbox`` — the Cloudflare Worker endpoint.
    """
    app = FastAPI(title="duitku-webhook", version="0.2.0", docs_url=None, redoc_url=None)

    @app.get("/healthz", response_class=Response)
    def healthz() -> Response:
        return Response(content="ok\n", media_type="text/plain; charset=utf-8")

    @app.post("/cf-inbox", status_code=status.HTTP_204_NO_CONTENT)
    async def cf_inbox(request: Request) -> Response:
        return await _handle_cf_inbox(request, cfg)

    return app


async def _handle_cf_inbox(request: Request, cfg: Config) -> Response:
    """Validate HMAC + multipart, write attachment + sidecar atomically."""

    # 1. Read raw body up-front so we can HMAC over the exact bytes.
    #    Starlette caches the result, so a later request.form() call
    #    re-parses from the same bytes without re-reading the stream.
    body_limit = cfg.max_attachment_size + (1 << 20)  # + small form overhead
    raw = await request.body()
    if len(raw) > body_limit:
        _bad(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "body too large")

    # 2. Verify timestamp first (cheap), then HMAC.
    ts_str = request.headers.get("X-Duitku-Timestamp", "")
    if not ts_str:
        _bad(status.HTTP_400_BAD_REQUEST, "X-Duitku-Timestamp header missing")
    try:
        ts_unix = int(ts_str)
    except ValueError:
        _bad(status.HTTP_400_BAD_REQUEST, "X-Duitku-Timestamp not an int")

    drift = abs(time.time() - ts_unix)
    if drift > cfg.max_replay_skew_seconds:
        _bad(
            status.HTTP_401_UNAUTHORIZED,
            f"timestamp drift {drift:.0f}s exceeds {cfg.max_replay_skew_seconds}s",
        )

    sig_hdr = request.headers.get("X-Duitku-Signature", "")
    if not sig_hdr:
        _bad(status.HTTP_401_UNAUTHORIZED, "X-Duitku-Signature header missing")
    expected = compute_hmac(cfg.hmac_key, raw, ts_str)
    provided = sig_hdr.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        _bad(status.HTTP_401_UNAUTHORIZED, "bad signature")

    # 3. HMAC passed. Parse the multipart body.
    form = await request.form()

    bank = str(form.get("bank", "")).lower()
    if not _BANK_NAME_RE.match(bank):
        _bad(status.HTTP_400_BAD_REQUEST, f"bad bank name {bank!r}")
    if bank not in ALLOWED_BANKS:
        _bad(status.HTTP_400_BAD_REQUEST, f"unknown bank {bank!r}")

    received_at_str = str(form.get("received_at", ""))
    try:
        # Python's fromisoformat in 3.11+ parses RFC3339 including 'Z'.
        received_at = datetime.fromisoformat(received_at_str.replace("Z", "+00:00"))
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        received_at = received_at.astimezone(timezone.utc)
    except ValueError:
        # Fall back to the verified header timestamp.
        received_at = datetime.fromtimestamp(ts_unix, tz=timezone.utc)

    message_id = str(form.get("message_id", ""))
    sender = str(form.get("sender", ""))
    declared_name = str(form.get("attachment_filename", ""))

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        _bad(status.HTTP_400_BAD_REQUEST, "file form field missing")

    # 4. Read attachment into memory once so we can hash it. The form
    #    parser respects MAX_FILE_SIZE on UploadFile via Starlette's
    #    spooled tempfile, but we still cap explicitly.
    buf = await upload.read(cfg.max_attachment_size + 1)
    if len(buf) > cfg.max_attachment_size:
        _bad(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"attachment exceeds limit {cfg.max_attachment_size}",
        )

    # Extension: prefer the declared name (original on-disk filename
    # at the sender), else the upload-part filename.
    ext = Path(declared_name).suffix.lower() if declared_name else ""
    if not ext and upload.filename:
        ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        _bad(status.HTTP_400_BAD_REQUEST, f"disallowed extension {ext!r}")

    digest = hashlib.sha256(buf).hexdigest()

    # 5. Final on-disk path: /landing/{bank}/inbox/{UTC-ts}-{digest[:16]}{ext}
    inbox_dir = cfg.landing_dir / bank / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{received_at.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}{ext}"
    final_path = inbox_dir / filename

    # Idempotency: same bytes -> same name -> skip the write.
    if final_path.exists():
        log.info(
            "attachment already present, skip write",
            extra={"bank": bank, "sha256": digest, "path": str(final_path)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 6. Tempfile + rename for crash safety.
    fd, tmp_path_str = tempfile.mkstemp(prefix=".tmp-", dir=str(inbox_dir))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, final_path)
    except OSError:
        # Best-effort cleanup; re-raise so FastAPI returns 500.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        log.exception("write attachment failed", extra={"bank": bank, "sha256": digest})
        raise

    # 7. Sidecar metadata. Non-fatal if it fails; the attachment is saved.
    meta = {
        "bank": bank,
        "received_at": received_at.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "message_id": message_id,
        "sender": sender,
        "attachment_filename": declared_name,
        "sha256": digest,
        "bytes": len(buf),
    }
    meta_path = final_path.with_suffix(final_path.suffix + ".meta.json")
    try:
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(meta_path, 0o644)
    except OSError as exc:
        log.warning("write sidecar failed", extra={"path": str(meta_path), "err": str(exc)})

    log.info(
        "accepted attachment",
        extra={
            "bank": bank,
            "sha256": digest,
            "bytes": len(buf),
            "path": str(final_path),
            "message_id": message_id,
            "sender_domain": _sender_domain(sender),
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _bad(code: int, msg: str) -> None:
    """Raise HTTPException + log at WARN. Never returns."""
    log.warning("webhook error: %s", msg, extra={"code": code})
    raise HTTPException(status_code=code, detail=msg)
