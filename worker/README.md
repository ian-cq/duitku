# duitku-email-worker

Cloudflare Email Worker that forwards bank-statement attachments from
`insert@quanianitis.com` to the in-cluster `duitku-webhook`.

## What it does

1. Cloudflare Email Routing receives the email and runs SPF / DKIM /
   DMARC. Routes messages addressed to `insert@quanianitis.com` to
   this Worker.
2. Worker rejects anything where SPF or DKIM didn't `pass`, or where
   the sender isn't in `SENDER_ALLOWLIST`.
3. Worker parses MIME, keeps attachments with extension `.pdf`,
   `.csv`, `.xls`, or `.xlsx`.
4. Classifies the bank from (in order) `X-Duitku-Bank` header,
   subject, sender domain. Drops unclassifiable messages.
5. POSTs each attachment to the webhook with `X-API-Key:
   ${GATEWAY_API_KEY}`. The receiver hashes filenames so retries are
   idempotent.

No state, no queues. Re-deploying overwrites the running version.

## Deploy

```sh
# Once, to pull deps
npm install

# Set the API key (matches Secret/gateway-api-key key `cf-worker` in
# the duitku namespace - see
# ~/Documents/homelab/infra/charts/duitku/api-key-secret.yaml). The
# value lives in 1Password under 'duitku cf-worker'.
npx wrangler secret put GATEWAY_API_KEY

# Deploy
npx wrangler deploy
```

The Email Routing rule itself (`insert@quanianitis.com` -> this
Worker) lives in the Cloudflare dashboard under Email > Email
Routing > Routes. wrangler does not yet manage that resource.

## Verify

```sh
# Stream logs while you send a test email
npx wrangler tail

# Or trigger from the homelab directly (bypasses CF Email)
python /tmp/duitku-smoke.py
```

## Rotate the API key

1. Edit `infra/charts/duitku/api-key-secret.yaml` in the homelab repo,
   regenerate the value, commit + push.
2. Bridge-apply or wait for Argo to reconcile.
3. `npx wrangler secret put GATEWAY_API_KEY` with the new value.
4. Smoke test.

There is a brief window where the Worker presents the old key and the
Gateway expects the new one; CF Email Workers are *not* automatically
retried, so do this when no email is in flight (overnight is fine).
