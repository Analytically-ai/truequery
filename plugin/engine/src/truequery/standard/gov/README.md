# Governance, security and privacy (`gov`) — draft practice pack

Scope: ownership, RBAC and grants, row filters, PII classification and
masking, audit logging, contracts-as-policy, catalog lineage and
documentation, retention and erasure. Out of scope: org process (team
rituals, access-request workflows, legal review steps) — only what is
checkable in code, contracts, or declared metadata.

All cited URLs live-fetched 2026-10-12.
Warehouse-native controls (Snowflake Dynamic Masking / row access
policies, BigQuery column/row-level security) could not be verified in
this session (docs hosts unreachable from here) and are NOT cited as
sources; practices that depend on them are marked accordingly.

## Enforce these five first

1. `gov/pii-classification-declared` (contract, required, warning) — every
   other privacy control (row filters, masks, grants, retention, erasure)
   keys off classification; without it they all silently skip unlabeled
   columns. It is one of the three facts in the small required contract set.
2. `gov/no-public-privileges` (detector, error) — one PUBLIC grant makes
   data world-readable for every future role; cheapest catastrophic leak to
   prevent, fully checkable from SQL text.
3. `gov/row-policy-for-sensitive-tables` (contract, error) — Postgres tables
   have no policies by default, so tenant/PII scoping otherwise lives only
   in app code where one missing filter leaks across tenants.
4. `gov/no-raw-pii-in-seeds` (detector, error) — raw identifiers in seeds are
   distributed to every repo clone with no access decision; checkable
   offline, irreversible once cloned.
5. `gov/least-privilege-grants` (detector, warning) — now the single
   least-privilege record (grants, role separation by job, per-environment
   adapter credentials); GRANT ALL and admin CI roles are checkable from code.

## Existing tool coverage (integration points, not replacements)

- dbt Core / dbt Mesh: groups + model access + contracts + exposures +
  snapshots cover ownership, interface boundaries, cross-project policy,
  and history. TrueQuery should read these declarations (contract
  enforcement), not re-implement them.
- dbt-project-evaluator: ships checks for documentation coverage and
  model structure that overlap `gov/description-coverage-for-shared-models`
  and parts of `gov/declared-model-owner`; integrate by importing its
  findings, add the privacy-specific gaps (classification, PII) it lacks.
- SQLFluff: parses SQL but enforces style/layout only; use as the SQL
  parser front-end for detector practices (`gov/no-public-privileges`,
  `gov/least-privilege-grants`, `gov/lineage-from-declared-sources`),
  not as a governance checker.
- Great Expectations / Soda: runtime data validation (values, not
  access); complementary — GX/Soda prove data meets expectations while
  gov practices prove only the right eyes see it. No overlap to merge.
- ruff: Python hygiene for pipeline code; no governance overlap, but
  keeps the detector codebase itself clean.
- Warehouse-native enforcement (Snowflake, BigQuery, Databricks Unity
  Catalog) is runtime by definition: TrueQuery's honest boundary is
  checking that policies are *declared* (contract) and flagging gaps,
  never claiming the policy is *correct* without the warehouse.

## Files

18 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| audit-columns-or-snapshots | contract | advisory | no |
| column-grants-for-pii | contract | warning | no |
| declared-model-owner | contract | warning |  |
| description-coverage-for-shared-models | detector | advisory |  |
| erasure-path-documented | contract | warning | no |
| exposures-registered | contract | advisory | no |
| group-private-access | contract | advisory | no |
| least-privilege-grants | detector | warning |  |
| lineage-from-declared-sources | detector | warning |  |
| mesh-public-contracts-only | contract | warning | no |
| no-bypassrls-roles | detector | error |  |
| no-public-privileges | detector | error |  |
| no-raw-pii-in-seeds | detector | error |  |
| pii-classification-declared | contract | warning | yes |
| production-pii-not-in-dev | contract | error | no |
| redacting-views-for-pii | contract | warning | no |
| retention-declared | contract | warning | no |
| row-policy-for-sensitive-tables | contract | error | no |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 6 |
| contract | 12 |
| runtime | 0 |
| judgment | 0 |
| **total** | **18** |

Severity: 5 error, 9 warning, 4 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

