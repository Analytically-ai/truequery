# /truequery:init — start a project to the standard

When: first run in a repo, or when `truequery context` reports
CONTEXT_STALE. Stack-neutral: plain SQL folders, dbt, SQLMesh.
`/truequery init` is the primary setup: mechanical init plus the teach
interview (step 6). Bare `truequery init` in a terminal is mechanical
only (steps 1–5) — for scripts and CI.

1. Run `truequery init` in the project root. It detects the stack and
   dialect (dbt `profiles.yml`, SQLMesh config, else auto) and writes
   `DATA.md` with mode globs, plus `contracts/` seeds from schema YAML
   if any.
2. Read back `DATA.md`. Fill in owners, layer conventions and SLAs —
   the generator leaves TODO markers, never invented facts.
3. For each seeded contract, confirm grain and keys with the team or
   mark them TODO. A contract with an unconfirmed grain is a draft:
   say so when you cite it.
4. Run `truequery review` once. The receipt is your baseline: what is
   checked at each evidence level, and what is not.
5. Never overwrite `DATA.md` or `contracts/` user edits without
   `--force`, and never install hooks here (that is `/truequery hooks
   on`).

6. Teach interview (skip questions already answered by the repo), in
   one call as in `flow.md` step 2 (four in the question tool, a fifth
   in text), each with a suggested default — (a) what counts as
   revenue / the headline metric, (b) which accounts are test or
   internal, (c) the money convention (smallest unit? currency?),
   (d) the business date for orders/events, (e) who owns corrections.
   Write the answers into `DATA.md` as house definitions and into
   contracts where they pin a grain or key. In a non-interactive
   session, record the defaults as assumptions instead and say so.

7. Updates. TrueQuery ships improvements often, and Claude Code doesn't
   auto-update third-party plugins unless asked. Tell the user once, in
   one sentence, how to turn it on (`/plugin` → Marketplaces →
   analytically → Enable auto-update). If this is a shared team repo,
   offer to turn it on for everyone: with a yes, merge this into the
   project's `.claude/settings.json`, keeping everything else in it:
   `"extraKnownMarketplaces": {"analytically": {"source": {"source":
   "github", "repo": "Analytically-ai/truequery"}, "autoUpdate": true}}`.
   Skip this step when TrueQuery was installed from a local clone: there,
   `git pull` is the update.

Output: `DATA.md` + `contracts/` present, house definitions recorded,
baseline receipt quoted, the update setting explained.
