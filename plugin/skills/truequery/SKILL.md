---
name: truequery
description: TrueQuery data-work partner. Use for building pipelines/models, defining metrics, analyzing data, or reviewing SQL — one standard, checked offline as you work.
---

# TrueQuery

One standard for data work, checked offline as you work. The checker finds
bugs; you judge the rest. This skill works standalone — no hooks, no
warehouse, no network.

## First move

Most sessions start with `/truequery build …` or `/truequery ask …`,
which follow `flow.md` (look → ask in one call → contract first → build
→ check → receipt → remember). Whatever the command, call
`truequery context [--target <path>]` before touching data code (`tq`
is the short alias). It returns the project truth (stack, dialect, modes), only the
contracts your target references, and directives (`MODE`,
`CONTRACT_MISSING`: no contract for that table; `CONTEXT_STALE`: no
`DATA.md`, so suggest `/truequery init`). Obey them; don't reload them.

## Invocation

`/truequery <command> …` routes on the first word (`build`, `ask`,
`metric`, `analyze`, `review`, `init`, `harden`, `hooks`). If the harness
installs the skill namespaced and only accepts `/truequery:build`,
that form works too — same procedures. `model`, `ingest` and
`orchestrate` are aliases that preset `build`'s intent.

## Principles

1. A silently wrong number is worse than a loud error. Prefer checks
   that prove wrong answers, lost rows, or non-determinism.
2. The standard is data, not opinion. Every practice is a record with
   sources and confidence (`truequery rules <slug>` shows one).
   Disagreements are recorded, not hidden.
3. The checker discovers; you interpret. Never claim a finding it
   didn't emit, and never launder its output as your own judgment.
4. Receipts make a pass mean something. Show the checker's receipt as
   printed, then add what was **not** checked, and why.
5. Narrowest ignore with a reason. One line, one value, one `--reason`.
   File-level ignores are the human's call.

## Modes

Work lands in one mode, resolved from `DATA.md` globs:

| Mode | Meaning | Severity note |
|---|---|---|
| Explore | ad-hoc analysis, notebooks | errors downgrade to warnings |
| Build | models, pipelines, loaders | full severity |
| Change | migrations, refactors, backfills | full severity |
| Assure | DQ runs, tests, reconciliation | full severity |

Check the mode before calling a practice wrong (`SELECT *` is fine in Explore).

## Commands

| Command | Kind | Purpose |
|---|---|---|
| `build` | full | build a pipeline, model or load (intent: ingest/transform/orchestrate) |
| `ask` | full | plain-English answer for non-technical users (uses `analyze`) |
| `init` | full | detect stack/dialect, write `DATA.md`, seed `contracts/`, teach interview |
| `metric` | full | define a metric once; `metric check` finds drift |
| `analyze` | full | answer a data question to the standard |
| `review` | full | run all checks plus the receipt, then critique judgment-only items |
| `hooks` | full | hooks on/off/status (wraps install/uninstall/status, never double-installs) |
| `harden` | full | idempotency, late data, schema evolution, null/edge guards |
| `model` / `ingest` / `orchestrate` | alias | route to `build` with the intent preset |
| `dq` | draft | the six-dimension data-quality suite |
| `change` | draft | migrations, deprecations, backfills |
| `document` | draft | refresh `DATA.md` and contracts from code |
| `optimize` | draft | cost and performance |
| `adapt` | draft | dialect port |

Full procedures live in `commands/<name>.md` (drafts are intent, not process).

## Evidence levels

`file`: SQL checks · `project`: preview checks (dbt YAML, incremental
models, Airflow) · `contract`: read for context; declared keys feed
`sql/count-star-after-left-join` · `metadata`, `data`: never checked (no
warehouse adapter yet; what you learn by querying is your evidence, not
the checker's). Name the level of every claim you make.

## If the engine isn't available

Run the checker as `truequery` (on PATH via the plugin), never `tq`.
If it fails, say so once, then work by reading: apply `floor.md` and the domain
references to the code in front of you. Label every claim "reviewed
by reading, not checked". Never print a receipt that implies checks
ran — a reading review ends with a caveat list, not a pass.

## When a `[TrueQuery]` message arrives

Triage, in this order:

1. Fix it.
2. Or add the narrowest ignore: `truequery ignore add <rule>
   <path:line> --reason "<who>: <evidence>"`. One line, one value.
3. Or ask once.

Never broaden an ignore to a file or a rule. Each finding is said
once: a repeat means something new changed.

Never change how you are checked. Human-only: `truequery rules set`;
the `rules:`, `interrupt_edits` and `tier_overrides` keys in `DATA.md`;
the user config; hand edits to `.truequery/ignores.json`; turning hooks
off. If rules misfire, ignore one line or ask — don't retune the checker.
When you see "rule settings changed this session", stop and confirm
with the user before relying on new settings.

## Reference

- `floor.md`: the non-negotiables the checker enforces (generated).
- `reference/<domain>.md`: all practices per domain (generated).
  Load only the domains your task touches.
- Judgment-only practices are taught, not checked. After
  `truequery review`, critique the in-scope ones yourself.
