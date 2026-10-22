{{ config(materialized='table') }}
-- fct_orders: one row per order with net revenue in USD.
--
-- House rules applied (see HOUSE_DEFINITIONS.md):
--   D1 revenue = completed orders, net of processed refunds, excl. test, USD
--   D4 order date = ordered_at (UTC day), never created_at
--   D6 status normalization with legacy values
--   D7 money is minor units until divided (100, except zero-decimal JPY = 1);
--   FX via fx_rates on the order day
--   D9 refunds with NULL customer_id still net against the ORDER
--   D10 ordered_at ties broken deterministically by order_id
with orders as (
    select * from {{ ref('stg_orders') }}
),

payments as (
    select * from {{ ref('stg_payments') }}
),

refunds as (
    select * from {{ ref('stg_refunds') }}
),

customers as (
    select * from {{ ref('stg_customers') }}
),

fx as (
    select rate_date, currency, rate_to_usd from {{ source('raw', 'fx_rates') }}
),

-- D6: canonical order statuses; legacy 'complete'/'COMPLETE'/'done' fold in
normalized_orders as (
    select
        o.*,
        case status_normalized
            when 'completed' then 'completed'
            when 'pending' then 'pending'
            when 'cancelled' then 'cancelled'
            when 'refunded' then 'refunded'
            when 'failed' then 'failed'
            else 'other'
        end as status_canon
    from orders o
),

-- D1/D7: gross per order in USD via the order-day FX rate
order_gross as (
    select
        n.*,
        fx.rate_to_usd as fx_rate,
        -- D7: JPY is zero-decimal (whole yen), other currencies are cents
        (n.total_cents
            / case when n.currency = 'JPY' then 1.0 else 100.0 end)
            * fx.rate_to_usd as gross_usd
    from normalized_orders n
    left join fx
        on fx.currency = n.currency
        and fx.rate_date = date_trunc('day', n.ordered_at)::date
),

-- succeeded payments only; split payments aggregated to the order
order_payments as (
    select
        order_id,
        count(*) as n_payments,
        sum(amount_cents
            / case when currency = 'JPY' then 1.0 else 100.0 end
        ) as paid_local,
        bool_or(status_normalized = 'succeeded') as has_succeeded_payment
    from payments
    where status_normalized in ('succeeded', 'refunded', 'disputed')
    group by 1
),

-- D1/D9: only processed refunds net out, joined to the ORDER so that
-- anonymous (NULL customer_id) refunds still reduce revenue
order_refunds as (
    select
        order_id,
        count(*) as n_refunds,
        sum(amount_cents
            / case when r.currency = 'JPY' then 1.0 else 100.0 end
        ) as refunded_local,
        sum(
            (r.amount_cents
                / case when r.currency = 'JPY' then 1.0 else 100.0 end)
            * coalesce(fx.rate_to_usd, 1.0)
        ) as refunded_usd
    from refunds r
    left join fx
        on fx.currency = r.currency
        and fx.rate_date = date_trunc('day', r.refunded_at)::date
    where r.is_processed
    group by 1
),

-- customer flags: test accounts and soft deletes flow through, never filtered
-- here (filtering is the consumer's job per D2) but are always labeled
customer_flags as (
    select
        customer_id,
        is_test,
        is_deleted,
        is_test_domain,
        current_plan_tier
    from customers
),

assembled as (
    select
        g.order_id,
        g.customer_id,
        -- D4: the business date is ordered_at, truncated to UTC day
        g.ordered_at,
        g.order_day_utc,
        g.created_at,
        g.updated_at,
        g.status_canon as status,
        g.currency,
        g.subtotal_cents,
        g.discount_cents,
        g.tax_cents,
        g.shipping_cents,
        g.total_cents,
        g.fx_rate,
        round(g.gross_usd, 2) as gross_usd,
        coalesce(p.n_payments, 0) as n_payments,
        coalesce(p.paid_local, 0) as paid_local,
        coalesce(p.has_succeeded_payment, false) as has_succeeded_payment,
        coalesce(r.n_refunds, 0) as n_refunds,
        coalesce(r.refunded_usd, 0) as refunded_usd,
        round(g.gross_usd - coalesce(r.refunded_usd, 0), 2) as net_usd,
        coalesce(c.is_test, true) as customer_is_test,
        coalesce(c.is_deleted, false) as customer_is_deleted,
        coalesce(c.is_test_domain, false) as customer_is_test_domain,
        c.current_plan_tier,
        g.ordered_at_tie_rank
    from order_gross g
    left join order_payments p using (order_id)
    left join order_refunds r using (order_id)
    left join customer_flags c using (customer_id)
),

-- D1: revenue-ready = completed, non-test, has a succeeded payment
final as (
    select
        *,
        (
            status = 'completed'
            and not customer_is_test
            and has_succeeded_payment
        ) as is_revenue_eligible,
        case
            when status = 'completed'
                and not customer_is_test
                and has_succeeded_payment
            then net_usd
            else 0
        end as revenue_usd
    from assembled
)

-- D10: deterministic order for tied ordered_at values
select * from final
order by ordered_at, order_id
