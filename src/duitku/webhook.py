"""HTTP receiver invoked by the Cloudflare Email Worker.

The Worker POSTs each extracted attachment to ``/cf-inbox``. Authn is
handled at the gateway by Envoy Gateway's ``apiKeyAuth`` filter
(``X-API-Key`` against the in-namespace ``gateway-api-key`` Secret);
this module does not re-verify it. The pod relies on the gateway to
drop unauthenticated traffic before it arrives.

The receiver enforces a tight allowlist on bank names and file
extensions, then writes the attachment plus a JSON sidecar to the
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


# ---- config ----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Webhook handler configuration. Validated by :func:`build_app`."""

    landing_dir: Path
    max_attachment_size: int = DEFAULT_MAX_ATTACHMENT_SIZE


def load_config(
    *,
    landing_dir: str | os.PathLike[str],
    max_attachment_size: int = DEFAULT_MAX_ATTACHMENT_SIZE,
) -> Config:
    """Validate config at boot. A misconfiguration here fails fast
    instead of producing 500s on every request.
    """
    landing = Path(landing_dir)
    landing.mkdir(parents=True, exist_ok=True)

    return Config(
        landing_dir=landing,
        max_attachment_size=max_attachment_size,
    )


# ---- helpers ---------------------------------------------------------


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
    app = FastAPI(title="duitku-webhook", version="0.3.0", docs_url=None, redoc_url=None)

    @app.get("/healthz", response_class=Response)
    def healthz() -> Response:
        return Response(content="ok\n", media_type="text/plain; charset=utf-8")

    @app.post("/cf-inbox", status_code=status.HTTP_204_NO_CONTENT)
    async def cf_inbox(request: Request) -> Response:
        return await _handle_cf_inbox(request, cfg)

    return app


async def _handle_cf_inbox(request: Request, cfg: Config) -> Response:
    """Parse multipart, write attachment + sidecar atomically.

    Authn is the gateway's job (``X-API-Key``). The optional
    ``X-Client-Id`` header is injected by EG's
    ``forwardClientIDHeader`` and is logged for audit only.
    """

    client_id = request.headers.get("X-Client-Id", "")

    # 1. Parse the multipart body. Starlette's UploadFile spools large
    #    parts to /tmp via SpooledTemporaryFile, so the request body
    #    isn't fully in RAM.
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
        # Fall back to current UTC time if the form field is missing
        # or unparseable.
        received_at = datetime.now(tz=timezone.utc)

    message_id = str(form.get("message_id", ""))
    sender = str(form.get("sender", ""))
    declared_name = str(form.get("attachment_filename", ""))

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        _bad(status.HTTP_400_BAD_REQUEST, "file form field missing")

    # 2. Read attachment into memory once so we can hash it. The form
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

    # 3. Final on-disk path: /landing/{bank}/inbox/{UTC-ts}-{digest[:16]}{ext}
    inbox_dir = cfg.landing_dir / bank / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{received_at.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}{ext}"
    final_path = inbox_dir / filename

    # Idempotency: same bytes -> same name -> skip the write.
    if final_path.exists():
        log.info(
            "attachment already present, skip write",
            extra={
                "bank": bank,
                "sha256": digest,
                "path": str(final_path),
                "client_id": client_id,
            },
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 4. Tempfile + rename for crash safety.
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

    # 5. Sidecar metadata. Non-fatal if it fails; the attachment is saved.
    meta = {
        "bank": bank,
        "received_at": received_at.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "message_id": message_id,
        "sender": sender,
        "attachment_filename": declared_name,
        "sha256": digest,
        "bytes": len(buf),
        "client_id": client_id,
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
            "client_id": client_id,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _bad(code: int, msg: str) -> None:
    """Raise HTTPException + log at WARN. Never returns."""
    log.warning("webhook error: %s", msg, extra={"code": code})
    raise HTTPException(status_code=code, detail=msg)
