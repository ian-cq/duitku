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
) -> None:
    """Scan the landing directory and push new statements to Firefly III.

    Stub: phase-1 work will wire parsers + dedup + Firefly emitter.
    The ``--passwords-file`` and ``--accounts-file`` flags are accepted
    now so the deployment manifest stays stable.
    """
    _setup_logging()
    log = logging.getLogger("duitku.sweep")
    log.info(
        "sweep stub: no parsers wired yet (landing=%s passwords=%s accounts=%s)",
        landing_dir,
        passwords_file,
        accounts_file,
    )


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
) -> None:
    """Parse a statement and emit canonical Transaction JSON to stdout.

    Stub: phase-1 wires the per-bank parsers.
    """
    _setup_logging()
    log = logging.getLogger("duitku.parse")
    log.warning(
        "parse stub: no parser wired for %s yet (%d file(s))",
        bank,
        len(paths),
    )
    raise typer.Exit(code=1)


@app.command(name="import")
def import_cmd(
    paths: list[Path] = typer.Argument(..., help="canonical Transaction JSON files"),
) -> None:
    """Push canonical Transaction JSON into Firefly III.

    Stub: phase-2 wires the Firefly emitter.
    """
    _setup_logging()
    log = logging.getLogger("duitku.import")
    log.warning("import stub: no Firefly emitter wired yet (%d file(s))", len(paths))
    raise typer.Exit(code=1)
