"""Firefly III emitter.

Pushes canonical :class:`Transaction` objects into Firefly via the v1
REST API. Uses ``external_id`` + ``error_if_duplicate_hash=true`` as
the dedup mechanism, so re-running the same statement is a no-op:
Firefly rejects the duplicate with HTTP 422 and the emitter treats
that as success-skip.

Configuration (env or constructor kwargs):

- ``FIREFLY_BASE_URL`` — e.g. ``https://finance.62a.quanianitis.com``
- ``FIREFLY_PAT`` — Personal Access Token, ``Authorization: Bearer …``

On startup the emitter probes ``GET /api/v1/about`` to fail fast on
bad URL / bad token / Firefly downtime; if the probe fails, the
caller (the sweep loop) should retry rather than start a partial
import.

The :func:`emit` call is intentionally one-transaction-at-a-time
rather than the bulk endpoint:

- The bulk endpoint short-circuits on first error which makes it
  hostile to "import 49/50 rows, log the one we couldn't".
- We don't push enough volume to make per-row HTTP cost matter.

Account name → Firefly account-id resolution happens in :func:`emit`
via the ``accounts`` map the operator maintains in ``accounts.yaml``;
the emitter consults Firefly's ``/api/v1/accounts`` to translate the
human-readable asset-account name to the numeric Firefly ID once per
statement, then caches it.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from duitku.models import Transaction

log = logging.getLogger("duitku.emitters.firefly")


class FireflyError(Exception):
    """Non-recoverable Firefly API error."""


class FireflyDuplicate(Exception):
    """Firefly rejected the transaction as a duplicate.

    Treated as success-skip by callers. Carries the duplicate hash /
    external_id for logging.
    """


@dataclass(frozen=True)
class FireflyConfig:
    base_url: str
    token: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "FireflyConfig":
        base = os.environ.get("FIREFLY_BASE_URL", "").rstrip("/")
        token = os.environ.get("FIREFLY_PAT", "")
        if not base:
            raise FireflyError("FIREFLY_BASE_URL not set")
        if not token:
            raise FireflyError("FIREFLY_PAT not set")
        return cls(base_url=base, token=token)


class FireflyClient:
    """Thin urllib-backed Firefly III v1 client.

    Avoids pulling httpx/requests into the runtime image. Firefly's
    API is small enough that hand-rolled is cheaper than dragging an
    HTTP library.
    """

    def __init__(self, cfg: FireflyConfig) -> None:
        self.cfg = cfg
        self._account_cache: dict[str, int] = {}

    # ---- low-level HTTP --------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
    ) -> tuple[int, dict]:
        url = self.cfg.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {
            "Authorization": f"Bearer {self.cfg.token}",
            "Accept": "application/vnd.api+json",
            "User-Agent": "duitku/0.4",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                payload = resp.read()
                if not payload:
                    return resp.status, {}
                return resp.status, json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                err_body = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                err_body = {"raw": raw.decode("utf-8", errors="replace")}
            return exc.code, err_body
        except urllib.error.URLError as exc:
            raise FireflyError(f"firefly network error: {exc.reason}") from exc

    # ---- public API ------------------------------------------------

    def probe(self) -> str:
        """Verify connectivity + auth. Returns the Firefly version string."""
        status, body = self._request("GET", "/api/v1/about")
        if status != 200:
            raise FireflyError(
                f"firefly /api/v1/about returned HTTP {status}: {body!r}"
            )
        try:
            return body["data"]["version"]
        except (KeyError, TypeError) as exc:
            raise FireflyError(f"firefly /api/v1/about malformed: {body!r}") from exc

    def resolve_account_id(self, name: str) -> int:
        """Return the Firefly numeric ID for asset-account *name*. Cached."""
        if name in self._account_cache:
            return self._account_cache[name]
        status, body = self._request(
            "GET",
            "/api/v1/accounts",
            query={"type": "asset", "limit": 100},
        )
        if status != 200:
            raise FireflyError(
                f"firefly /api/v1/accounts returned HTTP {status}: {body!r}"
            )
        for entry in body.get("data", []):
            attrs = entry.get("attributes", {})
            if attrs.get("name") == name:
                acct_id = int(entry["id"])
                self._account_cache[name] = acct_id
                return acct_id
        raise FireflyError(
            f"firefly asset account named {name!r} not found; "
            f"check accounts.yaml or create the account in Firefly"
        )

    def post_transaction(
        self,
        txn: Transaction,
        *,
        firefly_account_id: int,
    ) -> None:
        """POST one transaction. Raises :class:`FireflyDuplicate` on 422-duplicate."""
        body = _to_firefly_body(txn, firefly_account_id=firefly_account_id)
        status, resp = self._request(
            "POST",
            "/api/v1/transactions",
            body=body,
        )
        if status in (200, 201):
            return
        if status == 422 and _is_duplicate(resp):
            raise FireflyDuplicate(txn.external_id)
        raise FireflyError(
            f"firefly POST /api/v1/transactions returned HTTP {status}: {resp!r}"
        )


# ---- helpers --------------------------------------------------------


def _is_duplicate(body: dict) -> bool:
    """Detect Firefly's "Duplicate of #N" 422 response shape.

    Firefly responds with ``{"message": "...", "errors": {"transactions.0.description": ["..."]}}``
    when ``error_if_duplicate_hash=true`` catches a dup. We match on
    the literal "Duplicate" token because the field path varies by
    Firefly version.
    """
    raw = json.dumps(body).lower()
    return "duplicate" in raw


def _to_firefly_body(txn: Transaction, *, firefly_account_id: int) -> dict:
    """Translate :class:`Transaction` to Firefly's POST /transactions JSON.

    Direction:

    - ``withdrawal``: ``source_id`` = our account; Firefly auto-creates
      an expense account from the description.
    - ``deposit``: ``destination_id`` = our account; Firefly auto-creates
      a revenue account from the description.

    We do NOT pre-create source/destination accounts; Firefly creates
    them on demand. Users can later rename / merge them in the UI.

    ``external_id`` carries the dedup key. ``error_if_duplicate_hash``
    asks Firefly to refuse a second insert with the same content hash
    (Firefly's internal hash, not ours) which is belt-and-braces with
    our ``external_id``.
    """
    txn_obj: dict[str, object] = {
        "type": txn.kind,
        "date": txn.date.isoformat(),
        "amount": _format_amount(txn.amount),
        "currency_code": txn.currency,
        "description": txn.description,
        "external_id": txn.external_id,
        "notes": _render_notes(txn),
        "tags": _tags(txn),
    }
    if txn.kind == "withdrawal":
        txn_obj["source_id"] = str(firefly_account_id)
    else:
        txn_obj["destination_id"] = str(firefly_account_id)

    if txn.foreign_amount is not None and txn.foreign_currency:
        txn_obj["foreign_amount"] = _format_amount(txn.foreign_amount)
        txn_obj["foreign_currency_code"] = txn.foreign_currency

    return {
        "error_if_duplicate_hash": True,
        "apply_rules": True,
        "fire_webhooks": True,
        "transactions": [txn_obj],
    }


def _format_amount(amount: Decimal) -> str:
    """Firefly expects amounts as a positive decimal string."""
    return format(amount, "f")


def _tags(txn: Transaction) -> list[str]:
    tags = ["duitku", f"bank:{txn.bank}", f"account:{txn.account_id}"]
    if txn.reconciliation_skipped:
        tags.append("reconciliation:skipped")
    return tags


def _render_notes(txn: Transaction) -> str:
    """Render the audit notes dict as Firefly markdown."""
    lines = []
    for k in (
        "raw_description",
        "source_layout",
        "statement_period",
        "reconciliation",
    ):
        if k in txn.notes:
            lines.append(f"- **{k}**: {txn.notes[k]}")
    if txn.bank_reference:
        lines.append(f"- **bank_reference**: {txn.bank_reference}")
    return "\n".join(lines)


# ---- high-level emit-many ------------------------------------------


@dataclass
class EmitResult:
    inserted: int = 0
    duplicates: int = 0
    errors: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "errors": self.errors,
        }


def emit(
    txns: Iterable[Transaction],
    *,
    client: FireflyClient,
    account_map: dict[str, dict[str, str]],
) -> EmitResult:
    """Push *txns* to Firefly. Returns insert/duplicate/error counts.

    ``account_map`` is the parsed ``accounts.yaml`` body, shape::

        {"maybank": {"1234": "Maybank Savings ****1234"}, ...}

    Resolution failures (account_id missing from the map, or Firefly
    has no account with that name) bubble up as :class:`FireflyError`;
    transient HTTP errors bubble up too. The sweep loop decides what
    to do with failed statements (move to ``failed/``).
    """
    result = EmitResult()
    for txn in txns:
        firefly_account_id = _resolve(
            client, account_map, bank=txn.bank, account_id=txn.account_id
        )
        try:
            client.post_transaction(txn, firefly_account_id=firefly_account_id)
            result.inserted += 1
            log.info(
                "firefly insert ok",
                extra={
                    "bank": txn.bank,
                    "account_id": txn.account_id,
                    "external_id": txn.external_id,
                    "amount": str(txn.amount),
                },
            )
        except FireflyDuplicate:
            result.duplicates += 1
            log.info(
                "firefly duplicate skipped",
                extra={
                    "bank": txn.bank,
                    "external_id": txn.external_id,
                },
            )
        except FireflyError:
            result.errors += 1
            log.exception(
                "firefly insert failed",
                extra={
                    "bank": txn.bank,
                    "external_id": txn.external_id,
                },
            )
    return result


def _resolve(
    client: FireflyClient,
    account_map: dict[str, dict[str, str]],
    *,
    bank: str,
    account_id: str,
) -> int:
    try:
        name = account_map[bank][account_id]
    except KeyError as exc:
        raise FireflyError(
            f"accounts.yaml has no entry for bank={bank!r} account_id={account_id!r}; "
            f"add it before re-running"
        ) from exc
    return client.resolve_account_id(name)


__all__ = [
    "FireflyClient",
    "FireflyConfig",
    "FireflyDuplicate",
    "FireflyError",
    "EmitResult",
    "emit",
]
