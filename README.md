# duitku

> Malaysian bank statement parser + transaction normaliser + Firefly III importer.
> Status: **scaffold (phase 0)** — webhook works; bank parsers are
> stubs that will land progressively.

`duitku` (*duit* + *-ku* = "my money") eats PDF and CSV statements from
Malaysian retail banks and emits canonical transactions to your
personal-finance tool of choice. Designed to be the missing piece that
the Malaysian self-hosting community keeps reinventing.

See [`docs/duitku.md`](https://github.com/ian-cq/docs/blob/main/duitku.md)
for the design doc — architecture, dedup model, deployment shape.

## What works today

- Webhook receiver (`duitku serve`) — accepts HMAC-signed multipart
  POSTs from a Cloudflare Email Worker and writes attachments to a
  landing directory. FastAPI + uvicorn under the hood.
- Sweep / prune CLI subcommands (`duitku sweep`, `duitku prune`).
- Config loaders: TOML password list, YAML accounts map.

## What doesn't work yet

- Bank parsers (Maybank / UOB / Ryt) — not implemented. Phase 1.
- Firefly III emitter — phase 2.
- CLI subcommands `parse` and `import` — stubs that exit non-zero.

This shape is deliberate: stand the infrastructure up first, prove the
end-to-end transport works, then land parsers one bank at a time
against real (redacted) statements in `testdata/`.

## Subcommands

```
duitku serve   # HTTP webhook receiver for Cloudflare Email Worker payloads
duitku sweep   # parse files in landing/<bank>/inbox/ and import them
duitku prune   # delete processed files older than --retention-days
duitku parse   # (todo) one-shot CLI: parse a local file, emit canonical JSON
duitku import  # (todo) one-shot CLI: parse + post to a target emitter
```

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

When the phase-1 parsers land, install with the `parsers` extra:

```sh
.venv/bin/pip install -e '.[dev,parsers]'   # pulls pdfplumber + pikepdf
```

## Run the webhook locally

```sh
echo -n 'super-secret-test-key-32-chars' > /tmp/duitku-hmac.key
mkdir -p /tmp/duitku-landing
.venv/bin/duitku serve \
    --addr 127.0.0.1:8080 \
    --landing-dir /tmp/duitku-landing \
    --hmac-key-file /tmp/duitku-hmac.key
```

## License

Apache 2.0 — see [`LICENSE`](./LICENSE).
