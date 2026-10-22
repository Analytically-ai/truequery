# SQL-language correctness pack (cross-cutting, prefix `sql`)

Scope: parse-level and semantic SQL rules that hold across warehouses
(Postgres, BigQuery, Snowflake, SQL Server, Databricks, DuckDB), plus a
few dialect-scoped rules (`applies_to` names the dialects). The checker enforces
only rules where a query objectively returns a wrong answer, loses data,
or is non-deterministic; those carry `severity_suggestion: error` only when
a proof shows the wrong answer or the loss and the detector has no
legitimate-intent false positives. Heuristics whose failure depends on
intent are `warning`; performance and exploration practices are
`advisory`. Rules run on the Jinja pre-render template for dbt models where
markers like `is_incremental()` carry meaning.

## Every proof is machine-checked

Each correctness record carries a `proof` block that runs as-is in DuckDB:

```yaml
proof:
  setup: |            # DuckDB SQL, <= 10 rows per table
  setup_alt: |        # only for non-determinism / changing-state proofs
  bad: "<query>"
  bad_result: "<exact repr of DuckDB fetchall()>"   # or "ERROR: <ExceptionClass>"
  bad_result_alt: "<repr of bad on setup_alt>"
  good: "<query>"
  good_result: "<exact repr>"
  note: "<why bad is wrong; dialect differences>"
```

DML proofs put the statement and a final `SELECT` in one `;`-separated
query; the compared result is the final `SELECT`. `run_proofs.py` executes
every proof on fresh in-memory connections, three times each, compares
results as exact strings, checks that `good` is stable on `setup_alt`, that
bad and good actually differ, and that no table exceeds 10 rows. It exits
non-zero on any mismatch:

```sh
uv run --quiet --with duckdb --with pyyaml python -I run_proofs.py
```

Verified with DuckDB 1.5.6: 22 proofs, all passing. Records without a proof
are performance or exploration practices where there is no wrong answer
to show; each says so in its `detection_sketch`.

## Top 5 to enforce first

1. `sql/unguarded-update-delete` (error) - a single missing WHERE destroys a
   whole table and reports success. Cheapest catastrophic loss to prevent.
2. `sql/not-in-nullable-subquery` (error) - one NULL in the subquery
   silently empties the whole result.
3. `sql/null-equality-comparison` (error) - `= NULL` / `<> NULL` match
   nothing, ever; zero false positives, and data-quality checks written this
   way pass forever.
4. `sql/count-star-after-left-join` (error) - every zero-activity parent is
   counted as one; the "never ordered" cohort vanishes.
5. `sql/accidental-cross-join` (error) - multiplicative row explosion
   corrupts totals; agents drop join predicates often during refactors.

Next in line: `sql/fanout-join-pre-aggregation` (warning, contract), the
most common silent double-count, which needs declared uniqueness facts.

## Records

Correctness, with proof:

| Record | Severity | What goes wrong |
|---|---|---|
| `sql/accidental-cross-join` | error | Comma join / missing ON multiplies rows |
| `sql/count-star-after-left-join` | error | COUNT(*) counts unmatched parents as 1 |
| `sql/nondeterministic-merge-keys` | error | uuid()/random keys duplicate rows on rerun |
| `sql/not-in-nullable-subquery` | error | One NULL makes NOT IN return nothing |
| `sql/null-equality-comparison` | error | `= NULL` / `<> NULL` are always unknown |
| `sql/set-operation-column-position` | error | UNION pairs columns by position, not name |
| `sql/unguarded-update-delete` | error | UPDATE/DELETE without WHERE hits every row |
| `sql/average-of-ratios` | warning | AVG(rate) is not SUM(num)/SUM(den) |
| `sql/between-date-on-timestamp` | warning | BETWEEN date bounds drop the last day |
| `sql/distinct-dedup-bandaid` | warning | DISTINCT over a fanout merges real duplicates |
| `sql/fanout-join-pre-aggregation` | warning | Many-side join inflates SUM/COUNT |
| `sql/group-by-bare-column` | warning | Arbitrary value per group (MySQL, SQLite only) |
| `sql/insert-explicit-column-list` | warning | Positional INSERT swaps same-typed columns |
| `sql/integer-division-truncation` | warning | int / int truncates (Postgres, SQL Server, SQLite) |
| `sql/keyset-pagination` | warning | OFFSET paging skips/duplicates rows under writes |
| `sql/left-join-null-rejecting-where` | warning | WHERE on nullable side turns LEFT into INNER |
| `sql/limit-requires-order-by` | warning | Unordered LIMIT returns arbitrary rows |
| `sql/natural-join-explicit-keys` | warning | NATURAL JOIN keys change when columns are added |
| `sql/negative-filter-drops-nulls` | warning | `<>`, NOT LIKE, NOT IN (list) drop NULL rows |
| `sql/row-number-dedupe-tiebreaker` | warning | ROW_NUMBER ties pick a different winner |
| `sql/union-all-default` | warning | UNION dedups rows the author meant to keep (intent) |
| `sql/case-without-else-in-aggregate` | advisory | Missing ELSE yields NULL that drops out of sums |

Performance practices (no proof, both forms return the same rows):

| Record | Severity |
|---|---|
| `sql/random-sampling-no-sort` | advisory |
| `sql/sargable-filter-bare-column` | advisory |

Exploration practices (`modes: [explore]`, judgment, no proof):

| Record | Severity |
|---|---|
| `sql/explore-limit-is-not-a-sample` | advisory |
| `sql/explore-profile-filter-columns` | advisory |

The wider explore-mode floor for analysts (state the question and grain,
check row counts at each join, reconcile to a known total) lives in
`consume/explore-*`; the SQL records above cross-reference it with
`see_also`. `sql/wildcard-ctas-view` was folded into
`model/explicit-column-lists`.

## Existing tool coverage and integration

- SQLFluff (layout/style rules) covers formatting and some structure; this
  pack covers data semantics SQLFluff does not check. Co-run; do not
  collide with the TQ rule-code prefix.
- dbt built-in data tests (unique, not_null, accepted_values,
  relationships) and dbt project evaluator cover declared-model testing;
  this pack lints the SQL text before execution, including pre-render Jinja.
- Great Expectations and Soda cover post-execution data validation;
  TrueQuery SQL rules are pre-execution and offline, so they complement
  rather than duplicate: static rules catch the bug class, GX/Soda catch the
  instance.
- Ruff covers the Python layer, not SQL; the sibling `py` pack handles
  Python-for-data. Warehouse EXPLAIN and dry-run (BigQuery dry run,
  Snowflake query profile) are the way to confirm the advisory performance
  rules.
