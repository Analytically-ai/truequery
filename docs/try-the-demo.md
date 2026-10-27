# Try TrueQuery on the demo shop

This guide sits outside `examples/shop/` on purpose: the agent reads the
files in the project it works on, and the notes below say what a good
answer contains.

A small online shop with the kind of data real teams have: money stored
in minor units across several currencies with daily exchange rates,
legacy status spellings, soft-deleted and test accounts, refunds without
a customer, customer history with several rows per customer, and
timestamp ties. The team's agreed meanings live in
`examples/shop/dbt_repo/models/HOUSE_DEFINITIONS.md`.

## Setup (one command)

From `examples/shop/`:

```bash
./generate.sh        # builds warehouse.duckdb (seed 42; gitignored)
./dq "SHOW TABLES"   # run read-only SQL against it
```

The scripts need Python with duckdb (they use `uv` when it is installed).

Install TrueQuery first (see the repository README). The plugin's hooks
and the agent run the checker that ships inside it. To run `truequery`
yourself in a terminal, `pip install truequery`, or run it without
installing: `uvx truequery detect .`

## Prompts to paste

### Data engineer

`/truequery build an incremental load of the refunds feed into the warehouse`

What good looks like: it inspects the existing staging/marts first,
asks about the load pattern and unique key (or states its defaults),
writes `contracts/refunds.yaml` before code, nets refunds through
`order_id` (not the nullable `customer_id`), ships unique/not-null
tests, runs `truequery review`, and ends with a receipt listing assumptions.

### Analytics engineer

`/truequery build a customer lifetime value mart`

What good looks like: one row per customer (the grain is stated, not
assumed), SCD attributes via the current row only, test/deleted
customers handled per the house definitions, key tests included, and a
receipt that says what was not checked.

`/truequery review dbt_repo/models/`

What good looks like: findings with file:line and a disposition each,
then the receipt — checked levels, not-checked levels, and the
judgment-only practices it applied itself.

### BI engineer

`/truequery metric "active customer"`

What good looks like: it finds the existing definition
(`dim_customers.is_active_customer`, trailing-90-day window anchored at
the warehouse max order day) instead of inventing a new one, writes it
into DATA.md with grain, filters and unit, and names any conflict.

`/truequery metric check active customer`

What good looks like: a drift report — every place that computes
"active" differently, with the differing clause (window, anchor, test
accounts), not just a grep count.

### Analyst

`/truequery analyze why March revenue dropped`

What good looks like: the grain is fixed in words first, the query
uses the revenue definition (completed, non-test, net of processed
refunds, order-day FX), sanity checks run (row counts, NULL rates, a
duplicate-grain probe), and the answer separates what the data shows
from what it cannot.

### Non-technical

`/truequery ask how much money did we make last month?`

What good looks like: a plain number with its period, one sentence on
what it means, how sure TrueQuery is and why, assumptions in everyday
words — and the SQL tucked under a collapsed "How I got this" heading.

### Any role: when a request contradicts the house definitions

Ask for something the definitions rule out, for example an average over
a column stored in cents. TrueQuery says so before writing anything,
follows the definition unless you confirm, and records the choice.
