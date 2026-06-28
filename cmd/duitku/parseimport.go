package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

// newParseCmd returns the `duitku parse` subcommand.
//
// One-shot CLI: parse a local PDF/CSV and emit canonical JSON to stdout.
// Useful for community users who don't run k8s.
//
// Stub today.
func newParseCmd() *cobra.Command {
	var bank string
	cmd := &cobra.Command{
		Use:   "parse <file>",
		Short: "Parse a local PDF/CSV and emit canonical JSON to stdout",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			// TODO(phase 1): wire parsers per --bank.
			return fmt.Errorf("not yet implemented: parse %s --bank %s", args[0], bank)
		},
	}
	cmd.Flags().StringVar(&bank, "bank", "", "bank identifier: maybank | uob | ryt")
	_ = cmd.MarkFlagRequired("bank")
	return cmd
}

// newImportCmd returns the `duitku import` subcommand.
//
// One-shot CLI: parse a local file and POST to a target emitter (e.g. Firefly).
//
// Stub today.
func newImportCmd() *cobra.Command {
	var (
		bank       string
		emitter    string
		target     string
	)
	cmd := &cobra.Command{
		Use:   "import <file>",
		Short: "Parse a local file and post to an emitter",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			// TODO(phase 2): wire emitters per --emit.
			return fmt.Errorf("not yet implemented: import %s --bank %s --emit %s --to %s", args[0], bank, emitter, target)
		},
	}
	cmd.Flags().StringVar(&bank, "bank", "", "bank identifier: maybank | uob | ryt")
	cmd.Flags().StringVar(&emitter, "emit", "firefly", "emitter: firefly | json | csv")
	cmd.Flags().StringVar(&target, "to", "", "emitter target (e.g. Firefly base URL)")
	_ = cmd.MarkFlagRequired("bank")
	return cmd
}
