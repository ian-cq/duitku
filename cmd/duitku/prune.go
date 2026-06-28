package main

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/spf13/cobra"
)

// newPruneCmd returns the `duitku prune` subcommand.
//
// Walks /landing/<bank>/processed/ and unlinks files older than
// --retention-days. failed/ files are NEVER pruned by this command; they
// stay until a human triages them.
func newPruneCmd() *cobra.Command {
	var (
		landingDir     string
		retentionDays  int
		dryRun         bool
	)
	cmd := &cobra.Command{
		Use:   "prune",
		Short: "Delete processed files older than --retention-days",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if retentionDays < 0 {
				return errors.New("--retention-days must be >= 0")
			}
			cutoff := time.Now().Add(-time.Duration(retentionDays) * 24 * time.Hour)
			slog.Info("prune starting", "landing_dir", landingDir, "retention_days", retentionDays, "cutoff", cutoff, "dry_run", dryRun)

			banks, err := listDirs(landingDir)
			if err != nil {
				return fmt.Errorf("list landing dir: %w", err)
			}

			var (
				pruned int
				bytes  int64
			)
			for _, bank := range banks {
				processed := filepath.Join(landingDir, bank, "processed")
				if _, err := os.Stat(processed); os.IsNotExist(err) {
					continue
				}
				err := filepath.Walk(processed, func(path string, info os.FileInfo, err error) error {
					if err != nil {
						return err
					}
					if info.IsDir() {
						return nil
					}
					if info.ModTime().After(cutoff) {
						return nil
					}
					if dryRun {
						slog.Info("would delete", "path", path, "size", info.Size(), "mtime", info.ModTime())
						pruned++
						bytes += info.Size()
						return nil
					}
					if err := os.Remove(path); err != nil {
						slog.Warn("delete failed", "path", path, "err", err)
						return nil
					}
					pruned++
					bytes += info.Size()
					return nil
				})
				if err != nil {
					slog.Warn("walk failed", "bank", bank, "err", err)
				}
			}
			slog.Info("prune done", "files_pruned", pruned, "bytes_pruned", bytes)
			return nil
		},
	}
	cmd.Flags().StringVar(&landingDir, "landing-dir", envOr("DUITKU_LANDING_DIR", "/landing"), "landing PVC mount path")
	cmd.Flags().IntVar(&retentionDays, "retention-days", 30, "delete processed files older than this")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "log what would be deleted but do not delete")
	return cmd
}

// listDirs returns the immediate subdirectory names under path.
// Returns empty slice if path does not exist.
func listDirs(path string) ([]string, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			out = append(out, e.Name())
		}
	}
	return out, nil
}
