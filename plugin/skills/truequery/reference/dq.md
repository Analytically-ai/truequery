# dq — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## dq/accuracy-proxy-or-unmeasurable

*Accuracy is measured against a declared reference or reported unmeasurable*

No accuracy score is computed from the dataset alone. Either a reference source of truth is declared and drift against it is measured, or the dimension reports UNMEASURABLE with the reason stated.

Why: "Matches reality" cannot be derived from the data being judged; any score computed without ground truth is a fabrication that borrows the authority of the other five dimensions. Fake accuracy scores are worse than none b…

```sql
-- bad
{dimension: accuracy, score: 0.98, verdict: PASS}
# no reference declared; score invented from internal statistics
```
```sql
-- good
{dimension: accuracy, verdict: UNMEASURABLE,
 reason: "no source of truth declared; see reconciliation config"}
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: assure
- severity: assure=advisory
- sources: Introduction to GX Core; SodaCL metrics and checks

## dq/audits-blocking-every-run

*Audits run automatically on every materialization with blocking defaults*

Quality audits execute automatically as part of every model materialization or plan, and a failed audit blocks promotion of the data downstream by default; non-blocking downgrades require explicit per-audit justification.

Why: Opt-in quality gates are skipped exactly when they matter most: rushed hotfixes and large restatements. Automatic blocking audits contain bad data in an isolated environment before production tables are touched.

```sql
-- bad
AUDIT (name assert_price_not_null);  # defined but only run manually
```
```sql
-- good
MODEL (name marts.orders, audits (assert_price_not_null, assert_no_orphans));
-- plan halts before promotion when any audit returns rows
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,change
- severity: build=warning, change=warning
- sources: Auditing; Testing assets with asset checks

## dq/checks-versioned-with-code

*Checks live in versioned files beside the models they guard*

Quality checks are declared in version-controlled files reviewed in the same pull request as the model change (checks YAML, test blocks, or audit files); console-only or click-created checks with no file representation are not the system of record.

Why: Unversioned checks drift from the code they guard: a model changes, its console check does not, and failures (or silence) reference rules nobody can diff, blame, or roll back. Versioned checks make quality reviewable, re…

```sql
-- bad
# threshold exists only in a monitoring console; repo has no record
```
```sql
-- good
checks for dim_customer:
  - row_count > 0
  - duplicate_count(customer_id) = 0
# reviewed, merged, and deployed with the model change
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,change
- severity: build=advisory, change=advisory
- sources: SodaCL metrics and checks; Add data tests to your DAG

## dq/completeness-declare-missing-sentinels

*Missing-value sentinels are declared per column, not assumed to be NULL-only*

Each completeness check evaluates NULL plus an explicit per-column sentinel list (such as empty string, 'N/A', '0000', or 1900-01-01 dates where the domain uses them); a column with no declared sentinels documents that NULL is its only missing representation.

Why: Source systems encode "unknown" as placeholder values far more often than as NULL. A NULL-only check reports 100% complete while placeholder rows corrupt filters, joins, and averages with wrong numbers.

```sql
-- bad
SELECT COUNT(*) - COUNT(email) AS missing FROM customers;
-- '' and 'N/A' count as present
```
```sql
-- good
SELECT COUNT(*) - COUNT(NULLIF(NULLIF(TRIM(email), ''), 'N/A')) AS missing
FROM customers;
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SodaCL metrics and checks

## dq/completeness-fill-rate-thresholds

*Fill-rate thresholds are explicit per column instead of all-or-nothing*

Columns that are legitimately sparse declare an explicit minimum fill rate (for example missing_percent below 5%); key and required columns keep a 100% default. No column is left without a stated threshold.

Why: A single all-or-nothing null rule forces teams to either exempt sparse columns entirely (losing all signal) or drown in failures on legitimately optional data. Explicit per-column rates keep the suite green while still c…

```sql
-- bad
checks for customers:
  - missing_count(phone_number) = 0
# fails every run; team deletes the check instead of tuning it
```
```sql
-- good
checks for customers:
  - missing_percent(phone_number) < 45
  - missing_count(customer_id) = 0
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SodaCL metrics and checks

## dq/completeness-not-null-required-columns

*Every required column has a not-null completeness check*

For each table, the columns declared required in its contract carry an explicit not-null check (NULL count equals zero), and the check runs on every build that writes the table.

Why: Missing keys and measures propagate silently: joins drop rows, aggregates undercount, and downstream consumers cannot distinguish "zero" from "unknown". An explicit per-column check turns a silent wrong-number failure in…

```sql
-- bad
models:
  - name: fct_orders
    columns:
      - name: customer_id
# no test on customer_id; NULLs pass silently
```
```sql
-- good
models:
  - name: fct_orders
    columns:
      - name: customer_id
        data_tests:
          - not_null
          - relationships:
              to: ref('dim_customers')
              field: customer_id
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Add data tests to your DAG; SodaCL metrics and checks

## dq/completeness-table-not-empty

*Every produced table has a non-empty row-count floor check*

Each table a pipeline writes carries a check that its row count is greater than zero (or above a declared minimum) after every write; an empty result fails the build.

Why: A silently empty table is the cheapest catastrophic failure: a broken filter or an empty source produces a valid schema with zero rows, and every downstream consumer reports plausible-looking zeros instead of erroring.

```sql
-- bad
-- no volume assertion; empty output deploys cleanly
CREATE TABLE mart_daily AS SELECT * FROM staging WHERE 1 = 0;
```
```sql
-- good
checks for mart_daily:
  - row_count > 0
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: SodaCL metrics and checks; Introduction to GX Core

## dq/consistency-cross-column-row-rules

*Cross-column business rules within a row are checked as predicates*

Invariants spanning two or more columns in one row (for example end date after start date, total equal to quantity times price) are written as row predicates, and any violating row fails the check.

Why: Single-column checks all pass while the row as a whole is nonsense: a subscription ending before it starts, or a total disagreeing with its parts. These contradictions corrupt duration and money math downstream.

```sql
-- bad
-- each column checked alone; contradictory rows pass
SELECT * FROM subs WHERE end_at IS NULL OR start_at IS NULL;
```
```sql
-- good
SELECT * FROM subs
WHERE NOT (end_at IS NULL OR end_at > start_at);
-- zero rows required
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=error, build=error
- sources: SodaCL metrics and checks; Constraints

## dq/consistency-no-orphan-keys

*Foreign keys are checked for orphans with an anti-join on every build*

Each declared foreign-key relationship carries a referential-integrity check (child rows with no matching parent fail), and orphan count must be zero unless a documented exception with an expiry exists.

Why: Orphan keys silently annihilate rows in inner joins: facts referencing missing dimensions disappear from every mart built on the join, so revenue and counts are wrong with no error anywhere in the logs.

```sql
-- bad
SELECT COUNT(*) FROM order_lines o
JOIN dim_customers c ON o.customer_id = c.customer_id;
-- orphans vanish silently into the inner join
```
```sql
-- good
SELECT o.* FROM order_lines o
LEFT JOIN dim_customers c ON o.customer_id = c.customer_id
WHERE c.customer_id IS NULL;
-- zero rows required; any row fails the build
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: Add data tests to your DAG; Reference checks

## dq/consistency-paired-aggregate-reconciliation

*Derived tables reconcile agreed aggregates against their source*

Each derived table declares at least one reconciliation pair (row count or a summed measure) against its source, and the build fails when the drift exceeds the declared tolerance.

Why: Transforms can lose or duplicate rows while every row-level check passes: a dropped partition or a double-counted file changes totals without violating any single-row rule. Aggregate reconciliation is the only determinis…

```sql
-- bad
-- mart built; nobody compares its totals to the source
```
```sql
-- good
SELECT (SELECT SUM(amount) FROM staging_payments) AS source_total,
       (SELECT SUM(amount) FROM mart_payments) AS mart_total;
-- build fails unless the two match within tolerance
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: SodaCL metrics and checks; Reference checks

## dq/schema-conformance-checked-on-read

*Schema conformance is checked before content checks run*

Before any content check executes, the scan verifies required columns are present with expected types; schema mismatch aborts the run as ERROR rather than letting content checks pass vacuously or crash mid-suite.

Why: Content checks against a drifted schema lie in both directions: a missing column can crash the suite (masking all other results) or be silently skipped (reporting green on data never examined). A schema gate first keeps …

```sql
-- bad
-- content checks run directly; dropped column crashes suite mid-run
```
```sql
-- good
Schema Check [PASSED]
row_count > 0 [PASSED]
missing_count(email) = 0 [PASSED]
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: SodaCL metrics and checks; Constraints

## dq/unenforced-constraints-are-tested

*Constraints the warehouse does not enforce are covered by tests*

On platforms where declared constraints are informational rather than enforced, every relied-upon unique, primary-key, or foreign-key constraint has a corresponding data test; DDL declarations alone are never treated as guarantees.

Why: A PRIMARY KEY clause that the engine ignores is documentation masquerading as protection. Teams that trust it skip the test, and duplicate or orphan rows accumulate behind a schema that claims they are impossible.

```sql
-- bad
CREATE TABLE orders (order_id INT PRIMARY KEY, ...);
-- trusted as enforced; no test exists
```
```sql
-- good
CREATE TABLE orders (order_id INT PRIMARY KEY, ...);
-- plus: unique + not_null tests on order_id in the pipeline suite
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Constraints; PostgreSQL Constraints

## dq/uniqueness-composite-grain

*Multi-column grains are tested as a composite, not column by column*

When a table's grain is a tuple of columns (for example line items keyed by order_id plus line_number), the uniqueness check covers the full tuple; per-column uniqueness is asserted only for columns that are genuinely unique alone.

Why: Testing each grain column separately either fails spuriously (order_id repeats across lines, as designed) or, when "fixed" by deleting the check, leaves the real composite key untested and duplicate line items free to in…

```sql
-- bad
columns:
  - name: order_id
    data_tests: [unique]   # wrong: repeats per line by design
```
```sql
-- good
data_tests:
  - dbt_utils.unique_combination_of_columns:
      combination_of_columns: [order_id, line_number]
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Add data tests to your DAG; Expectation Gallery

## dq/uniqueness-key-no-duplicates

*Declared keys carry a zero-duplicate uniqueness check*

Every declared primary key or unique identifier has a check asserting zero duplicate values (COUNT(*) minus COUNT(DISTINCT key) equals zero), enforced on every write to the table.

Why: Duplicate keys fan out every downstream join, double-counting revenue, users, or events. The failure is multiplicative and silent: each dup row inflates all measures built on the table.

```sql
-- bad
SELECT order_id, COUNT(*) AS n FROM fct_orders
GROUP BY order_id HAVING COUNT(*) > 1;
-- query exists in a notebook but blocks nothing
```
```sql
-- good
models:
  - name: fct_orders
    columns:
      - name: order_id
        data_tests:
          - unique
          - not_null
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Add data tests to your DAG; PostgreSQL Constraints

## dq/unit-tests-with-fixtures

*Transform logic is unit-tested against fixed input fixtures*

Models with non-trivial logic carry unit tests that feed fixed input rows (fixtures) through the model SQL and assert exact expected output rows, run in CI without warehouse data.

Why: Data tests only observe outputs after production runs; a logic error ships bad data first and is caught later. Fixture unit tests catch the error before merge, on edge cases (empty inputs, boundary values, tie handling) …

```sql
-- bad
-- only production-output tests exist; logic change merges untested
```
```sql
-- good
test_rank_ties:
  model: mart_ranked_txns
  inputs: {staging_txns: [{rows: [...ties...]}]}
  outputs: {query: [{rows: [...expected winners...]}]}
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: Unit tests; Testing

## dq/validity-allowed-value-sets

*Categorical columns are checked against declared allowed-value sets*

Each categorical column declares its allowed values in the contract, and a check fails any row whose non-missing value falls outside the set.

Why: Undeclared domain drift (a new status code, a renamed plan tier) flows into CASE WHEN logic that silently buckets unknown values as NULL or "other", producing wrong segment numbers that look complete.

```sql
-- bad
SELECT * FROM orders WHERE status NOT IN
  ('pending','completed','cancelled','refunded');
-- ad-hoc query, runs nowhere automatically
```
```sql
-- good
columns:
  - name: status
    data_tests:
      - accepted_values:
          values: ['pending','completed','cancelled','refunded']
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Add data tests to your DAG

## dq/validity-enforce-model-contracts

*Breaking schema changes are blocked by enforced model contracts*

Models that feed other teams or marts enable enforced contracts naming every column with its data type, so a renamed, dropped, or retyped column fails the build instead of propagating.

Why: Schema drift is the highest-blast-radius data failure: one renamed column breaks every downstream model and consumer at once. A contract turns that coordinated outage into a single early build failure with the culprit na…

```sql
-- bad
models:
  - name: stg_customers   # no contract; renames flow downstream
```
```sql
-- good
models:
  - name: stg_customers
    config:
      contract: {enforced: true}
    columns:
      - name: customer_id
        data_type: int
        constraints: [{type: not_null}]
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: Model contracts

## dq/validity-range-format-type-rules

*Numeric ranges, string formats, and types are checked per column*

Columns with a known shape declare range, format, length, or type rules in the contract (for example amount non-negative, email matching a pattern, code within a length bound), and a check fails rows violating them.

Why: Out-of-shape values are the classic wrong-number source: negative amounts, malformed identifiers that break joins, and type drift (integer ids arriving as floats) that silently changes join and aggregation behavior.

```sql
-- bad
-- no constraint; negative amounts persist for months
amount NUMERIC
```
```sql
-- good
ALTER TABLE lines ADD CONSTRAINT positive_amount CHECK (amount >= 0);
-- plus a validity check on the pipeline for warehouses without enforcement
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: PostgreSQL Constraints; SodaCL metrics and checks
