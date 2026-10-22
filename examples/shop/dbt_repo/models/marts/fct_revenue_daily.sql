{{ config(materialized='table') }}
-- fct_revenue_daily: one row per UTC day with gross/net revenue in USD.
--
-- House rules applied (see HOUSE_DEFINITIONS.md):
--   D1 revenue = completed net of processed refunds, excl. test, in USD
--   D4 day grain = ordered_at::date UTC; refunds net on the ORDER day,
--      not the refund day (cohort-consistent revenue)
--   D5 soft-deleted customers stay excluded via the test/delete flags
--   D7 cents/100 and fx_rates join on the business date
with orders as (
    select * from {{ ref('fct_orders') }}
),

-- calendar spine so days with zero revenue still appear
spine as (
    select
        (date '2024-01-01' + (n || ' days')::interval)::date as day_utc
    from range(0, 500) t(n)
),

-- D1: only revenue-eligible orders aggregate
eligible as (
    select
        order_day_utc as day_utc,
        count(*) as n_orders,
        count(distinct customer_id) as n_customers,
        sum(gross_usd) as gross_usd,
        sum(refunded_usd) as refunded_usd,
        sum(revenue_usd) as revenue_usd
    from orders
    where is_revenue_eligible
    group by 1
),

-- refunds that hit NON-eligible orders (test/failed) are tracked separately
-- so finance can reconcile gross-to-net without polluting D1 revenue
non_eligible_refunds as (
    select
        order_day_utc as day_utc,
        sum(refunded_usd) as non_eligible_refunded_usd,
        count(*) filter (where n_refunds > 0) as n_orders_with_refund
    from orders
    where not is_revenue_eligible
    group by 1
),

-- marketing spend per day in USD for ROAS (spend is also cents + multi-fx)
spend as (
    select * from {{ source('raw', 'marketing_spend') }}
),

fx as (
    select rate_date, currency, rate_to_usd from {{ source('raw', 'fx_rates') }}
),

daily_spend as (
    select
        s.spend_date as day_utc,
        sum((s.amount_cents
            / case when s.currency = 'JPY' then 1.0 else 100.0 end
        ) * fx.rate_to_usd) as marketing_usd
    from spend s
    left join fx
        on fx.currency = s.currency
        and fx.rate_date = s.spend_date
    group by 1
),

-- new vs returning split on the revenue-eligible population
cohort_split as (
    select
        e.order_day_utc as day_utc,
        count(*) filter (
            where d.first_order_day = e.order_day_utc
        ) as n_new_customer_orders,
        count(*) filter (
            where d.first_order_day < e.order_day_utc
        ) as n_returning_customer_orders,
        sum(e.revenue_usd) filter (
            where d.first_order_day = e.order_day_utc
        ) as new_customer_revenue_usd,
        sum(e.revenue_usd) filter (
            where d.first_order_day < e.order_day_utc
        ) as returning_customer_revenue_usd
    from orders e
    join {{ ref('dim_customers') }} d using (customer_id)
    where e.is_revenue_eligible
    group by 1
),

assembled as (
    select
        s.day_utc,
        coalesce(e.n_orders, 0) as n_orders,
        coalesce(e.n_customers, 0) as n_customers,
        round(coalesce(e.gross_usd, 0), 2) as gross_usd,
        round(coalesce(e.refunded_usd, 0), 2) as refunded_usd,
        round(coalesce(e.revenue_usd, 0), 2) as revenue_usd,
        round(coalesce(n.non_eligible_refunded_usd, 0), 2)
            as non_eligible_refunded_usd,
        coalesce(n.n_orders_with_refund, 0) as n_non_eligible_orders_with_refund,
        round(coalesce(m.marketing_usd, 0), 2) as marketing_usd,
        coalesce(c.n_new_customer_orders, 0) as n_new_customer_orders,
        coalesce(c.n_returning_customer_orders, 0) as n_returning_customer_orders,
        round(coalesce(c.new_customer_revenue_usd, 0), 2) as new_customer_revenue_usd,
        round(coalesce(c.returning_customer_revenue_usd, 0), 2)
            as returning_customer_revenue_usd
    from spine s
    left join eligible e on e.day_utc = s.day_utc
    left join non_eligible_refunds n on n.day_utc = s.day_utc
    left join daily_spend m on m.day_utc = s.day_utc
    left join cohort_split c on c.day_utc = s.day_utc
),

-- D5 audit: soft-deleted-customer orders excluded from revenue, by day
deleted_excluded as (
    select
        o.order_day_utc as day_utc,
        count(*) as n_deleted_customer_orders,
        round(sum((o.total_cents
            / case when o.currency = 'JPY' then 1.0 else 100.0 end
        ) * fx.rate_to_usd), 2) as deleted_gross_usd
    from {{ ref('stg_orders') }} o
    join {{ ref('stg_customers') }} c using (customer_id)
    left join fx
        on fx.currency = o.currency
        and fx.rate_date = o.order_day_utc
    where c.is_deleted
        and o.status_normalized = 'completed'
    group by 1
),

-- weekday label for weekly-seasonality analysis (derived from D4 day grain)
with_dow as (
    select
        a.*,
        dayname(a.day_utc) as weekday,
        -- D10 note: ties inside a day do not affect daily grain
        case
            when a.revenue_usd > 0 and a.n_orders = 0 then 'CHECK: revenue without orders'
            when a.revenue_usd < 0 then 'CHECK: negative revenue day'
        end as data_quality_flag
    from assembled a
),

final as (
    select
        w.*,
        coalesce(d.n_deleted_customer_orders, 0) as n_deleted_customer_orders,
        coalesce(d.deleted_gross_usd, 0) as deleted_gross_usd,
        -- ROAS is null (not zero) on days with no spend
        case
            when w.marketing_usd > 0 then w.revenue_usd / w.marketing_usd
        end as roas,
        sum(w.revenue_usd) over (
            order by w.day_utc rows between 6 preceding and current row
        ) as revenue_usd_trailing_7d,
        avg(w.revenue_usd) over (
            order by w.day_utc rows between 27 preceding and current row
        ) as revenue_usd_trailing_28d_avg
    from with_dow w
    left join deleted_excluded d on d.day_utc = w.day_utc
)

select * from final
order by day_utc
