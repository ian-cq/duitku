// Cloudflare Email Worker for duitku.
//
// Receives every email routed to insert@quanianitis.com, extracts PDF
// and CSV attachments, classifies the bank, and POSTs each attachment
// to the in-cluster duitku webhook over HTTPS with an X-API-Key
// header. No state, no retries beyond what the runtime gives us; the
// receiver's hash-based filename provides idempotency.
//
// See ~/Documents/docs/duitku.md §4.0 for the design rationale.

import PostalMime from "postal-mime";

export interface Env {
  // Public config (from wrangler.toml [vars]).
  WEBHOOK_URL: string;
  SENDER_ALLOWLIST: string;
  MAX_ATTACHMENT_BYTES: string;
  // Secret (set with `wrangler secret put GATEWAY_API_KEY`).
  GATEWAY_API_KEY: string;
}

// Bank classification: header > subject > sender domain. No fallback;
// unclassifiable messages are dropped with a log line.
const BANK_RULES: ReadonlyArray<{
  bank: string;
  subjectRe: RegExp;
  domainRe: RegExp;
}> = [
  {
    bank: "maybank",
    subjectRe: /\bmaybank\b/i,
    domainRe: /(^|\.)maybank(2u)?\.com\.my$/i,
  },
  {
    bank: "uob",
    subjectRe: /\buob\b/i,
    domainRe: /(^|\.)uob\.com\.(my|sg)$/i,
  },
  {
    bank: "ryt",
    subjectRe: /\bryt\s*bank\b/i,
    domainRe: /(^|\.)(rytbank|ryt)\.com\.my$/i,
  },
];

// Receiver-side allowlist; mirror its ALLOWED_EXT for fast-fail.
const ALLOWED_EXT = new Set([".pdf", ".csv", ".xls", ".xlsx"]);

export default {
  async email(message: ForwardableEmailMessage, env: Env, _ctx: ExecutionContext): Promise<void> {
    const log = (level: string, msg: string, extra: Record<string, unknown> = {}): void => {
      // JSON lines so wrangler tail + Logpush both parse cleanly.
      console.log(JSON.stringify({ level, msg, ...extra }));
    };

    // 1. SPF + DKIM check. Cloudflare runs these before invoking us
    //    and writes the result into Authentication-Results.
    const authResults = message.headers.get("Authentication-Results") ?? "";
    if (!/spf=pass/i.test(authResults) || !/dkim=pass/i.test(authResults)) {
      log("warn", "auth check failed", { from: message.from, authResults });
      // Reject so Cloudflare drops the message; no bounce, no forward.
      message.setReject("SPF or DKIM did not pass");
      return;
    }

    // 2. Sender allowlist (defence in depth on top of CF address rules).
    const allow = parseAllowlist(env.SENDER_ALLOWLIST);
    if (!senderAllowed(message.from, allow)) {
      log("warn", "sender not in allowlist", { from: message.from });
      message.setReject("Sender not allowed");
      return;
    }

    // 3. Parse MIME, extract attachments.
    const raw = await streamToUint8(message.raw);
    const parsed = await PostalMime.parse(raw);

    const messageId = parsed.messageId ?? message.headers.get("Message-ID") ?? "";
    const subject = parsed.subject ?? "";
    const fromAddr = parsed.from?.address ?? message.from;
    const fromDomain = domainOf(fromAddr);

    // 4. Bank classification.
    const headerBank = (message.headers.get("X-Duitku-Bank") ?? "").trim().toLowerCase();
    const bank =
      (headerBank && /^[a-z0-9_-]{2,32}$/.test(headerBank) ? headerBank : null) ??
      classifyBank(subject, fromDomain);
    if (!bank) {
      log("warn", "unclassifiable", { from: fromAddr, subject });
      // Don't reject - Cloudflare quarantine catches it on the dashboard
      // side; rejecting here would bounce a legitimate user email.
      return;
    }

    // 5. Filter + POST attachments.
    const maxBytes = Number(env.MAX_ATTACHMENT_BYTES) || 25 * 1024 * 1024;
    const receivedAt = new Date().toISOString();

    const attachments = (parsed.attachments ?? []).filter((a) => {
      const ext = extOf(a.filename ?? "");
      return ALLOWED_EXT.has(ext);
    });

    if (attachments.length === 0) {
      log("info", "no qualifying attachments", { bank, from: fromAddr, subject });
      return;
    }

    let posted = 0;
    let failed = 0;
    for (const att of attachments) {
      const bytes = toUint8(att.content);
      if (bytes.byteLength > maxBytes) {
        log("warn", "attachment too large, skip", {
          bank,
          filename: att.filename,
          bytes: bytes.byteLength,
        });
        failed += 1;
        continue;
      }
      try {
        await postAttachment(env, {
          bank,
          receivedAt,
          messageId,
          sender: fromAddr,
          filename: att.filename ?? "attachment.bin",
          mimeType: att.mimeType ?? "application/octet-stream",
          bytes,
        });
        posted += 1;
      } catch (err) {
        failed += 1;
        log("error", "post failed", {
          bank,
          filename: att.filename,
          err: err instanceof Error ? err.message : String(err),
        });
      }
    }

    log("info", "email processed", { bank, from: fromAddr, posted, failed });

    if (failed > 0 && posted === 0) {
      // Every attempt failed; let the runtime mark this invocation as
      // an error so it shows up in the dashboard's failed-deliveries
      // list. Cloudflare does not retry email workers automatically,
      // but the visibility matters.
      throw new Error(`all ${failed} attachment POST(s) failed`);
    }
  },
} satisfies ExportedHandler<Env>;

// ---- helpers --------------------------------------------------------

function parseAllowlist(s: string): Set<string> {
  return new Set(
    s
      .split(",")
      .map((x) => x.trim().toLowerCase())
      .filter(Boolean),
  );
}

function senderAllowed(from: string, allow: Set<string>): boolean {
  const lower = from.trim().toLowerCase();
  if (allow.has(lower)) return true;
  const dom = domainOf(lower);
  if (!dom) return false;
  // Suffix match on domain entries (no leading '@' or '.').
  for (const entry of allow) {
    if (entry.includes("@")) continue;
    if (dom === entry || dom.endsWith("." + entry)) return true;
  }
  return false;
}

function domainOf(addr: string): string {
  const at = addr.lastIndexOf("@");
  return at === -1 ? "" : addr.slice(at + 1).toLowerCase();
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i === -1 ? "" : name.slice(i).toLowerCase();
}

function classifyBank(subject: string, fromDomain: string): string | null {
  for (const r of BANK_RULES) {
    if (r.domainRe.test(fromDomain)) return r.bank;
  }
  for (const r of BANK_RULES) {
    if (r.subjectRe.test(subject)) return r.bank;
  }
  return null;
}

async function streamToUint8(stream: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  const reader = stream.getReader();
  let total = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      total += value.byteLength;
    }
  }
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}

function toUint8(content: ArrayBuffer | Uint8Array | string): Uint8Array {
  if (content instanceof Uint8Array) return content;
  if (content instanceof ArrayBuffer) return new Uint8Array(content);
  // postal-mime returns string for text parts; encode as UTF-8.
  return new TextEncoder().encode(content);
}

interface PostArgs {
  bank: string;
  receivedAt: string;
  messageId: string;
  sender: string;
  filename: string;
  mimeType: string;
  bytes: Uint8Array;
}

async function postAttachment(env: Env, a: PostArgs): Promise<void> {
  const form = new FormData();
  form.append("bank", a.bank);
  form.append("received_at", a.receivedAt);
  form.append("message_id", a.messageId);
  form.append("sender", a.sender);
  form.append("attachment_filename", a.filename);
  // Workers FormData accepts Blob. Construct from the Uint8Array
  // directly - workers-types accepts ArrayBufferView in the Blob ctor.
  const blob = new Blob([a.bytes], { type: a.mimeType });
  form.append("file", blob, a.filename);

  const resp = await fetch(env.WEBHOOK_URL, {
    method: "POST",
    headers: {
      "X-API-Key": env.GATEWAY_API_KEY,
    },
    body: form,
  });

  if (resp.status === 204) return;
  // Read a small slice of the body for the error log; ignore failures.
  let body = "";
  try {
    body = (await resp.text()).slice(0, 256);
  } catch {
    /* ignore */
  }
  throw new Error(`webhook returned ${resp.status}: ${body}`);
}
