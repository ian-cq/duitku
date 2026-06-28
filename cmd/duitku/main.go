// Package main is the duitku CLI entry point.
//
// Subcommands:
//   serve  - run the HTTP webhook receiver (Cloudflare Email Worker target)
//   sweep  - parse files in landing/<bank>/inbox/ and import them
//   prune  - delete processed files older than --retention-days
//   parse  - parse a local file, emit canonical JSON (todo)
//   import - parse + post to a target emitter (todo)
//
// All subcommands honour DUITKU_LOG_LEVEL (debug|info|warn|error).
package main

import (
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

// version is overridden at link time via -ldflags "-X main.version=...".
var version = "dev"

func main() {
	setupLogger()

	root := &cobra.Command{
		Use:           "duitku",
		Short:         "Malaysian bank statement parser + Firefly III importer",
		Version:       version,
		SilenceUsage:  true,
		SilenceErrors: true,
	}

	root.AddCommand(
		newServeCmd(),
		newSweepCmd(),
		newPruneCmd(),
		newParseCmd(),
		newImportCmd(),
	)

	if err := root.Execute(); err != nil {
		slog.Error("command failed", "err", err)
		os.Exit(1)
	}
}

func setupLogger() {
	lvl := slog.LevelInfo
	switch strings.ToLower(os.Getenv("DUITKU_LOG_LEVEL")) {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	}
	h := slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: lvl})
	slog.SetDefault(slog.New(h))
}

// envOr returns the env var if set, otherwise the fallback.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// must panics if err is non-nil. Use sparingly, only for boot-time invariants.
func must(err error, what string) {
	if err != nil {
		panic(fmt.Sprintf("%s: %v", what, err))
	}
}
