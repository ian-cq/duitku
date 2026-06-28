// Package webhook implements the HTTP receiver invoked by the Cloudflare
// Email Worker after Email Routing delivers a statement to
// insert@<routed-domain>. The Worker POSTs each extracted attachment to
// /cf-inbox with an HMAC signature over the body; this handler verifies
// the signature, enforces a tight allowlist on bank names and file
// extensions, and writes the attachment plus a JSON sidecar to the
// landing directory.
//
// The receiver deliberately does NOT parse, normalise, or talk to
// Firefly. That separation is load-bearing: a parser bug must never be
// able to make the Worker retry (because retries cause duplicate
// inbox files, and even though we hash-name files, retry storms are
// noisy). The sweep CronJob picks up files from the landing directory
// asynchronously.
package webhook

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Config controls the webhook handler.
type Config struct {
	// LandingDir is the directory under which {bank}/inbox/ trees live.
	// Must exist and be writable.
	LandingDir string

	// HMACKeyFile is the path to a file whose contents are the shared
	// secret. Whitespace at the start/end is trimmed. Empty contents
	// are rejected at boot.
	HMACKeyFile string

	// MaxReplaySkew rejects requests whose X-Duitku-Timestamp drifts
	// from the receiver's clock by more than this amount. 5 minutes
	// is a comfortable default given Workers and the cluster are both
	// NTP-synced.
	MaxReplaySkew time.Duration

	// MaxAttachmentSize bounds a single attachment in bytes. 25 MiB
	// matches Gmail's outbound limit.
	MaxAttachmentSize int64
}

// allowedBanks is a strict whitelist; anything else is 400. Extend when
// a new parser lands.
var allowedBanks = map[string]struct{}{
	"maybank": {},
	"uob":     {},
	"ryt":     {},
}

// allowedExt is a strict whitelist of acceptable attachment extensions.
var allowedExt = map[string]struct{}{
	".pdf": {},
	".csv": {},
	".xls": {},
	".xlsx": {},
}

// bankNameRe is an extra paranoid filter on the bank form field.
var bankNameRe = regexp.MustCompile(`^[a-z0-9_-]{2,32}$`)

// Server is the HTTP handler.
type Server struct {
	cfg     Config
	hmacKey []byte
}

// New constructs a webhook server. It validates the HMAC key file at
// boot so a misconfiguration fails fast instead of producing 500s on
// every request.
func New(cfg Config) (*Server, error) {
	if cfg.LandingDir == "" {
		return nil, errors.New("LandingDir is required")
	}
	if cfg.HMACKeyFile == "" {
		return nil, errors.New("HMACKeyFile is required")
	}
	if cfg.MaxReplaySkew <= 0 {
		cfg.MaxReplaySkew = 5 * time.Minute
	}
	if cfg.MaxAttachmentSize <= 0 {
		cfg.MaxAttachmentSize = 25 << 20
	}

	keyBytes, err := os.ReadFile(cfg.HMACKeyFile)
	if err != nil {
		return nil, fmt.Errorf("read HMAC key file %q: %w", cfg.HMACKeyFile, err)
	}
	key := strings.TrimSpace(string(keyBytes))
	if len(key) < 16 {
		return nil, fmt.Errorf("HMAC key in %q is too short (%d chars); want >=16", cfg.HMACKeyFile, len(key))
	}

	if err := os.MkdirAll(cfg.LandingDir, 0o755); err != nil {
		return nil, fmt.Errorf("create landing dir %q: %w", cfg.LandingDir, err)
	}

	return &Server{
		cfg:     cfg,
		hmacKey: []byte(key),
	}, nil
}

// ServeHTTP routes requests to /healthz and /cf-inbox.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.URL.Path == "/healthz" && r.Method == http.MethodGet:
		s.healthz(w, r)
	case r.URL.Path == "/cf-inbox" && r.Method == http.MethodPost:
		s.cfInbox(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) healthz(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, "ok\n")
}

// cfInbox accepts a multipart POST from the Cloudflare Email Worker.
//
// Required headers:
//
//	X-Duitku-Signature: sha256=<hex>      HMAC-SHA256 of the raw body
//	X-Duitku-Timestamp: <unix-seconds>    used for replay protection
//
// Required form fields:
//
//	bank                 (text)
//	received_at          (text, RFC3339)
//	message_id           (text)
//	sender               (text)
//	attachment_filename  (text)
//	file                 (binary)
//
// Returns 204 on success, 4xx on validation failure, 5xx on storage failure.
func (s *Server) cfInbox(w http.ResponseWriter, r *http.Request) {
	// Cap the raw body so a malicious sender cannot fill memory.
	bodyLimit := s.cfg.MaxAttachmentSize + 1<<20 // attachment + small form overhead
	r.Body = http.MaxBytesReader(w, r.Body, bodyLimit)

	// Read full body up-front so we can HMAC over the exact bytes.
	// This is OK because bodyLimit caps it; for very large attachments
	// the Worker would chunk into multiple POSTs anyway.
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		httpErr(w, http.StatusRequestEntityTooLarge, "body too large or unreadable", err)
		return
	}

	// Verify timestamp first (cheap), then HMAC (also cheap but blocks log noise on stale replays).
	tsStr := r.Header.Get("X-Duitku-Timestamp")
	if tsStr == "" {
		httpErr(w, http.StatusBadRequest, "X-Duitku-Timestamp header missing", nil)
		return
	}
	tsUnix, err := strconv.ParseInt(tsStr, 10, 64)
	if err != nil {
		httpErr(w, http.StatusBadRequest, "X-Duitku-Timestamp not an int", err)
		return
	}
	ts := time.Unix(tsUnix, 0)
	if drift := time.Since(ts).Abs(); drift > s.cfg.MaxReplaySkew {
		httpErr(w, http.StatusUnauthorized, fmt.Sprintf("timestamp drift %s exceeds %s", drift, s.cfg.MaxReplaySkew), nil)
		return
	}

	sigHdr := r.Header.Get("X-Duitku-Signature")
	if sigHdr == "" {
		httpErr(w, http.StatusUnauthorized, "X-Duitku-Signature header missing", nil)
		return
	}
	expected, err := computeHMAC(s.hmacKey, raw, tsStr)
	if err != nil {
		httpErr(w, http.StatusInternalServerError, "hmac compute", err)
		return
	}
	if !hmac.Equal([]byte(strings.TrimPrefix(sigHdr, "sha256=")), []byte(expected)) {
		httpErr(w, http.StatusUnauthorized, "bad signature", nil)
		return
	}

	// HMAC passed. Now parse the multipart body.
	// We need to re-attach a Body for ParseMultipartForm.
	r.Body = io.NopCloser(newBytesReader(raw))
	if err := r.ParseMultipartForm(s.cfg.MaxAttachmentSize); err != nil {
		httpErr(w, http.StatusBadRequest, "parse multipart", err)
		return
	}

	bank := strings.ToLower(r.FormValue("bank"))
	if !bankNameRe.MatchString(bank) {
		httpErr(w, http.StatusBadRequest, fmt.Sprintf("bad bank name %q", bank), nil)
		return
	}
	if _, ok := allowedBanks[bank]; !ok {
		httpErr(w, http.StatusBadRequest, fmt.Sprintf("unknown bank %q", bank), nil)
		return
	}

	receivedAtStr := r.FormValue("received_at")
	receivedAt, err := time.Parse(time.RFC3339, receivedAtStr)
	if err != nil {
		// fall back to the verified header timestamp
		receivedAt = ts
	}
	messageID := r.FormValue("message_id")
	sender := r.FormValue("sender")
	declaredName := r.FormValue("attachment_filename")

	file, hdr, err := r.FormFile("file")
	if err != nil {
		httpErr(w, http.StatusBadRequest, "file form field missing", err)
		return
	}
	defer file.Close()

	if hdr.Size > s.cfg.MaxAttachmentSize {
		httpErr(w, http.StatusRequestEntityTooLarge, fmt.Sprintf("attachment %d > limit %d", hdr.Size, s.cfg.MaxAttachmentSize), nil)
		return
	}

	// Pick extension from declaredName if available (Worker sends the
	// original attachment filename), else from the upload header.
	ext := strings.ToLower(filepath.Ext(declaredName))
	if ext == "" {
		ext = strings.ToLower(filepath.Ext(hdr.Filename))
	}
	if _, ok := allowedExt[ext]; !ok {
		httpErr(w, http.StatusBadRequest, fmt.Sprintf("disallowed extension %q", ext), nil)
		return
	}

	// Read the attachment into memory once so we can hash it. Bounded by
	// MaxAttachmentSize via the form parser above.
	buf, err := io.ReadAll(io.LimitReader(file, s.cfg.MaxAttachmentSize+1))
	if err != nil {
		httpErr(w, http.StatusInternalServerError, "read attachment", err)
		return
	}
	if int64(len(buf)) > s.cfg.MaxAttachmentSize {
		httpErr(w, http.StatusRequestEntityTooLarge, "attachment exceeds limit after read", nil)
		return
	}

	sum := sha256.Sum256(buf)
	digest := hex.EncodeToString(sum[:])

	// Final on-disk path: /landing/{bank}/inbox/{UTC-timestamp}-{digest[:16]}{ext}
	inboxDir := filepath.Join(s.cfg.LandingDir, bank, "inbox")
	if err := os.MkdirAll(inboxDir, 0o755); err != nil {
		httpErr(w, http.StatusInternalServerError, "mkdir inbox", err)
		return
	}
	filename := fmt.Sprintf("%s-%s%s", receivedAt.UTC().Format("20060102T150405Z"), digest[:16], ext)
	finalPath := filepath.Join(inboxDir, filename)

	// Idempotency: if the file already exists with the same sha256 in
	// its name, skip the write entirely. Same bytes hash to same name.
	if _, err := os.Stat(finalPath); err == nil {
		slog.Info("attachment already present, skip write",
			"bank", bank, "sha256", digest, "path", finalPath)
		w.WriteHeader(http.StatusNoContent)
		return
	}

	// Write via a tempfile + rename for crash safety.
	tmp, err := os.CreateTemp(inboxDir, ".tmp-*")
	if err != nil {
		httpErr(w, http.StatusInternalServerError, "create tmp", err)
		return
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(buf); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpPath)
		httpErr(w, http.StatusInternalServerError, "write tmp", err)
		return
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpPath)
		httpErr(w, http.StatusInternalServerError, "close tmp", err)
		return
	}
	if err := os.Rename(tmpPath, finalPath); err != nil {
		_ = os.Remove(tmpPath)
		httpErr(w, http.StatusInternalServerError, "rename tmp -> final", err)
		return
	}

	// Sidecar metadata. Mirror naming so the sweep can find it.
	meta := map[string]any{
		"bank":                bank,
		"received_at":         receivedAt.UTC().Format(time.RFC3339),
		"message_id":          messageID,
		"sender":              sender,
		"attachment_filename": declaredName,
		"sha256":              digest,
		"bytes":               len(buf),
	}
	metaPath := finalPath + ".meta.json"
	metaBytes, _ := json.MarshalIndent(meta, "", "  ")
	if err := os.WriteFile(metaPath, metaBytes, 0o644); err != nil {
		// Non-fatal: the attachment is already saved. Log loudly.
		slog.Warn("write sidecar failed", "path", metaPath, "err", err)
	}

	slog.Info("accepted attachment",
		"bank", bank, "sha256", digest, "bytes", len(buf),
		"path", finalPath, "message_id", messageID, "sender_domain", senderDomain(sender))
	w.WriteHeader(http.StatusNoContent)
}

// computeHMAC returns hex(HMAC-SHA256(key, timestamp || "." || body)).
//
// Including the timestamp inside the MAC binds the body and the
// timestamp together so an attacker cannot replay a captured request
// with a fresh timestamp.
func computeHMAC(key, body []byte, ts string) (string, error) {
	h := hmac.New(sha256.New, key)
	if _, err := h.Write([]byte(ts)); err != nil {
		return "", err
	}
	if _, err := h.Write([]byte(".")); err != nil {
		return "", err
	}
	if _, err := h.Write(body); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// senderDomain returns the part after '@' lowercased, or "" if absent.
// Used for log fields; we deliberately do not log the local-part.
func senderDomain(sender string) string {
	at := strings.LastIndex(sender, "@")
	if at == -1 {
		return ""
	}
	return strings.ToLower(sender[at+1:])
}

// httpErr writes a plain-text 4xx/5xx response and logs the detail.
//
// We log err separately so we can include sensitive internals at debug
// level without leaking them to the client.
func httpErr(w http.ResponseWriter, code int, msg string, err error) {
	if err != nil {
		slog.Warn("webhook error", "code", code, "msg", msg, "err", err)
	} else {
		slog.Warn("webhook error", "code", code, "msg", msg)
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(code)
	_, _ = io.WriteString(w, msg+"\n")
}
