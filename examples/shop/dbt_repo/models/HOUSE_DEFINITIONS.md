# House Definitions (10 canonical rules)

These are the ONLY sanctioned meanings. Every mart model cites the D-number
it implements. When raw data disagrees with a definition, the definition wins.

- **D1 — Revenue**: sum of `net_usd` over revenue-eligible orders, where
  eligible = canonical status `completed`, non-test customer, at least one
  succeeded payment; net = gross minus `processed` refunds only; reported in
  USD. (`fct_orders`, `fct_revenue_daily`)
- **D2 — Active customer**: a non-test, non-deleted customer with ≥1 completed
  order in the trailing 90 days, anchored at the warehouse max order day
  (not at each customer's own last order). (`dim_customers.is_active_customer`)
- **D3 — Churn**: a non-test customer (or subscription) active in the prior
  90-day window with zero activity in the trailing 90 days (same anchor);
  for subscriptions the churn month is the month containing `ended_at`.
  (`dim_customers`, `fct_subscription_mrr`)
- **D4 — Order date**: `ordered_at` truncated to UTC day. `created_at` and
  `updated_at` are write times and must never grain revenue or cohorts.
- **D5 — Soft deletes**: any row with `deleted_at` set is excluded from
  headline metrics; it may appear only in explicitly labeled audit/reconciliation
  outputs (e.g. `non_eligible_refunded_usd`, `test_mrr_usd`).
- **D6 — Status normalization**: legacy values fold case-insensitively —
  `complete`/`COMPLETE`/`done` → `completed`; `canceled` → `cancelled`;
  `success`/`SUCCEEDED` → `succeeded`. Unknown values map to `other`, never
  silently dropped.
- **D7 — Money & currency**: all `*_cents` columns are integer minor
  units; divide by the currency's minor-unit factor only at conversion —
  100 for USD/EUR/GBP, **1 for zero-decimal JPY (whole yen)** — then
  multiply by `fx_rates.rate_to_usd` joined on the BUSINESS date (order
  day / spend date / month start), never `today()`.
- **D8 — Customer SCD**: `customers_history` holds >1 row per customer; the
  live attribute source is the single `is_current` row. Joins on customer
  attributes must go through `stg_customers`, never the raw history.
- **D9 — Refund attribution**: refunds join to metrics through `order_id`
  because `refunds.customer_id` is nullable (~1/3 are anonymous). Anonymous
  processed refunds still reduce order revenue.
- **D10 — Timestamp ties**: `ordered_at` ties are real (batch checkouts).
  Any "latest order" or ordered output must break ties deterministically with
  `order_id`; `row_number() over (partition by ordered_at order by order_id)`
  is the standard pattern.
