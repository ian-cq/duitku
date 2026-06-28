package webhook

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

// TestCFInbox_HappyPath exercises the full request path: timestamp +
// HMAC + multipart parse + file write + sidecar.
func TestCFInbox_HappyPath(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "key")
	if err := os.WriteFile(keyFile, []byte("super-secret-test-key-32-chars"), 0o600); err != nil {
		t.Fatal(err)
	}

	srv, err := New(Config{
		LandingDir:        filepath.Join(dir, "landing"),
		HMACKeyFile:       keyFile,
		MaxReplaySkew:     5 * time.Minute,
		MaxAttachmentSize: 1 << 20,
	})
	if err != nil {
		t.Fatal(err)
	}

	body, contentType := buildMultipart(t, map[string]string{
		"bank":                "maybank",
		"received_at":         time.Now().UTC().Format(time.RFC3339),
		"message_id":          "<test@example.com>",
		"sender":              "user@example.com",
		"attachment_filename": "statement.pdf",
	}, "file", "statement.pdf", []byte("%PDF-1.4\n...fake pdf body..."))

	ts := strconv.FormatInt(time.Now().Unix(), 10)
	sig := signTest(t, []byte("super-secret-test-key-32-chars"), body, ts)

	req := httptest.NewRequest(http.MethodPost, "/cf-inbox", bytes.NewReader(body))
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("X-Duitku-Timestamp", ts)
	req.Header.Set("X-Duitku-Signature", "sha256="+sig)

	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}

	inbox := filepath.Join(dir, "landing", "maybank", "inbox")
	entries, err := os.ReadDir(inbox)
	if err != nil {
		t.Fatalf("read inbox: %v", err)
	}
	var pdfs, metas int
	for _, e := range entries {
		switch filepath.Ext(e.Name()) {
		case ".pdf":
			pdfs++
		case ".json":
			metas++
		}
	}
	if pdfs != 1 || metas != 1 {
		t.Fatalf("expected 1 pdf + 1 meta, got %d / %d (entries=%v)", pdfs, metas, entries)
	}
}

// TestCFInbox_BadSignature ensures HMAC mismatches are 401.
func TestCFInbox_BadSignature(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "key")
	_ = os.WriteFile(keyFile, []byte("super-secret-test-key-32-chars"), 0o600)

	srv, err := New(Config{
		LandingDir:  filepath.Join(dir, "landing"),
		HMACKeyFile: keyFile,
	})
	if err != nil {
		t.Fatal(err)
	}

	body, contentType := buildMultipart(t, map[string]string{
		"bank":        "maybank",
		"received_at": time.Now().UTC().Format(time.RFC3339),
	}, "file", "x.pdf", []byte("body"))

	req := httptest.NewRequest(http.MethodPost, "/cf-inbox", bytes.NewReader(body))
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("X-Duitku-Timestamp", strconv.FormatInt(time.Now().Unix(), 10))
	req.Header.Set("X-Duitku-Signature", "sha256=deadbeef")

	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

// TestCFInbox_StaleTimestamp ensures old timestamps are 401.
func TestCFInbox_StaleTimestamp(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "key")
	_ = os.WriteFile(keyFile, []byte("super-secret-test-key-32-chars"), 0o600)

	srv, err := New(Config{
		LandingDir:    filepath.Join(dir, "landing"),
		HMACKeyFile:   keyFile,
		MaxReplaySkew: 30 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}

	body, contentType := buildMultipart(t, map[string]string{
		"bank":        "maybank",
		"received_at": time.Now().UTC().Format(time.RFC3339),
	}, "file", "x.pdf", []byte("body"))

	stale := strconv.FormatInt(time.Now().Add(-1*time.Hour).Unix(), 10)
	sig := signTest(t, []byte("super-secret-test-key-32-chars"), body, stale)

	req := httptest.NewRequest(http.MethodPost, "/cf-inbox", bytes.NewReader(body))
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("X-Duitku-Timestamp", stale)
	req.Header.Set("X-Duitku-Signature", "sha256="+sig)

	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 on stale ts, got %d", rec.Code)
	}
}

// TestHealthz ensures /healthz returns 200.
func TestHealthz(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "key")
	_ = os.WriteFile(keyFile, []byte("super-secret-test-key-32-chars"), 0o600)
	srv, err := New(Config{LandingDir: filepath.Join(dir, "landing"), HMACKeyFile: keyFile})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
}

// buildMultipart constructs a multipart/form-data body with the given
// text fields and one file part. Used by tests only.
func buildMultipart(t *testing.T, fields map[string]string, fileField, filename string, content []byte) ([]byte, string) {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	for k, v := range fields {
		if err := mw.WriteField(k, v); err != nil {
			t.Fatal(err)
		}
	}
	fw, err := mw.CreateFormFile(fileField, filename)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(fw, bytes.NewReader(content)); err != nil {
		t.Fatal(err)
	}
	if err := mw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes(), mw.FormDataContentType()
}

// signTest reproduces the receiver's HMAC scheme for tests.
func signTest(t *testing.T, key, body []byte, ts string) string {
	t.Helper()
	h := hmac.New(sha256.New, key)
	h.Write([]byte(ts))
	h.Write([]byte("."))
	h.Write(body)
	return hex.EncodeToString(h.Sum(nil))
}
