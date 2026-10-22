# flow.md — the shared build flow (v1)

`build`, `metric` and `init` follow all seven steps. The answer
commands (`ask`, `analyze`) change no pipeline code: they run steps 1
(look), 2 (ask), 5 (check the queries they write) and 6 (receipt, in
plain words for `ask`), and step 7 only when the user settles a
definition. `review` runs steps 1, 5 and 6. Procedures cite practice slugs (`truequery rules <slug>` shows
one); never invent a practice. If a needed practice is missing, say it
is a gap in the standard instead of improvising one.

## 1. Look

- Run `truequery context --target <path>`; read the relevant models, YAML and
  DAGs; inspect the schema read-only if a warehouse connection or file
  (e.g. DuckDB) is available.
- Write down what was discovered: stack, the tables/contracts in scope,
  grain and keys where known. Discover, don't ask (step 2 is only for
  what cannot be discovered).

## 2. Ask

- **Discover before asking.** A question answerable from the warehouse,
  the repo or the contracts (e.g. "do subscription charges also appear
  as orders?") is checked in step 1, never asked. Only genuinely
  business questions — whose answer cannot be discovered and that
  change the output — reach the user.
- Gather **all** questions first, then ask them in **one** question-tool
  call (**at most 4** in the call; a 5th goes in text). Never one
  pop-up per question.
- Each carries a suggested default: "I'll assume one row per payment
  intent unless you say otherwise."
- Non-interactive session, or the user says "just do it": use the
  defaults and list them as assumptions in the receipt (step 6).

## 3. Contract first

Before code, write or extend `contracts/<name>.yaml`: grain, keys,
columns with units, freshness, PII flags, owner if known. A contract
written from a step-2 answer cites that answer; an unconfirmed grain is
marked TODO and called a draft when cited
(`model/grain-preserved-joins`, `dq/uniqueness-composite-grain`).

## 4. Build

- Code in the project's own stack and conventions (dbt / Airflow /
  Dagster / plain SQL / pandas) — detected in step 1, never imposed.
- Tests ship with it: `unique` + `not_null` on the key
  (`dq/uniqueness-key-no-duplicates`,
  `dq/completeness-not-null-required-columns`), accepted values on
  status enums (`dq/validity-allowed-value-sets`), a freshness check on
  sources (`ingest/declare-source-freshness`).
- Tests must be **runnable here, not just declared.** If dbt is
  installed, ship dbt tests and run `dbt test`. If it is not, ship the
  key/grain/reconciliation checks as SQL assertions that run on the
  available engine (`tests/*.sql` plus a runner, or the project's query
  tool), run them, and report pass/fail in the receipt. Declarative
  dbt tests ship as well either way.
- **Destructive actions need confirmation first:** regenerating raw or
  ingested data, dropping or replacing source tables, rewriting
  history (`ops/full-refresh-approval`,
  `ops/pre-destructive-snapshot`). Never rebuild raw data unasked.

## 5. Check

Run `truequery review` on what was written. Fix real findings; report the
rest. Never claim a finding the checker did not emit, and never hide
one it did.

## 6. Receipt

Every command ends with: what was built · what was checked (tq evidence
levels) · what was **not** checked, and why · assumptions (step-2
defaults used) · **conflicts** (see the conflict rule).

## 7. Remember

Answers to step-2 questions go into `DATA.md` (definitions,
conventions) or into contracts. Never into rule settings — `truequery rules
set` is human-only, and agents never touch the `rules:` block.
If the project has no DATA.md yet, the first build creates a minimal
one (definitions + decisions so far, marked as the agent's working
notes) and says so in the receipt — answers are never kept only in
the contract or the chat.

## The conflict rule (every command, `analyze` and `ask` included)

When a user's request contradicts a definition or contract — "sum the
amount column" where the contract stores amounts in minor units, "count
signups" where the house definition excludes internal accounts:

1. Say so **before writing**, naming the definition and the conflict.
2. Do what the definition says unless the user confirms the override.
   The headline number always uses the definition's reading;
   alternatives go below it.
3. Record the conflict in the receipt (step 6).

Silent compliance with a contradicted definition is a failure of the
command, even if the SQL is internally correct.
