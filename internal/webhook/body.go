package webhook

import "bytes"

// newBytesReader wraps bytes.NewReader in a way the http server is happy
// to consume as the request body. We need this because we read the body
// into memory once to compute the HMAC and then have to re-feed it to
// the multipart parser.
//
// Kept as a tiny helper so the intent is obvious to anyone reading
// ServeHTTP for the first time.
func newBytesReader(b []byte) *bytes.Reader {
	return bytes.NewReader(b)
}
