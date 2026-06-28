"""Sweep loop: walk the landing PVC, parse, normalise, push to Firefly.

Filesystem state machine (per the design doc §5):

    /landing/<bank>/inbox/<file>          ← webhook drops files here
    /landing/<bank>/processed/<file>      ← sweep moves on success
    /landing/<bank>/failed/<file>         ← sweep moves on hard failure
    /landing/<bank>/failed/<file>.err     ← sidecar with stack + summary

A run does:

1. Load configs (``passwords.toml``, ``accounts.yaml``, env).
2. Probe Firefly so the run aborts before touching files if the API
   is down.
3. For each ``<bank>/inbox/<file>``:
   a. Parse via :mod:`duitku.parsers`.
   b. Reconcile (opening + sum(txns) == closing, ±1c).
   c. Normalise to canonical Transactions.
   d. Emit to Firefly with ``error_if_duplicate_hash=true``.
   e. Move to ``processed/`` on success, ``failed/`` on hard failure.

Hard failure = parse threw, reconciliation failed, or Firefly
returned a non-duplicate error. Duplicates are not a failure — the
whole point of ``external_id`` is to make re-runs safe.

The sweep is intentionally synchronous + single-threaded. We process
a handful of statements per month; concurrency would only add
failure modes.
"""

from __future__ import annotations

import logging
import shutil
import tomllib
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from duitku import parsers
from duitku.emitters.firefly import (
    EmitResult,
    FireflyClient,
    FireflyConfig,
    FireflyError,
    emit,
)
from duitku.normalise import normalise, reconcile

log = logging.getLogger("duitku.sweep")


# ---- config loading -------------------------------------------------


def load_passwords(path: Path) -> list[str]:
    """Load ``passwords.toml``. Missing file = empty list (unencrypted)."""
    if not path.exists():
        log.warning("no passwords file at %s, trying empty password only", path)
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    raw = data.get("passwords", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: top-level 'passwords' must be a list")
    return [str(p) for p in raw]


def load_accounts(path: Path) -> dict[str, dict[str, str]]:
    """Load ``accounts.yaml``. Missing file is a hard error.

    Returns a dict shaped ``{bank: {account_id: firefly_account_name}}``.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"accounts.yaml missing at {path}; the sweep cannot route "
            f"transactions to Firefly without it"
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: dict[str, dict[str, str]] = {}
    for bank, sub in data.items():
        if sub is None:
            out[bank] = {}
            continue
        if not isinstance(sub, dict):
            raise ValueError(
                f"accounts.yaml: bank {bank!r} must map to a dict, got {type(sub).__name__}"
            )
        out[bank] = {str(k): str(v) for k, v in sub.items()}
    return out


# ---- per-statement result ------------------------------------------


@dataclass
class StatementResult:
    bank: str
    path: Path
    outcome: str  # "ok" | "failed"
    emit: EmitResult | None = None
    error: str | None = None


# ---- sweep ----------------------------------------------------------


def discover(landing_dir: Path, *, only_bank: str | None = None) -> Iterable[tuple[str, Path]]:
    """Yield ``(bank, file_path)`` for every file under each bank's inbox.

    Hidden files (``.``-prefixed) and the webhook's atomic-rename
    ``.tmp`` files are skipped.
    """
    if not landing_dir.is_dir():
        return
    for bank_dir in sorted(landing_dir.iterdir()):
        if not bank_dir.is_dir():
            continue
        if only_bank and bank_dir.name != only_bank:
            continue
        inbox = bank_dir / "inbox"
        if not inbox.is_dir():
            continue
        for f in sorted(inbox.iterdir()):
            if not f.is_file():
                continue
            if f.name.startswith(".") or f.name.endswith(".tmp"):
                continue
            yield bank_dir.name, f


def process_one(
    bank: str,
    path: Path,
    *,
    passwords: list[str],
    accounts: dict[str, dict[str, str]],
    client: FireflyClient,
) -> StatementResult:
    """Parse, reconcile, normalise, emit. Pure-ish: caller does the move."""
    try:
        statement = parsers.parse(bank, path, passwords=passwords)
    except KeyError as exc:
        return StatementResult(bank=bank, path=path, outcome="failed", error=str(exc))
    except Exception as exc:  # parser-level failure - move to failed/
        return StatementResult(
            bank=bank,
            path=path,
            outcome="failed",
            error=f"parse failed: {exc}\n{traceback.format_exc()}",
        )

    if not statement.transactions:
        return StatementResult(
            bank=bank,
            path=path,
            outcome="failed",
            error="parser returned zero transactions",
        )

    if not reconcile(statement):
        return StatementResult(
            bank=bank,
            path=path,
            outcome="failed",
            error=(
                f"reconciliation failed for {bank} account {statement.account_id}; "
                f"opening={statement.opening_balance} closing={statement.closing_balance}"
            ),
        )

    txns = normalise(statement)
    try:
        result = emit(txns, client=client, account_map=accounts)
    except FireflyError as exc:
        return StatementResult(
            bank=bank,
            path=path,
            outcome="failed",
            error=f"firefly emit aborted: {exc}",
        )

    if result.errors > 0:
        return StatementResult(
            bank=bank,
            path=path,
            outcome="failed",
            emit=result,
            error=f"{result.errors} transactions failed to insert; see logs",
        )

    return StatementResult(bank=bank, path=path, outcome="ok", emit=result)


def _move(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        # Don't clobber - append a suffix. Re-runs are rare; manual
        # cleanup is fine.
        i = 1
        while True:
            candidate = dest_dir / f"{path.stem}.{i}{path.suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    shutil.move(str(path), str(dest))
    return dest


def _write_err_sidecar(failed_path: Path, message: str) -> None:
    sidecar = failed_path.with_suffix(failed_path.suffix + ".err")
    try:
        sidecar.write_text(message, encoding="utf-8")
    except OSError:
        log.exception("could not write .err sidecar", extra={"path": str(sidecar)})


def run_sweep(
    landing_dir: Path,
    *,
    passwords_file: Path,
    accounts_file: Path,
    only_bank: str | None = None,
    dry_run: bool = False,
) -> list[StatementResult]:
    """Top-level entrypoint. Returns one :class:`StatementResult` per file."""
    passwords = load_passwords(passwords_file)
    accounts = load_accounts(accounts_file)

    cfg = FireflyConfig.from_env()
    client = FireflyClient(cfg)
    version = client.probe()
    log.info("firefly reachable", extra={"version": version, "base_url": cfg.base_url})

    results: list[StatementResult] = []
    for bank, path in discover(landing_dir, only_bank=only_bank):
        log.info(
            "processing statement",
            extra={"bank": bank, "file": path.name, "dry_run": dry_run},
        )
        if dry_run:
            results.append(StatementResult(bank=bank, path=path, outcome="ok"))
            continue

        result = process_one(
            bank, path, passwords=passwords, accounts=accounts, client=client
        )
        bank_dir = landing_dir / bank
        if result.outcome == "ok":
            moved = _move(path, bank_dir / "processed")
            log.info(
                "statement processed",
                extra={
                    "bank": bank,
                    "file": path.name,
                    "moved_to": str(moved),
                    **(result.emit.summary() if result.emit else {}),
                },
            )
        else:
            moved = _move(path, bank_dir / "failed")
            _write_err_sidecar(moved, result.error or "")
            log.error(
                "statement failed",
                extra={
                    "bank": bank,
                    "file": path.name,
                    "moved_to": str(moved),
                    "error": result.error,
                },
            )
        results.append(result)

    return results


__all__ = [
    "StatementResult",
    "discover",
    "load_accounts",
    "load_passwords",
    "process_one",
    "run_sweep",
]
