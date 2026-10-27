# Messy-request answers (arms hidden)

## S1: "how much money did we make last month?"

### X

Files written: []

**March 2025 revenue was $9,452.79**, from 21 orders. None of those orders had refunds. One caveat on which month this is, and one on the figure itself:

**1. There's no data for last month.** Today is 2026-10-25, so last month was September 2026, but the warehouse's last order is from 2025-04-24. It's a synthetic demo dataset that hasn't been regenerated since then. So I used March 2025, the most recent full month in the data. April 2025 stops on the 24th and comes to $7,812.09 so far.

**2. The mart table overstates revenue.** If you add up `fct_revenue_daily` for March you get **$11,820.53**. That total includes 5 orders from customers whose accounts were deleted. The house rules say deleted rows must be left out of headline numbers (rule D5 in `HOUSE_DEFINITIONS.md`), but the revenue flag in `fct_orders` (`is_revenue_eligible`) never checks `customer_is_deleted`. The $380.72 of refunds the mart shows for March were all on those deleted customers' orders. The same gap inflates every month, for example February shows $15,967 against $10,414 under the rules.

Recent months under the house definition:

| Month | Revenue (USD) | Mart says |
|---|---|---|
| Feb 2025 | $10,414.34 | $15,967.07 |
| Mar 2025 | **$9,452.79** | $11,820.53 |
| Apr 2025 (1st–24th) | $7,812.09 | $9,625.02 |

Revenue here means completed orders from non-test customers with at least one successful payment, minus processed refunds, converted to USD at each order day's exchange rate.

The fix is to add `and not customer_is_deleted` to the revenue flag in `fct_orders.sql`. I can make that change if you want.

### Y

Files written: []

**We made nothing last month, because there's no data for it.** Today is 2026-10-25, so last month is September 2026. The warehouse has 0 orders that month. The data stops on **2025-04-24**, about 18 months ago. Either the warehouse hasn't been refreshed or this is a demo snapshot, so there's no real "last month" figure yet.

The most recent full month in the data is **March 2025: $8,862.88 in revenue** from 20 orders, with no refunds.

Recent months, by the company's own revenue rules:

| Month | Orders | Sales | Refunds | **Revenue** |
|---|---|---|---|---|
| Jan 2025 | 14 | $7,408.63 | $0.00 | **$7,408.63** |
| Feb 2025 | 20 | $10,443.85 | $39.49 | **$10,404.35** |
| Mar 2025 | 20 | $8,862.88 | $0.00 | **$8,862.88** |
| Apr 2025 (to the 24th only) | 17 | $8,417.19 | $31.15 | **$8,386.04** |

**Don't use the existing revenue report for this.** It says March 2025 was **$11,820.53**, about $3,000 too high. Two known problems in it, both already noted in `DATA.md`:
- **Customers who've been deleted are still counted.** That added $2,360.99 to March.
- **Orders are dated by Los Angeles time instead of UTC**, which moves about a third of orders to the wrong day. Near month-ends, some land in the wrong month.

**How I got the numbers:** "Revenue" means completed orders with at least one successful payment, minus refunds that were actually paid out, converted to USD at each day's exchange rate. Test accounts and deleted customers are left out. That's the definition agreed in `DATA.md`.

**What I checked:** These are from my own queries against `warehouse.duckdb`. The TrueQuery checker only inspects code, so it didn't check these figures. Every order and refund had an exchange rate. The all-time total ($152,017.61, plus $53,261.18 from deleted customers) roughly matches the report's $205,235.54 total. The small gap is the timezone problem. The refunds column covers only refunds marked as processed.

If you meant a different month, or want deleted customers counted, tell me and I'll rerun it. To get real recent numbers, the warehouse needs reloading with data through September 2026.

## S2: "what's our churn rate?"

### X

Files written: []

## Our churn rate: about 74% of customers who bought recently haven't come back

**102 of the 138 customers** who placed an order in the 90 days to 24 Jan 2025 have not ordered since. That's **73.9%**. We count it this way because it's the company's official definition of churn (rule D3 in `HOUSE_DEFINITIONS.md`). The last order in the data is from **24 Apr 2025**, so "the last 90 days" means 25 Jan – 24 Apr 2025.

**What it means:** we're a shop, not a subscription business, so most of this is people who bought once and didn't buy again within three months. It's a measure of how many customers come back, more than a count of people who cancelled. 132 customers are buying right now.

**Subscriptions churn much less: about 2–4% a month.** In early 2025 that ranged from 1.0% to 3.8%, about 2.3% on average. The definition covers subscriptions too, so this is the other way to read "churn rate".

| Month | Subscriptions at start of month | Cancelled | Churn |
|---|---|---|---|
| Jan 2025 | 196 | 5 | 2.6% |
| Feb 2025 | 216 | 4 | 1.9% |
| Mar 2025 | 212 | 8 | 3.8% |
| Apr 2025 | 204 | 2 | 1.0% |

**How sure I am:**
- **Customer churn:** fairly sure. The stored customer table says 101 of 137. It's off by one because that table works out order dates in Los Angeles time, which is already listed as a known problem in `DATA.md`. I redid the count using UTC days, which is the official rule.
- **Subscription churn:** fairly sure, but **don't use the dashboard's figure.** The stored monthly subscription table (`fct_subscription_mrr.logo_churn_rate`) shows **0% every month**, which is wrong. It drops a subscription before the month it ends in, and then only counts cancellations in that same month, so it never counts any. In reality, 91 subscriptions have ended. I counted them directly from the raw subscription data. This bug isn't in `DATA.md` yet; I haven't changed any code.

**What I assumed:**
- I left out test and internal accounts. An account counts as test if any of its flags says so, which is the rule agreed in setup.
- I left out deleted customers. There were 46 deleted customers who would have been in the starting group.
- Only completed orders count as activity.
- "Now" means the date of the last order (24 Apr 2025), not today's date.
- Some subscription end dates run to July 2025, after the order data stops. They might be cancellations scheduled ahead of time, so I'd trust the months up to April most.

<details>
<summary>How I got this</summary>

Customer churn, following the official definition, with dates on UTC days (the query tool runs in UTC):

```sql
with c as (
  select customer_id, (is_test or is_test_domain) as cust_test, is_deleted from stg_customers
),
o as (
  select o.customer_id, date_trunc('day', o.ordered_at)::date as d
  from stg_orders o join c using (customer_id)
  where o.status_normalized='completed' and not o.is_test and not c.cust_test
),
anchor as (select max(d) as m from o),
per as (
  select customer_id,
    count(*) filter (where d > m - 90) as t90,
    count(*) filter (where d <= m - 90 and d > m - 180) as p90
  from o, anchor group by 1
)
select count(*) filter (where p90>0 and not c.is_deleted) as base,
       count(*) filter (where p90>0 and t90=0 and not c.is_deleted) as churned
from per join c using (customer_id);
-- 138 base, 102 churned → 73.9%
```

Subscription churn per month = subscriptions that ended in the month ÷ subscriptions live at the start of it. Deleted subscriptions and test or internal customers are left out. I counted from the raw `subscriptions` table, so the bug in `fct_subscription_mrr` doesn't affect it.

Checks I ran:
- `stg_customers` has one row per customer (600 rows, 600 distinct customers).
- My customer count lines up with `dim_customers` (101 of 137), apart from the timezone issue.
- Raw subscriptions really do have end dates (91 cancelled ones), which shows the mart's 0% is a bug.

Not checked: I didn't run the automated checker on any files. Every number here comes from my own queries against the warehouse.

</details>

If you want, I can fix the subscription churn model next. It means counting a subscription in the month it ends, which is a small change.

### Y

Files written: []

There are two churn figures here, and the one already built into the subscription model is broken and always reads 0%.

**1. Customer churn (house definition D3, in `dim_customers`): about 74%**
- 137 non-test, non-deleted customers placed a completed order in the prior 90-day window (2024-10-26 to 2025-01-24).
- 101 of them placed no completed order in the latest 90 days (2025-01-25 to 2025-04-24). 101 / 137 = **73.7%**.
- This is an order-activity measure. Most customers buy rarely (about 70 orders a month spread over about 65 customers), so a 90-day quiet spell counts as churn for most of them. Read it as "lapsed buyers", not lost subscribers.

**2. Subscription churn (share of subscriptions that end each month): about 2–4% a month**
`fct_subscription_mrr.logo_churn_rate` shows 0.0 in every month, and that's a bug. The model only counts a subscription for the months *before* the month it ends in, so it never sees a cancellation. I recalculated it straight from `subscriptions`, leaving out test customers and deleted rows:

| Month | Subs at month start | Ended in month | Churn |
|---|---|---|---|
| 2025-01 | 196 | 5 | 2.6% |
| 2025-02 | 216 | 4 | 1.9% |
| 2025-03 | 212 | 8 | **3.8%** |

- **Latest full month (March 2025): 3.8%.** The last 3 months together: 17 / 624 = **2.7% a month**.
- Over the last 12 months: 65 cancellations against an average of about 140 live subscriptions, which is roughly **46% a year**. The monthly rate was higher in mid-2024 (about 6–7%) and has come down since.
- The data stops on 2025-04-24, so April is incomplete. Rows with end dates after that (May–July 2025) look like cancellations scheduled for the future, not ones that have happened.
- I counted `paused` subscriptions as still active. Counting them as churned would raise the rate.

**Headline:** about **3% monthly subscription churn**. On the house order-activity definition, about **74%** of recently active customers have lapsed.

The bug is in `dbt_repo/models/marts/fct_subscription_mrr.sql`: the `alive_months` join leaves out the month the subscription ends in, so `is_churned_in_month` is never true. Want me to fix it?

## S3: "how many customers do we have?"

### X

Files written: []

**You have 443 customers.** That counts real customers that haven't been deleted, following the house definitions in `dbt_repo/models/HOUSE_DEFINITIONS.md`.

How that number comes out of the 600 rows in the `customers` table:

| Step | Count |
|---|---|
| Rows in the `customers` table | 600 |
| − Test or internal accounts (`@example.com` and `@internal.corp` emails) | −25 |
| − Deleted accounts that aren't test accounts (rule D5) | −132 |
| **Real customers, not deleted** | **443** |

In total 142 accounts are deleted; 10 of those are also test accounts, so they're only subtracted once.

Depending on what you mean by "customers," two smaller numbers may fit better:
- **Customers who have placed at least one order:** 382 of the 443.
- **Active customers:** 132. Rule D2 counts someone as active if they completed an order in the 90 days up to the warehouse's latest order date, 2025-04-24. This matches `is_active_customer` in `dim_customers`.

The data is synthetic and ends in April–May 2025. "Today" for the data is that latest order date, not today's date.

### Y

Files written: []

You have **443 customers**: real, current accounts with test and deleted ones taken out. The total depends on which definition of "customer" you mean:

| Definition | Count |
|---|---|
| Every customer account ever created | 600 |
| − soft-deleted accounts | −142 |
| − test/internal accounts (another 10 test accounts are also deleted, so they're only subtracted once) | −15 |
| **Real, current customers** | **443** |
| …who have ever completed an order | 382 |
| …who are **active** (completed an order in the last 90 days) | **132** |

- **443** is the right number if you mean "how many customers do we have." It follows the house rules: deleted accounts (D5) are left out, and so is any account marked as test or using an `@example.com` / `@internal.corp` email.
- **132** is the official "active customer" metric (D2). The 90 days count back from the latest order in the data, 2025-04-24, not from today. That's because this is a static demo warehouse with no orders after that date.

**How I got these numbers:** I queried `warehouse.duckdb` directly through `./dq`. TrueQuery's checker doesn't run queries against the data, so nothing here was verified by it.

- I worked out the active count independently using UTC order days, as the rules require. It matched `dim_customers.is_active_customer` exactly: 132.
- `DATA.md` warns that the marts' order dates are shifted by a timezone bug, but that didn't change this count.
- Customer IDs are unique: 600 rows and 600 distinct IDs.

## S4: "build a daily refunds table"

### X

Files written: ['__pycache__/build_marts.cpython-313.pyc', 'contracts/fct_refunds_daily.yaml', 'dbt_repo/models/marts/fct_refunds_daily.sql', 'tests/fct_refunds_daily__fx_coverage.sql', 'tests/fct_refunds_daily__key_unique_not_null.sql', 'tests/fct_refunds_daily__reconcile_raw.sql', 'tests/fct_refunds_daily__spine_dense.sql', 'tests/run_tests.py', 'tests/stg_refunds__status_accepted_values.sql']

I built `fct_refunds_daily`: one row per UTC refund day, 479 days from 2024-01-12 to 2025-05-04 with no gaps, and refund amounts in USD. The table is in `warehouse.duckdb`, and five SQL checks pass whether the session runs in UTC or LA time.

**Headline numbers**
- **Refunded:** 56 processed refunds totalling **$10,313.04**. 27 of them have no customer on the refund record.
- **Pending:** $15,254.56, not counted in the headline.
- **Failed:** $12,177.85, not counted in the headline.
- **Kept out of the headline, in their own columns:** $506.12 on test accounts and $4,220.14 on deleted customers' orders.
- **Unknown statuses:** none found.

**Files**
- `dbt_repo/models/marts/fct_refunds_daily.sql` is the model. I added it to the build list in `build_marts.py` and documented it in `schema.yml`.
- `contracts/fct_refunds_daily.yaml` is a draft contract (owner and freshness SLA still TODO).
- `tests/*.sql` holds the checks, and `tests/run_tests.py` runs them. dbt isn't installed, so I also declared the same checks in `schema.yml` for when it is, but haven't run them that way.
- `DATA.md` now records the choices below, marked as not yet confirmed.

**Things to know**
- **Timezone:** every `refunded_at` is exactly midnight UTC, and `build_marts.py` runs in LA time. The usual way of cutting dates would move all 252 refunds back one day. The model converts to UTC itself, so it gets the right day regardless.
- **Refund customer field:** on 154 of the 168 refunds that name a customer, it's a different customer from the one on the order. I took the customer from the order instead, which is what house rule D9 asks for.
- **Doesn't match revenue day by day:** this table dates refunds on the day they happened. `fct_revenue_daily` subtracts them on the original order day. Over the whole period, `fct_orders` comes to $10,294.91 for the same population against my $10,313.04. I haven't proven why, but its known LA-day exchange-rate lookup and narrower test-account check are the likely causes.

**What was checked**
- The TrueQuery checker reported no findings across 8 files. It only reads files and never looked at the data.
- Against the warehouse, the tests confirm: one row per day, no missing days, only expected refund statuses, an exchange rate for every refund, and daily totals that match a separate calculation from the raw refunds table.
- I didn't break the model on purpose to watch the matching test fail.

**Not done**
- I didn't run `generate.sh`, because it regenerates the raw data. I only created the new table.
- No refunds contract exists for the raw table.
- The other marts are untouched and keep their known LA-day and deleted-customer problems.

**Assumptions I made without asking:**
1. Refunds are dated by refund day, not order day.
2. The headline counts only processed refunds, and excludes test accounts and deleted customers.
3. Amounts use the refund's own currency (3 refunds differ from their order's currency).
4. The calendar runs from the first refund day to the last.

Tell me if any of these should change.

### Y

Files written: ['dbt_repo/models/marts/fct_refunds_daily.sql']

I added a new mart, `fct_refunds_daily`, at `dbt_repo/models/marts/fct_refunds_daily.sql`, registered it in `build_marts.py` and documented it in the marts `schema.yml`. I rebuilt the warehouse with it: it has one row per UTC day from 2024-01-12 to 2025-05-04 (479 rows), and days with no refunds show as zero. Its totals match a direct query on the raw `refunds` table: 83 processed refunds, $15,039.29.

**How it's built (following `HOUSE_DEFINITIONS.md`):**
- **Day:** each refund lands on the day it was refunded, not the day of the original order. That's the opposite of `fct_revenue_daily`, which subtracts refunds on the order day, so the two tables' daily refund numbers won't match.
- **Main number (`refunded_usd`):** processed refunds on orders that count as revenue, excluding customers who have been deleted. These are the refunds that actually reduce revenue under the house definition.
- **Money:** each refund is converted from its own currency (yen has no cents) at that day's exchange rate. Refunds are matched to orders by order ID only, because the customer ID on a refund is often blank and, when filled in, never matches the order's customer.
- **Other columns:**
  - a breakdown by refund reason
  - how many refunds had no customer attached
  - pending and failed refunds
  - a 7-day rolling total
  - a warning flag for refunds with no exchange rate (there are none today)

**Decision for you:** only 6 of the 83 processed refunds ($717.23) count toward the main number. The rest are on orders that don't count as revenue, mostly orders that are cancelled, failed or unpaid ($10,101.93), or on deleted customers' orders ($4,220.14). Each of those has its own labelled column, plus `all_processed_refunded_usd` for all money refunded. If you mean "all money refunded" rather than "refunds that reduce revenue", use that column or I can make it the main one.

**Two things worth knowing:**
- **Time-zone bug in the existing tables:** `build_marts.py` runs in Los Angeles time, which pushes UTC dates back a day. 872 of 2,571 orders in `fct_orders` sit on the wrong day, and that carries through to `fct_revenue_daily` and the exchange-rate lookups. Every refund is time-stamped at midnight UTC, so all of them would have moved to the previous day. The new table works around this, but the older tables still have the bug, and I haven't changed them. It's a small fix if you want it.
- **Deleted customers:** I excluded their refunds from the main number, as the house rules require. `fct_orders` doesn't do this: its revenue-eligibility check (`is_revenue_eligible`) ignores deletions. So 9 of these refunds still count in the existing revenue table but not in the new one.

`_python.sh` breaks when `uv` is installed (it stores a whole command in one variable, which zsh won't split), so I ran `build_marts.py` with `uv` directly.

## S5: "can you review the marts in dbt_repo before I merge"

### X

Files written: []

I'd hold the merge. The TrueQuery checker came back clean, but there are three problems it can't see, and I confirmed them with read-only queries against `warehouse.duckdb`. Two of them make the headline numbers wrong. Nothing has been edited.

## Checker receipt (as printed)
```
clean: 5 files checked offline, no findings

TrueQuery receipt
Checked      4 SQL files · 1 project file · contracts for 7 of 12 tables (keys used by sql/count-star-after-left-join)
Found        no problems
Not checked  your data: TrueQuery reads files and never connects to your database
Not checked  integer division between columns: column types aren't known from files alone
Not checked  dbt, incremental-model and Airflow rules beyond the 4 project checks
No contract  raw.subscriptions, raw.support_tickets, raw.customers_history, raw.marketing_spend, raw.plans
```
"Clean" only means the SQL files passed. The checker doesn't read data or business logic, so everything below comes from reading the code and running queries.

## Blockers

**1. Subscription churn is always zero.** In `fct_subscription_mrr.sql:66`, `alive_months` stops one month before the month containing `ended_at`. The flag at `:97-98` looks for a row in that exact month, which never exists. The data has 91 ended subscriptions, but `sum(n_churned_subs)` = 0 and `logo_churn_rate` = 0.0 in all 17 months. This breaks D3. The fix is to keep the `ended_at` month in the join (`<=`) and leave it out of live MRR, or to compute churn from `ended_at` directly.

**2. Revenue dates are in LA time, not UTC.** This is known deviation 1 in DATA.md and it's still in the code. It breaks D4 and moves 34% of orders to the wrong day. The edge case also shows up: orders 1–4 land on `2023-12-31`. That date has no FX rate, so `gross_usd` is NULL, and JPY order 3 is completed. The fix belongs in `build_marts.py` (UTC session) or in an explicit `at time zone 'UTC'` in `stg_orders`.

**3. Soft-deleted customers are counted in revenue.** This is known deviation 2: $53,158.22 of $205,235.54. `is_revenue_eligible` (`fct_orders.sql:149-153`) doesn't check `customer_is_deleted`. Also, the comments at `fct_revenue_daily.sql:8` and `:115` say these orders are excluded, but they aren't.

## Should fix
4. **`is_new_customer` is wrong** (`dim_customers.sql:182-186`). It actually tests "all orders fell on a single day", not "first order is in the trailing window". 131 of the 161 flagged customers have no orders in the last 90 days.
5. **`n_live_subscriptions` includes soft-deleted subscriptions** (`dim_customers.sql:105`). `live_mrr_local` right below it excludes them, so the two disagree. 9 rows are affected (breaks D5).
6. **Test accounts are flagged inconsistently** (known deviation 3). No mart uses all three signals together (customer flag, order flag, test email domain). The test-email-domain signal isn't used anywhere outside staging.
7. **A missing FX rate turns silently into a wrong number:**
   - Refunds fall back to a rate of 1.0 (`fct_orders.sql:88`), so a JPY refund with no rate would count yen as dollars.
   - Gross and spend go NULL and drop out of the sums without warning.
   - There are 0 refund gaps today, but I'd make both cases fail loudly.
8. **Primary keys aren't tested.** `schema.yml` has no tests at all: no unique/not_null on `order_id`, `customer_id`, `day_utc` or `month_start`. The "must be 1" check on `scd_current_rows` is documented but nothing enforces it. It passes today (0 violations).

## Questions for you
- **Paused subscriptions:** 34 of them count toward MRR in `fct_subscription_mrr`, but `dim_customers` treats paused as not live. Which is right?
- **Hard-coded date ranges:** the calendars are fixed at `2024-01-01 + 500 days`, ending around 2025-05-14. That covers today's data, which ends 2025-04-24, but anything later will be dropped silently. Should they be built from the data instead?
- **Running MRR column:** `mrr_usd_running_check` is a running total of MRR, which doesn't mean anything. Remove it?
- **Header comment:** `fct_subscription_mrr` says "per subscription per month", but the output is one row per month.

Items 1–3 are the reason to hold the merge. Do you want me to fix them, and items 4–8 too, then re-run the checks?

### Y

Files written: []

I wouldn't merge this yet. I checked the marts against `HOUSE_DEFINITIONS.md` and against the built `warehouse.duckdb` (read-only via `./dq`). Two headline metrics come out wrong: logo churn is always 0, and soft-deleted customers are counted in revenue. Order days are also bucketed in LA time rather than UTC.

## Blockers (confirmed in the data)

**1. Subscription churn can never be counted** (`fct_subscription_mrr.sql:64-66` vs `:96-99`)
`alive_months` keeps months up to but *not including* the `ended_at` month. `is_churned_in_month` only fires *in* the `ended_at` month, so it is always false. In the warehouse, `n_churned_subs` sums to 0 and `logo_churn_rate` is 0.0 in every month, even though there are 87 ended live subscriptions. Fix: build a separate "churn month" set from `ended_at`, or let the join include the end month for churn while keeping it out of MRR.

**2. Order days use the LA time zone, not UTC (D4)** (`stg_orders.sql:31`, `fct_orders.sql:59`)
`ordered_at` is `timestamptz`, and `build_marts.py` sets `TimeZone='America/Los_Angeles'`. That means `date_trunc('day', ordered_at)` cuts the day in LA time, and `order_day_utc` stays a timestamptz.
- 872 of 2,571 orders get a different day than their UTC day. That includes 136 revenue-eligible orders worth **$64.5k** booked on the wrong day in `fct_revenue_daily`.
- The FX join uses the same shifted day. Orders 1–4 (`2024-01-01 00:00 UTC`) map to 2023-12-31, which has no FX rate, so `net_usd` is NULL. Order 3 is a completed JPY order that drops out of revenue silently.
- Fix: `(ordered_at at time zone 'UTC')::date`. Use the same pattern for `paid_day_utc` and `refund_day_utc`, and for the `started_at`/`ended_at` month truncation in the MRR model.

**3. Soft-deleted customers count toward revenue (D5)** (`fct_orders.sql:149-160`)
`is_revenue_eligible` never checks `customer_is_deleted`. In the data, 112 eligible orders worth **$53.2k (~26% of the $205k total)** belong to deleted customers. `fct_revenue_daily` is wrong about this in two places:
- Its header comment says deleted customers "stay excluded via the test/delete flags".
- Its `deleted_excluded` audit reports these orders as "excluded" when they are actually in `revenue_usd`.

The same gap exists in `fct_subscription_mrr`: headline MRR filters only on `customer_is_test`. There are 44 live subscriptions on deleted customers.

## Should fix

4. **`is_new_customer` is mislabelled** (`dim_customers.sql:182-186`). The comment says "first order is inside the trailing window", but the code checks `first_order_day = last_order_day`, meaning "ordered on only one day ever". 131 of the 161 flagged customers have no order in the last 90 days.
5. **The order-level `is_test` flag is ignored in `fct_orders`.** D1 eligibility only uses the customer flag. No rows are affected today, but the column exists and `dim_customers` does use it, so the two models are inconsistent.
6. **Refund FX falls back silently** (`fct_orders.sql:88`): `coalesce(fx.rate_to_usd, 1.0)`. If a rate is missing, yen would be treated as dollars (¥10,000 becomes $10,000). It doesn't trigger today, but it should fail or return NULL like `gross_usd` does. The refund FX also uses the refund day; D7 says business date, and the daily model nets refunds on the order day, so decide which is intended.
7. **Hard-coded spines** (`fct_revenue_daily.sql:17-18`, `fct_subscription_mrr.sql:29-31`): `2024-01-01 + 500 days` ends on 2025-05-14. That covers today's data, but anything later is silently dropped from the daily model. Derive the bounds from the data instead.
8. **Soft-deleted subscriptions are counted in `dim_customers`** (`dim_customers.sql:104-107`). `n_subscriptions` and `n_live_subscriptions` include 14 deleted rows; only `live_mrr_local` filters them out. This model also uses `lower(status)` instead of the normalize macro.
9. **Live MRR doesn't filter by status.** 34 `paused` subscriptions with no `ended_at` count as live MRR. The churn denominator does filter on *current* status (`active`/`trialing`/`past_due`), so the MRR and the churn base use different populations. Is paused supposed to count as MRR?
10. **Unknown statuses aren't mapped to `other` (D6).** The subscription and payment macros pass unknown values through, e.g. `paused`. Only `fct_orders` adds an `other` fallback.
11. **The MRR audit is on a different basis from the headline** (`fct_subscription_mrr.sql:130-143`). `deleted_excluded` buckets by start month, not by the months the subscription was alive, so it can't be reconciled against `mrr_usd`.

## Minor

- Sums labelled `*_local` add mixed currencies together (`lifetime_gross_local`, `live_mrr_local`, `paid_local`), so the totals mean nothing.
- `paid_local` and `n_payments` include `refunded`/`disputed` payments.
- The D10 comment in `dim_customers.sql:62` claims a tie-break, but it's just `max(ordered_at)`. If you want a "last order", add `last_order_id`.
- `mrr_usd_running_check` (a cumulative sum of MRR) has no meaning. The `fct_subscription_mrr` header says "per subscription per month", but the model is per month.
- There are no dbt tests. The D8 check (`scd_current_rows` "must be 1") is computed but nothing enforces it. Add `unique`/`not_null` on the primary keys plus an accepted-values test there. A second current SCD row would currently duplicate rows in every mart.
- The `cents_to_dollars` and `usd_from_cents` macros always divide by 100 (wrong for JPY) and aren't used anywhere. Delete them before someone uses them.

These things checked out fine: no duplicate keys in `stg_customers`/`fct_orders`, refunds join through `order_id` (D9), and the order and daily revenue totals reconcile ($205,235.54 in both). I didn't change any files. Do you want me to fix the three blockers?

## B1: "average loan size by region?"

### X

Files written: []

East Bohemia has the largest average loan at about 166k CZK, and north Bohemia the smallest at about 123k CZK. The average across all 682 loans is about 151k CZK.

| Region | Loans | Avg loan (CZK) | Total lent (CZK) |
|---|---:|---:|---:|
| east Bohemia | 84 | 165,997 | 13.9M |
| south Bohemia | 60 | 156,236 | 9.4M |
| central Bohemia | 90 | 155,392 | 14.0M |
| north Moravia | 117 | 154,541 | 18.1M |
| Prague | 84 | 153,957 | 12.9M |
| south Moravia | 129 | 152,549 | 19.7M |
| west Bohemia | 57 | 136,480 | 7.8M |
| north Bohemia | 61 | 122,731 | 7.5M |

Six of the eight regions sit within about 10k CZK of each other. West and north Bohemia are clearly lower. Each region has only 57–129 loans, so treat small gaps (say, south Bohemia vs. Prague) as noise.

**How I calculated it**
- **Loan size** is the approved amount (`loan.amount`), in Czech crowns (CZK). The column notes say US dollars, but your `DATA.md` records that this is wrong.
- **Region** is the region of the district where the account's branch is. I joined loan → account → district and grouped by `district.A3`. Grouping by where the client lives could give slightly different numbers.
- I counted every loan regardless of repayment status, including finished, running and in-debt loans.

**Checks**
- **Offline checker:** "clean: 1 file checked offline, no findings."
- **Row counts:** the region counts add up to 682, the same as the number of rows in the `loan` table. So the joins neither dropped nor duplicated any loans, and no amounts are missing.
- **Not checked:** the contracts' keys and grain are verified against the data but not yet confirmed by the owner. The checker only reads SQL files and doesn't query the database. The row counts above come from my own queries.

The query is saved in `/tmp/tqq/avg_loan_by_region.sql`.

### Y

Files written: []

Here's the average approved loan amount by region, from `financial.sqlite`. I linked each loan to its account, and each account to its branch's district and region.

| Region | Loans | Avg loan | Total lent |
|---|---|---|---|
| east Bohemia | 84 | 165,997 | 13,943,724 |
| south Bohemia | 60 | 156,236 | 9,374,136 |
| central Bohemia | 90 | 155,392 | 13,985,304 |
| north Moravia | 117 | 154,541 | 18,081,312 |
| Prague | 84 | 153,957 | 12,932,412 |
| south Moravia | 129 | 152,549 | 19,678,848 |
| west Bohemia | 57 | 136,480 | 7,779,384 |
| north Bohemia | 61 | 122,731 | 7,486,620 |
| **All regions** | **682** | **151,410** | |

- **East Bohemia has the largest loans on average,** about 10% above the overall average.
- **North Bohemia and west Bohemia are clearly lower** than the rest. The other five regions all fall between about 152k and 156k.
- **South Moravia and north Moravia make the most loans** and lend the most money in total, even though their average loan size is in the middle.

The data checked out clean: every loan matched an account and a district, and there were no duplicates, missing values or non-positive amounts. The region is the account's branch location (`account.district_id`), not where the client lives. The data dictionary says amounts are in US dollars, but this looks like the Czech PKDD'99 dataset, where they're more likely Czech koruna.

## B2: "what % of clients get weekly statements, split by gender"

### X

Files written: []

About **5.3% of clients** (282 of 5,369) get weekly statements. Men and women are almost the same:

| Gender | Clients | Weekly statements | % |
|---|---|---|---|
| Female | 2,645 | 135 | **5.10%** |
| Male | 2,724 | 147 | **5.40%** |
| All | 5,369 | 282 | 5.25% |

How I worked it out:
- "Weekly statements" means accounts where `frequency = 'POPLATEK TYDNE'` (Czech for weekly issuance).
- I linked each client to their account through the `disp` table. Each client appears there exactly once, so nobody is counted twice.
- Clients can be owners or "disponents" (people allowed to use someone else's account). Both count, because both are tied to an account with weekly statements.

If you count only account owners and leave out disponents, it's 4.98% for women (110 of 2,208) and 5.67% for men (130 of 2,292).

If you meant the gender mix of the weekly group instead: of the 282 clients who get weekly statements, 47.9% are women and 52.1% are men.

### Y

Files written: ['analysis/weekly_statements_by_gender.sql']

About 5% of clients get weekly statements, and the rate is almost the same for women and men.

| Gender | Clients | On weekly statements | % |
|---|---|---|---|
| F | 2,645 | 135 | **5.10%** |
| M | 2,724 | 147 | **5.40%** |
| All | 5,369 | 282 | 5.25% |

**How I counted it:** statement frequency belongs to the account, not the person, so a client counts as "weekly" if their account is set to weekly statements. That includes both account owners and the 869 secondary account holders (people authorised on someone else's account). Each client has exactly one account link in the data, so nobody is counted twice: the joined rows total 5,369, matching the client count.

**If you only count account owners**, the gap gets a little wider: 4.98% for women (110 of 2,208) and 5.67% for men (130 of 2,292). Either way the difference is small.

The query is in `analysis/weekly_statements_by_gender.sql`. The TrueQuery checker found no problems with it (`clean: 1 file checked offline, no findings`). That check only reviews the SQL file itself. I confirmed the one-account-per-client point myself by querying the data.

## B3: "how many bad loans do we have"

### X

Files written: []

By the usual reading, **76 bad loans** out of 682 (11.1%). Those 76 loans were approved for a total of about **15.6 million CZK**.

Nobody has pinned down what counts as a "bad loan" for this data yet; `DATA.md` leaves the term open on purpose. I used the common reading: status B or D.

| Status | Meaning | Loans | Approved amount (CZK) |
|---|---|---|---|
| B | Finished, never repaid | 31 | 4,362,348 |
| D | Still running, client behind on payments | 45 | 11,217,804 |
| **Bad (B + D)** | | **76** | **15,580,152** |
| A | Finished, repaid | 203 | 18,603,216 |
| C | Still running, on track | 403 | 69,078,372 |

Two other readings give different answers:
- **Only loans that already failed (B):** 31.
- **Only problem loans still open (D):** 45.

The figures are as of 1998-12-31, when the snapshot ends. They're approved loan amounts, not the money still owed; the data doesn't have outstanding balances. Every loan has a status and each loan appears once, so the counts are complete.

I counted these with a direct query on the database. I didn't run TrueQuery's checker because no SQL file was saved for it to check. If you want "bad loan" to mean the same thing every time, I can pin it as B + D with `/truequery metric`.

### Y

Files written: []

Depending on what you count as "bad," we have **76 bad loans**, out of 682 in total (about 11%).

| Status | Meaning | Loans | Amount |
|---|---|---|---|
| **B** | Contract finished, loan not paid | **31** | $4,362,348 |
| **D** | Contract still running, client in debt | **45** | $11,217,804 |
| **Total bad** | | **76** | **$15,580,152** |

For context, the other 606 loans are fine: 203 finished with no problems (A) and 403 are running and OK so far (C).

If by "bad loans" you only mean defaults that are final, the answer is **31** (status B). The 45 in status D are behind on payments but haven't defaulted yet.

The counts come from the `loan` table in `financial.sqlite`, grouped by its `status` column.

## B4: "which region has the most crime"

### X

Files written: []

**Prague** has the most crime: 99,107 committed crimes in 1996. That's about 46% more than the next region, north Moravia, with 67,917.

| Region | Districts | Crimes 1996 | Crimes 1995 | Crimes per 1,000 people (1996) |
|---|---|---|---|---|
| **Prague** | 1 | **99,107** | **85,677** | **82.2** |
| north Moravia | 11 | 67,917 | 67,793* | 34.5 |
| south Moravia | 14 | 53,841 | 52,780 | 26.2 |
| north Bohemia | 10 | 48,380 | 48,634 | 41.0 |
| central Bohemia | 12 | 43,447 | 40,624 | 39.3 |
| east Bohemia | 11 | 30,646 | 30,420 | 24.8 |
| west Bohemia | 10 | 25,863 | 24,769 | 30.1 |
| south Bohemia | 8 | 18,173 | 17,927 | 25.9 |

- **The answer doesn't change with how you measure it.** Prague is highest in 1996 (`A16`) and in 1995 (`A15`). It's also highest per person, at roughly twice the rate of any other region. Prague is still on top even though it's a single district and the other regions have 8–14.
- **Where the numbers come from:** I added up the district-level crime counts in the `district` table by region (`A3`). The per-1,000 rate divides by the number of inhabitants (`A4`), which is stored as text, so I converted it to a number first.
- **\*One gap:** Jesenik (north Moravia) has no 1995 crime count, so north Moravia's 1995 total is slightly low. Prague would still lead if it were filled in.
- **Lowest:** south Bohemia has the fewest crimes in total. East Bohemia has the lowest rate per person.

### Y

Files written: []

**Prague has the most crime**, whichever way you measure it. This comes from the `district` table in `financial.sqlite`, which has crime counts for 1995 (column `A15`) and 1996 (column `A16`). I added up the districts in each of the 8 regions.

| Region | Districts | Crimes 1995 | Crimes 1996 | Crimes per 1,000 people (1996) |
|---|---|---|---|---|
| **Prague** | 1 | **85,677** | **99,107** | **82.2** |
| north Moravia | 11 | 67,793* | 67,917 | 34.5 |
| south Moravia | 14 | 52,780 | 53,841 | 26.2 |
| north Bohemia | 10 | 48,634 | 48,380 | 41.0 |
| central Bohemia | 12 | 40,624 | 43,447 | 39.3 |
| east Bohemia | 11 | 30,420 | 30,646 | 24.8 |
| west Bohemia | 10 | 24,769 | 25,863 | 30.1 |
| south Bohemia | 8 | 17,927 | 18,173 | 25.9 |

- **Total crimes:** Prague is a single district, but it had about 99,000 crimes in 1996. That's 46% more than north Moravia, which comes second.
- **Per person:** Prague's rate of 82 crimes per 1,000 people is about twice the next-highest region, north Bohemia at 41.
- \***Missing value:** The Jesenik district in north Moravia has no 1995 crime count, so north Moravia's 1995 total is slightly low. That doesn't change the ranking.

## B5: "how much do we make from fees"

### X

Files written: []

**About 2.78 million CZK in fees, 1993–1998. Most of it, 2.74M CZK (99%), is the monthly statement fee.** The rest is penalty interest charged on overdrawn accounts.

| Year | Statement fees | Penalty interest | Total fees | Accounts charged |
|---|---:|---:|---:|---:|
| 1993 | 51,000 | 75 | 51,075 | 678 |
| 1994 | 237,670 | 699 | 238,369 | 1,359 |
| 1995 | 350,945 | 1,126 | 352,071 | 1,921 |
| 1996 | 511,500 | 1,831 | 513,331 | 2,965 |
| 1997 | 759,690 | 6,838 | 766,528 | 4,036 |
| 1998 | 833,240 | 27,167 | **860,407** | 4,396 |
| **Total** | **2,744,045** | **37,736** | **2,781,781** | 4,396 |

Amounts are in Czech crowns (CZK). The data description says USD, but the project notes say that's wrong.

- **The rise mostly comes from more accounts, not higher fees.** The statement fee ranges from 15 to 100 CZK per charge. Fee income grows roughly in step with the number of accounts that have transactions: about 1,100 in 1993, about 4,500 by 1997–98.
- **Penalty interest is small but jumped in 1998.** It went from 6.8k to 27k CZK, across 264 accounts in total. Some of these rows are 0 CZK.

**How I defined "fees"**
- I counted transactions coded as statement fee (`SLUZBY`) or penalty interest on a negative balance (`SANKC. UROK`) in `trans.k_symbol`. All 157k of these rows are money taken out of customer accounts.
- I did **not** count loan interest. The data has loan amounts and payments but no clean interest split, and interest isn't really a fee. I also left out insurance payments (`POJISTNE`), which look like money passed through to an insurer rather than bank income.
- About 535k transactions have no category code. If the bank charged any fees without coding them, they're missing from these totals.

I ran these queries directly against `financial.sqlite`. They were not run through TrueQuery's checker.

If you want this as a fixed company metric, or a wider revenue view that includes an estimate of loan interest, I can set either up.

### Y

Files written: []

About **$2.78M in total fees** over the whole dataset (Jan 1993 – Dec 1998).

The data has no "fee" field, so I counted the two transaction codes that are charges the bank takes from customers:

| Fee type | Code | Charges | Accounts | Total |
|---|---|---|---|---|
| Statement fees | `SLUZBY` | 155,832 | 4,396 | **$2,744,045** |
| Penalty interest on negative balances | `SANKC. UROK` | 1,577 | 264 | **$37,736** |
| **Total** | | | | **$2,781,781** |

**By year:**

| Year | Statement fees | Penalty interest | Total |
|---|---|---|---|
| 1993 | $51,000 | $75 | $51,075 |
| 1994 | $237,670 | $699 | $238,369 |
| 1995 | $350,945 | $1,126 | $352,071 |
| 1996 | $511,500 | $1,831 | $513,331 |
| 1997 | $759,690 | $6,838 | $766,528 |
| 1998 | $833,240 | $27,167 | $860,407 |

- **Fees grew every year,** mostly because there were more accounts. The average statement fee stayed around $17–18.
- **Penalty interest is small but rising fast.** It roughly quadrupled from 1997 to 1998, so more accounts are going negative.
- **The bank paid out more interest than it took in fees.** It credited customers about $27.5M in interest (`UROK`) over the same period, roughly 10 times the fee income. So fees don't come close to covering what the bank pays on deposits.

Loan interest the bank earns isn't in these numbers. Loan repayments (`UVER`) bundle principal and interest together, so the interest part can't be separated out without modeling it. I can estimate it from the `loan` table if you want.
