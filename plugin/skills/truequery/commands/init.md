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
   **Data files and no contracts for them** (`.csv`, `.tsv`, `.jsonl`,
   `.parquet` in the repo, and no contract whose `location` covers them):
   run `truequery init --from <the folders that hold them>` instead. It
   also writes draft contracts (`status: draft`, with `inferred_from`):
   types, keys, time zones, status values, units and path bases from a
   2,000-row sample, read as text. It never writes over a contract and
   refuses paths outside the project or a declared base. Its output lists
   the open questions; keep them for step 6.
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
   revenue / the headline metric, (b) which accounts or status values
   are test, internal, or expected absences (`means: absent_expected`),
   (c) the money and unit conventions (`unit: minor|major` + `currency`,
   or binary vs decimal file sizes `GiB` vs `GB`), (d) the business date
   and time zone of naive timestamps (`UTC`, `naive`, or `local:<zone>`),
   (e) who owns corrections and any base directories (`bases:` in
   `DATA.md` `tq-config`), and (f) only when `truequery status` says the
   data reader is "needed here" or "useful here" and `DATA.md` has no
   `setup: data` answer yet: "Install the data reader (DuckDB, a 15–25 MB
   download from PyPI) so TrueQuery can check these files against the contracts?
   Without it, code and contracts are still checked." Default: no.
   On a yes, run `truequery setup data --yes`; on a no,
   `truequery setup data --no`. Both record the answer in `DATA.md`, so
   it's never asked again. Never install without the yes. (f) counts
   toward the five: when (a)–(e) are all still open, don't ask it; end
   init with one line offering the data reader instead. In a
   non-interactive session, run neither command: nobody answered.
   After `init --from`, the interview asks only what it couldn't settle,
   from its question list and the `# TODO` lines in the drafts: the grain
   and key, what each `means: TODO` status value means for the file
   (is it there: `present`; absent on purpose: `absent_expected`; or
   absent because something went wrong: `absent_unexpected`. Only a
   missing `present` file fails `truequery verify`; the other two are
   counted), the zone of
   `time_zone: naive` or mixed timestamps, a unit taken from a column's
   name (GB or GiB?), a path base, and `pii: true` guesses. These replace
   (b)–(e) where they overlap; group them per dataset, and when there are
   more than the five slots, ask the ones that change results first
   (statuses, zones, units) and list the rest as open in the output.
   A confirmed answer goes into the contract and removes that column's
   `status: draft` and its `# TODO` comment; an unanswered one stays a
   draft. Never fill a `means`, zone or unit the user didn't confirm.
   A `values` list read from the sample is a draft too: ask whether it's
   complete before it becomes a rule. Remove the contract's own `status:
   draft` (and the header comment `init --from` wrote) only when the grain
   and keys are confirmed and no column is still a draft.
   Write the answers into `DATA.md` as house definitions, into
   `contracts/*.yaml` (`version: 2`), and into `metrics/*.yaml` where
   they define a governed metric, then run `truequery contract validate`.
   In a non-interactive session, record the defaults as assumptions
   instead and say so; leave `init --from` drafts as drafts.

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

Output: `DATA.md` + `contracts/` present (drafts named as drafts, with
their open questions), house definitions recorded, baseline receipt
quoted, the update setting explained.
