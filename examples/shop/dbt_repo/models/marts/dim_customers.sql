{{ config(materialized='table') }}
-- dim_customers: one row per customer with lifecycle flags.
--
-- House rules applied (see HOUSE_DEFINITIONS.md):
--   D2 active customer = non-test, non-deleted, completed order in trailing 90d
--   D3 churn = active 90d ago with no completed order in the last 90d
--   D5 soft deletes (deleted_at) exclude everywhere except explicit audits
--   D8 SCD: customers_history is_current row is the live attribute source
with customers as (
    select * from {{ ref('stg_customers') }}
),

orders as (
    select * from {{ ref('stg_orders') }}
),

subscriptions as (
    select * from {{ source('raw', 'subscriptions') }}
),

tickets as (
    select * from {{ source('raw', 'support_tickets') }}
),

history as (
    select * from {{ source('raw', 'customers_history') }}
),

-- D8: guardrail -- exactly one current row per customer must exist
scd_check as (
    select
        customer_id,
        count(*) filter (where is_current) as n_current,
        count(*) as n_versions,
        min(valid_from) as first_valid_from,
        max(valid_to) as last_valid_to
    from history
    group by 1
),

-- D1/D2 inputs: completed, non-test order days per customer on ordered_at
completed_orders as (
    select
        o.customer_id,
        o.order_id,
        o.order_day_utc,
        o.ordered_at,
        (o.total_cents
            / case when o.currency = 'JPY' then 1.0 else 100.0 end
        ) as total_local
    from orders o
    where o.status_normalized = 'completed'
        and not o.is_test
),

per_customer_orders as (
    select
        customer_id,
        count(*) as lifetime_completed_orders,
        min(order_day_utc) as first_order_day,
        max(order_day_utc) as last_order_day,
        -- D10: deterministic latest order under ordered_at ties
        max(ordered_at) as last_ordered_at,
        sum(total_local) as lifetime_gross_local
    from completed_orders
    group by 1
),

-- warehouse "today": latest completed order day across all customers.
-- Trailing/prior 90d windows anchor here so churn can actually fire.
ref_day as (
    select max(order_day_utc) as max_day from completed_orders
),

activity_windows as (
    select
        p.customer_id,
        p.lifetime_completed_orders,
        p.first_order_day,
        p.last_order_day,
        p.last_ordered_at,
        p.lifetime_gross_local,
        -- orders in the trailing 90d ending at warehouse "today"
        (
            select count(*)
            from completed_orders c
            where c.customer_id = p.customer_id
                and c.order_day_utc > (select max_day from ref_day) - interval '90 days'
        ) as orders_trailing_90d,
        -- orders in the 90d window BEFORE that (churn comparison window)
        (
            select count(*)
            from completed_orders c
            where c.customer_id = p.customer_id
                and c.order_day_utc <= (select max_day from ref_day) - interval '90 days'
                and c.order_day_utc > (select max_day from ref_day) - interval '180 days'
        ) as orders_prior_90d
    from per_customer_orders p
),

subscription_state as (
    select
        customer_id,
        count(*) as n_subscriptions,
        count(*) filter (
            where lower(status) in ('active', 'trialing', 'past_due')
        ) as n_live_subscriptions,
        sum(mrr_cents
            / case when currency = 'JPY' then 1.0 else 100.0 end
        ) filter (
            where lower(status) in ('active', 'trialing', 'past_due')
                and deleted_at is null
        ) as live_mrr_local
    from subscriptions
    group by 1
),

ticket_stats as (
    select
        customer_id,
        count(*) as n_tickets,
        count(*) filter (
            where lower(status) in ('open', 'pending')
        ) as n_open_tickets
    from tickets
    where deleted_at is null
        and customer_id is not null
    group by 1
),

assembled as (
    select
        c.customer_id,
        c.email,
        c.full_name,
        c.country,
        c.currency,
        c.current_plan_tier,
        c.created_at,
        -- D5: soft-delete flag; D2: deleted customers are never active
        c.is_deleted,
        c.is_test,
        c.is_test_domain,
        coalesce(s.n_current, 0) as scd_current_rows,
        coalesce(s.n_versions, 0) as scd_versions,
        coalesce(a.lifetime_completed_orders, 0) as lifetime_completed_orders,
        a.first_order_day,
        a.last_order_day,
        a.last_ordered_at,
        coalesce(a.lifetime_gross_local, 0) as lifetime_gross_local,
        coalesce(a.orders_trailing_90d, 0) as orders_trailing_90d,
        coalesce(a.orders_prior_90d, 0) as orders_prior_90d,
        coalesce(b.n_subscriptions, 0) as n_subscriptions,
        coalesce(b.n_live_subscriptions, 0) as n_live_subscriptions,
        coalesce(b.live_mrr_local, 0) as live_mrr_local,
        coalesce(k.n_tickets, 0) as n_tickets,
        coalesce(k.n_open_tickets, 0) as n_open_tickets
    from customers c
    left join scd_check s using (customer_id)
    left join activity_windows a using (customer_id)
    left join subscription_state b using (customer_id)
    left join ticket_stats k using (customer_id)
),

final as (
    select
        *,
        -- D2: active = live row, non-test, non-deleted, ordered in trailing 90d
        (
            not is_test
            and not is_deleted
            and orders_trailing_90d > 0
        ) as is_active_customer,
        -- D3: churned = was ordering in the prior window, silent in trailing
        (
            not is_test
            and not is_deleted
            and orders_trailing_90d = 0
            and orders_prior_90d > 0
        ) as is_churned,
        -- brand-new: first order ever is inside the trailing window
        (
            not is_test
            and lifetime_completed_orders > 0
            and first_order_day = last_order_day
        ) as is_new_customer
    from assembled
)

select * from final
order by customer_id
