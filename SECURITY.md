# Security Policy

If you discover a security vulnerability, please email security@analytically.ai.
Please do not open a public issue for security reports.

## Trust boundary: configuration can disable checks by design

Rule settings and contracts are configuration, not code under check.
Anyone who can write them can weaken or silence checks:

- `DATA.md` `tq-config` `rules:` entries can enable, disable, re-tier, or
  re-severity any rule, and `interrupt_edits: false` silences per-edit
  interruption. `~/.config/truequery/config.yaml` (personal defaults) and
  the `--enable-rule` / `--disable-rule` / `--rule-tier` CLI flags adjust
  the same settings. Precedence is CLI flag > project config > user
  config > shipped default.
- `contracts/*.yaml` keys (grain, keys, nullability, allowed values) declare
  facts the file-level checks cannot see. A wrong or missing key weakens or
  silences the checks that consume it, by design.

Treat both as human-owned: agents may only add the narrowest ignore with a
reason, and the hook warns when the `tq-config` rules block changes
mid-session. `truequery review` prints a `Settings` line in the receipt whenever
the effective rule settings differ from the shipped defaults, so a weakened
configuration is visible at the point of use.
