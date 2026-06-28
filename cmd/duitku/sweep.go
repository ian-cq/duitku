package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

// newSweepCmd returns the `duitku sweep` subcommand.
//
// Sweep walks /landing/<bank>/inbox/, parses each file, normalises the
// transactions, dedups via sqlite, and emits to the configured target
// (today: Firefly III).
//
// This is the in-cluster CronJob entry. It is also runnable locally for
// debugging if you point --landing-dir at a directory of test files.
//
// Currently a stub: it logs that no parsers are wired and exits 0.
// Parsers and the firefly emitter land in subsequent phases.
func newSweepCmd() *cobra.Command {
	var (
		landingDir   string
		passwordFile string
		accountsFile string
		dryRun       bool
	)
	cmd := &cobra.Command{
		Use:   "sweep",
		Short: "Parse files in landing/<bank>/inbox/ and import them",
		RunE: func(cmd *cobra.Command, _ []string) error {
			// TODO(phase 1+): wire parsers, normaliser, dedup, firefly emitter.
			fmt.Println("duitku sweep: no parsers wired yet; exiting cleanly")
			fmt.Printf("  landing_dir=%s passwords=%s accounts=%s dry_run=%v\n",
				landingDir, passwordFile, accountsFile, dryRun)
			return nil
		},
	}
	cmd.Flags().StringVar(&landingDir, "landing-dir", envOr("DUITKU_LANDING_DIR", "/landing"), "landing PVC mount path")
	cmd.Flags().StringVar(&passwordFile, "passwords-file", envOr("DUITKU_PASSWORDS_FILE", "/etc/duitku/passwords.toml"), "TOML file with PDF password list")
	cmd.Flags().StringVar(&accountsFile, "accounts-file", envOr("DUITKU_ACCOUNTS_FILE", "/etc/duitku/accounts.yaml"), "YAML file with account_id -> firefly account name")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "parse + dedup but do not POST to the emitter")
	return cmd
}
