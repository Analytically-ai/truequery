# /truequery:metric — define a metric once, then keep it honest

When: "define active customer", "what should MRR mean here?", or
checking that a dashboard still matches the house definition. For BI
and analytics engineers. Follows `flow.md`.

## Form 1: `metric "<name>"` — define it

1. **Look**: `truequery context`; search `metrics/*.yaml`, `DATA.md`,
   contracts, the semantic layer (dbt metrics/MetricFlow, LookML) and
   models for an existing definition of this name or a near-synonym.
   If one exists, this is form 2, not a new definition.
2. **Ask** (flow step 2, one call, each with a default): plain-English
   meaning, formula, grain (one value per what?), filters (test
   accounts? deleted rows? which statuses?), currency/unit, time zone
   (default: the project's). Cite the practice each answer follows
   (`sem/single-governed-metric-definition`,
   `sem/metric-grain-declaration`, `sem/metric-filters-declared-not-hidden`,
   `sem/unit-consistency`, `sem/time-dimension-standardized`).
3. **Write it**:
   - When the metric covers one dataset, one column per aggregate, and
     simple filters, write `metrics/<name>.yaml` (`kind: simple|ratio`,
     `dataset`, `measure` or `numerator`/`denominator` with `agg`,
     `column`, `where`, `grain`, `unit`, `owner`), link it from
     `DATA.md` (under `## Metrics`), and run
     `truequery contract validate`.
   - When a headline metric needs a join or an expression across
     columns (such as revenue that excludes deleted customers via a
     join and converts currency), keep the definition in `DATA.md`
     (a `## Metrics` entry: name, meaning, formula, grain, filters,
     unit, owner) instead of `metrics/<name>.yaml`, and state in the
     receipt that it lives in `DATA.md` and is not machine-checked.
   - If the stack has a semantic layer, write it there too (dbt metric
     / MetricFlow / LookML measure) so the governed definition is the
     computed one (`sem/derived-metrics-compose-from-governed`).
4. Add a smoke test or a `truequery`-checkable assertion where the stack
   allows (e.g. the metric over a known fixture period), per
   `sem/metric-smoke-test-on-change`.
5. **Receipt**: the definition as written, where it lives (`metrics/` or
   prose-only in `DATA.md`, and whether it is machine-checked), what was
   assumed, conflicts (an existing different computation is a conflict
   — name it, don't silently replace it).

## Form 2: `metric check <name>` — find the drift

1. Load the governed definition from `metrics/<name>.yaml`, `DATA.md`,
   and the semantic layer.
2. Find candidate computations: grep models, dashboards-as-code and
   SQL for the metric's tables and aggregations; read each candidate.
3. Compare by judgment (this is not a detector): formula, grain,
   filters, unit, time basis. Report each candidate as `matches`,
   `differs: <which clause>`, or `cannot tell: <what's missing>`.
4. Receipt lists every differing computation with file and clause.
   Never claim a match you didn't read.

Output: definition written + receipt, or drift report + receipt.
