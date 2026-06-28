"""``duitku`` CLI.

Mirrors the Go-era cobra layout: ``serve``, ``sweep``, ``prune``,
``parse``, ``import``. Phase-1 sweep/parse/import are stubs until the
bank parsers land; ``serve`` is fully wired and is what runs in cluster.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="duitku",
    help="Malaysian bank-statement parser and Firefly III importer.",
    no_args_is_help=True,
    add_completion=False,
)


def _setup_logging() -> None:
    level = os.environ.get("DUITKU_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        stream=sys.stderr,
    )


# ---- serve -----------------------------------------------------------


@app.command()
def serve(
    addr: str = typer.Option(
        os.environ.get("DUITKU_ADDR", "0.0.0.0:8080"),
        "--addr",
        help="address to listen on, host:port",
    ),
    landing_dir: Path = typer.Option(
        Path(os.environ.get("DUITKU_LANDING_DIR", "/landing")),
        "--landing-dir",
        help="landing PVC mount path",
    ),
    max_attachment_size: int = typer.Option(
        int(os.environ.get("DUITKU_MAX_ATTACH_SIZE", str(25 * 1024 * 1024))),
        "--max-attachment-size",
        help="reject attachments larger than this (bytes)",
    ),
) -> None:
    """Run the HTTP webhook receiver.

    Authn is enforced at the gateway by Envoy Gateway's ``apiKeyAuth``
    filter (``X-API-Key`` header). The pod itself does not re-verify
    the key; it relies on the gateway to drop unauthenticated traffic
    upstream. The pod still validates bank name, file extension, and
    attachment size, and writes the file atomically to the landing
    directory. Nothing here parses or talks to Firefly — that is
    sweep's job.
    """
    _setup_logging()

    # Imported lazily so `duitku --help` doesn't pay the FastAPI import cost.
    import uvicorn

    from duitku.webhook import build_app, load_config

    cfg = load_config(
        landing_dir=landing_dir,
        max_attachment_size=max_attachment_size,
    )
    fastapi_app = build_app(cfg)

    host, _, port_s = addr.rpartition(":")
    if not host:
        host = "0.0.0.0"
    port = int(port_s)

    logging.getLogger("duitku").info(
        "webhook listening", extra={"addr": addr, "landing_dir": str(landing_dir)}
    )
    uvicorn.run(
        fastapi_app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        timeout_keep_alive=30,
    )


# ---- sweep / prune / parse / import (stubs) --------------------------


@app.command()
def sweep(
    landing_dir: Path = typer.Option(
        Path(os.environ.get("DUITKU_LANDING_DIR", "/landing")),
        "--landing-dir",
    ),
    passwords_file: Path = typer.Option(
        Path(os.environ.get("DUITKU_PASSWORDS_FILE", "/etc/duitku/passwords.toml")),
        "--passwords-file",
        help="TOML file with PDF passwords to try in order",
    ),
    accounts_file: Path = typer.Option(
        Path(os.environ.get("DUITKU_ACCOUNTS_FILE", "/etc/duitku/accounts.yaml")),
        "--accounts-file",
        help="YAML map from bank-account fingerprint to Firefly account id",
    ),
    only_bank: str = typer.Option(
        "", "--bank", help="restrict sweep to one bank slug (default: all)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="discover files but don't parse/emit/move"
    ),
) -> None:
    """Scan the landing directory and push new statements to Firefly III.

    Files move from ``<landing>/<bank>/inbox/`` to ``processed/`` on
    success or ``failed/<file>.err`` on hard failure. Re-runs are
    safe — Firefly's ``error_if_duplicate_hash=true`` plus our
    ``external_id`` make duplicates a no-op success.
    """
    _setup_logging()
    log = logging.getLogger("duitku.sweep")

    from duitku.sweep import run_sweep

    try:
        results = run_sweep(
            landing_dir,
            passwords_file=passwords_file,
            accounts_file=accounts_file,
            only_bank=only_bank or None,
            dry_run=dry_run,
        )
    except Exception:
        log.exception("sweep aborted before processing")
        raise typer.Exit(code=2)

    ok = sum(1 for r in results if r.outcome == "ok")
    failed = sum(1 for r in results if r.outcome == "failed")
    log.info(
        "sweep complete",
        extra={"processed": ok, "failed": failed, "total": len(results)},
    )
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def prune(
    landing_dir: Path = typer.Option(
        Path(os.environ.get("DUITKU_LANDING_DIR", "/landing")),
        "--landing-dir",
    ),
    retention_days: int = typer.Option(
        int(os.environ.get("DUITKU_RETENTION_DAYS", "30")),
        "--retention-days",
        help="delete files older than this many days from inbox/done/",
    ),
) -> None:
    """Delete attachments older than ``--retention-days`` from inbox+done."""
    _setup_logging()
    log = logging.getLogger("duitku.prune")

    import time as _time

    cutoff = _time.time() - retention_days * 86400
    removed = 0
    if not landing_dir.exists():
        log.info("nothing to prune; landing dir absent", extra={"landing_dir": str(landing_dir)})
        return
    for bank_dir in landing_dir.iterdir():
        if not bank_dir.is_dir():
            continue
        for sub in ("inbox", "done"):
            d = bank_dir / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    log.exception("prune unlink failed", extra={"path": str(f)})
    log.info("prune complete", extra={"removed": removed, "retention_days": retention_days})


@app.command(name="parse")
def parse_cmd(
    bank: str = typer.Option(..., "--bank", help="bank slug, e.g. maybank"),
    paths: list[Path] = typer.Argument(..., help="statement files"),
    passwords_file: Path = typer.Option(
        Path(os.environ.get("DUITKU_PASSWORDS_FILE", "/etc/duitku/passwords.toml")),
        "--passwords-file",
        help="TOML file with PDF passwords to try in order",
    ),
) -> None:
    """Parse statements and emit canonical Transaction JSON to stdout.

    Useful for offline debugging / fixture inspection. Does not talk to
    Firefly. One JSON document per file, newline-delimited (NDJSON).
    """
    _setup_logging()
    log = logging.getLogger("duitku.parse")

    import json as _json
    from dataclasses import asdict

    from duitku import parsers
    from duitku.normalise import normalise
    from duitku.sweep import load_passwords

    passwords = load_passwords(passwords_file)
    exit_code = 0
    for p in paths:
        try:
            statement = parsers.parse(bank, p, passwords=passwords)
        except Exception:
            log.exception("parse failed", extra={"file": str(p)})
            exit_code = 1
            continue
        txns = normalise(statement)
        doc = {
            "bank": statement.bank,
            "account_id": statement.account_id,
            "currency": statement.currency,
            "period_start": statement.period_start.isoformat() if statement.period_start else None,
            "period_end": statement.period_end.isoformat() if statement.period_end else None,
            "opening_balance": str(statement.opening_balance) if statement.opening_balance is not None else None,
            "closing_balance": str(statement.closing_balance) if statement.closing_balance is not None else None,
            "layout_version": statement.layout_version,
            "transactions": [
                {
                    **asdict(t),
                    "date": t.date.isoformat(),
                    "amount": str(t.amount),
                    "foreign_amount": str(t.foreign_amount) if t.foreign_amount is not None else None,
                }
                for t in txns
            ],
        }
        typer.echo(_json.dumps(doc, default=str))
    raise typer.Exit(code=exit_code)


@app.command(name="import")
def import_cmd(
    bank: str = typer.Option(..., "--bank", help="bank slug, e.g. maybank"),
    paths: list[Path] = typer.Argument(..., help="statement files (PDF)"),
    passwords_file: Path = typer.Option(
        Path(os.environ.get("DUITKU_PASSWORDS_FILE", "/etc/duitku/passwords.toml")),
        "--passwords-file",
    ),
    accounts_file: Path = typer.Option(
        Path(os.environ.get("DUITKU_ACCOUNTS_FILE", "/etc/duitku/accounts.yaml")),
        "--accounts-file",
    ),
) -> None:
    """One-shot: parse + reconcile + push to Firefly. No filesystem state machine.

    Sibling of ``sweep`` for manually re-importing a specific file
    without going through the inbox/processed/failed lifecycle. Files
    are not moved.
    """
    _setup_logging()
    log = logging.getLogger("duitku.import")

    from duitku import parsers
    from duitku.emitters.firefly import FireflyClient, FireflyConfig, emit
    from duitku.normalise import normalise, reconcile
    from duitku.sweep import load_accounts, load_passwords

    passwords = load_passwords(passwords_file)
    accounts = load_accounts(accounts_file)
    client = FireflyClient(FireflyConfig.from_env())
    client.probe()

    exit_code = 0
    for p in paths:
        try:
            statement = parsers.parse(bank, p, passwords=passwords)
        except Exception:
            log.exception("parse failed", extra={"file": str(p)})
            exit_code = 1
            continue
        if not reconcile(statement):
            log.error("reconciliation failed", extra={"file": str(p)})
            exit_code = 1
            continue
        txns = normalise(statement)
        result = emit(txns, client=client, account_map=accounts)
        log.info("import done", extra={"file": str(p), **result.summary()})
        if result.errors > 0:
            exit_code = 1
    raise typer.Exit(code=exit_code)
