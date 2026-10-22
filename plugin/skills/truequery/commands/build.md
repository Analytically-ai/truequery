# /truequery:build — build a pipeline, model or load end to end

When: "build a daily pipeline that loads Stripe payouts", "add a
product dimension", "load the support-tickets export". For data engineers and
analytics engineers. This command follows `flow.md`; the steps below
are the build-specific parts.

1. **Look** (flow step 1): `truequery context --target <path>`; detect the
   stack from the repo (dbt project, Airflow/Dagster DAGs, plain SQL,
   pandas) and the existing conventions (naming, materializations,
   test style). Read the source schema read-only if reachable.
2. **Classify the intent** — ingest, transform, or orchestrate — then
   **ask** (flow step 2, one call, each with a default):
   - *ingest:* load pattern full / incremental / CDC (default:
     incremental on the source's update timestamp), unique key
     (default: the source primary key), late-data window (default:
     3 days), idempotent re-runs, schema-drift handling
     (`ingest/idempotent-batch-loads`,
     `ingest/watermark-and-late-data-policy`,
     `ingest/handle-schema-drift-explicitly`,
     `ingest/dedupe-on-natural-key-before-append`).
   - *transform:* grain (default: one row per key), keys, incremental
     `unique_key` and lookback (`model/unique-key-merge-key`,
     `model/incremental-lookback-window`,
     `model/grain-preserved-joins`).
   - *orchestrate:* schedule and timezone, `catchup`/backfill policy
     (default: `catchup=False`, backfill only on request), retries,
     SLA/alerting (`orch/explicit-catchup-policy`,
     `orch/idempotent-retries-with-limits`, `orch/sla-deadline-alerts`).
3. **Contract first, build, check, receipt, remember** — flow steps
   3–7 unchanged, in the detected stack. Tests must run here (flow
   step 4: dbt tests, or SQL assertions on the available engine), and
   destructive actions need confirmation first — never rebuild raw
   data unasked.
4. Apply the conflict rule: if the request contradicts a contract or
   definition ("just append the feed, keys don't matter" against a
   contracted key), say so before writing and follow the contract
   unless the user confirms the override.

Output: code + tests + contracts + receipt. `model`, `ingest` and
`orchestrate` are aliases for this command with the intent preset.
