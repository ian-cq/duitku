package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/ian-cq/duitku/internal/webhook"
	"github.com/spf13/cobra"
)

// newServeCmd returns the `duitku serve` subcommand.
//
// Runs the HTTP webhook receiver that the Cloudflare Email Worker calls.
// HMAC-validates incoming POSTs and writes attachments to the landing
// directory; nothing here parses or talks to Firefly — that's sweep's job.
//
// Environment / flags:
//
//	--addr                or DUITKU_ADDR              (default ":8080")
//	--landing-dir         or DUITKU_LANDING_DIR        (default "/landing")
//	--hmac-key-file       or DUITKU_HMAC_KEY_FILE     (required)
//	--max-replay-skew     or DUITKU_MAX_REPLAY_SKEW   (default "5m")
//	--max-attachment-size or DUITKU_MAX_ATTACH_SIZE   (default 26214400 = 25 MiB)
func newServeCmd() *cobra.Command {
	var (
		addr         string
		landingDir   string
		hmacKeyFile  string
		replaySkew   time.Duration
		maxAttachSz  int64
		readTimeout  time.Duration
		writeTimeout time.Duration
	)

	cmd := &cobra.Command{
		Use:   "serve",
		Short: "Run the HTTP webhook receiver",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if hmacKeyFile == "" {
				return errors.New("--hmac-key-file (or DUITKU_HMAC_KEY_FILE) is required")
			}

			srvCfg := webhook.Config{
				LandingDir:        landingDir,
				HMACKeyFile:       hmacKeyFile,
				MaxReplaySkew:     replaySkew,
				MaxAttachmentSize: maxAttachSz,
			}
			handler, err := webhook.New(srvCfg)
			if err != nil {
				return fmt.Errorf("init webhook: %w", err)
			}

			srv := &http.Server{
				Addr:         addr,
				Handler:      handler,
				ReadTimeout:  readTimeout,
				WriteTimeout: writeTimeout,
			}

			ctx, stop := signal.NotifyContext(cmd.Context(), syscall.SIGINT, syscall.SIGTERM)
			defer stop()

			errCh := make(chan error, 1)
			go func() {
				slog.Info("webhook listening", "addr", addr, "landing_dir", landingDir)
				errCh <- srv.ListenAndServe()
			}()

			select {
			case err := <-errCh:
				if !errors.Is(err, http.ErrServerClosed) {
					return fmt.Errorf("listen: %w", err)
				}
				return nil
			case <-ctx.Done():
				slog.Info("shutdown signal received")
				shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				return srv.Shutdown(shutdownCtx)
			}
		},
	}

	cmd.Flags().StringVar(&addr, "addr", envOr("DUITKU_ADDR", ":8080"), "address to listen on")
	cmd.Flags().StringVar(&landingDir, "landing-dir", envOr("DUITKU_LANDING_DIR", "/landing"), "landing PVC mount path")
	cmd.Flags().StringVar(&hmacKeyFile, "hmac-key-file", envOr("DUITKU_HMAC_KEY_FILE", ""), "path to file containing the HMAC shared secret")
	cmd.Flags().DurationVar(&replaySkew, "max-replay-skew", 5*time.Minute, "reject requests with timestamps older than this")
	cmd.Flags().Int64Var(&maxAttachSz, "max-attachment-size", 25<<20, "reject attachments larger than this (bytes)")
	cmd.Flags().DurationVar(&readTimeout, "read-timeout", 60*time.Second, "HTTP read timeout")
	cmd.Flags().DurationVar(&writeTimeout, "write-timeout", 30*time.Second, "HTTP write timeout")
	return cmd
}
