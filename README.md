# duitku

> Malaysian bank statement parser + transaction normaliser + Firefly III importer.
> Status: **scaffold (phase 0)** — the public API surface is taking shape; bank parsers are stubs that will land progressively.

`duitku` (*duit* + *-ku* = "my money") eats PDF and CSV statements from
Malaysian retail banks and emits canonical transactions to your
personal-finance tool of choice. Designed to be the missing piece that
the Malaysian self-hosting community keeps reinventing.

See [`docs/duitku.md`](https://github.com/ian-cq/docs/blob/main/duitku.md)
for the design doc — architecture, dedup model, deployment shape.

## What works today

- Webhook receiver (`duitku serve`) — accepts HMAC-signed multipart
  POSTs from a Cloudflare Email Worker and writes attachments to a
  landing directory.
- Sweep / prune orchestration scaffolding (`duitku sweep`, `duitku prune`).
- Config loaders: TOML password list, YAML accounts map.
- Sqlite-backed dedup + file-state store.

## What doesn't work yet

- Bank parsers (Maybank / UOB / Ryt) — interface defined, no
  extraction implemented. The pipeline is wired end-to-end but produces
  zero transactions today.
- Firefly III emitter — skeleton with the boot version check; the POST
  loop is not wired in.
- CLI subcommands `parse` and `import` — stubs that print "not yet
  implemented".

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

## License

Apache 2.0 — see [`LICENSE`](./LICENSE).
