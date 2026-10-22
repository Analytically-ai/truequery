{{ config(materialized='table') }}
-- fct_subscription_mrr: one row per subscription per month with MRR in USD.
--
-- House rules applied (see HOUSE_DEFINITIONS.md):
--   D3 churn: ended subscriptions whose ended_at falls in-month count as
--      churned; legacy 'canceled' folds to 'cancelled'
--   D5 soft-deleted subscription rows are excluded from live MRR
--   D7 mrr_cents/100 with the month-start FX rate
--   D8 customer tier comes from the SCD is_current row (stg_customers)
with subscriptions as (
    select * from {{ source('raw', 'subscriptions') }}
),

customers as (
    select * from {{ ref('stg_customers') }}
),

plans as (
    select * from {{ source('raw', 'plans') }}
),

fx as (
    select rate_date, currency, rate_to_usd from {{ source('raw', 'fx_rates') }}
),

-- month spine covering the warehouse range
months as (
    select
        (date_trunc('month', date '2024-01-01' + (n || ' days')::interval))::date
            as month_start
    from range(0, 500) t(n)
    group by 1
),

-- D3/D6: canonical subscription status incl. legacy variants
normalized as (
    select
        s.*,
        {{ normalize_subscription_status('status') }} as status_canon
    from subscriptions s
),

-- D5: soft-deleted rows leave live MRR but are kept for audit reconciliation
live as (
    select * from normalized where deleted_at is null
),

-- expand each subscription across the months it is alive:
-- from start month through (but excluding) the end month
alive_months as (
    select
        l.subscription_id,
        l.customer_id,
        l.plan_id,
        l.status_canon,
        l.mrr_cents,
        l.currency,
        l.started_at,
        l.ended_at,
        m.month_start
    from live l
    join months m
        on m.month_start >= date_trunc('month', l.started_at)::date
        and (
            l.ended_at is null
            or m.month_start < date_trunc('month', l.ended_at)::date
        )
),

-- D7: MRR converted at the month-start rate for the subscription currency
with_fx as (
    select
        a.*,
        fx.rate_to_usd as fx_rate,
        (a.mrr_cents
            / case when a.currency = 'JPY' then 1.0 else 100.0 end
        ) * fx.rate_to_usd as mrr_usd
    from alive_months a
    left join fx
        on fx.currency = a.currency
        and fx.rate_date = a.month_start
),

enriched as (
    select
        w.*,
        c.current_plan_tier as customer_tier_now,
        c.is_test as customer_is_test,
        p.name as plan_name,
        p.tier as plan_tier,
        -- new logo: subscription started inside this month
        (
            date_trunc('month', w.started_at)::date = w.month_start
        ) as is_new_in_month,
        -- D3: churned logo: subscription ended inside this month
        (
            w.ended_at is not null
            and date_trunc('month', w.ended_at)::date = w.month_start
        ) as is_churned_in_month,
        -- expansion/contraction vs plan list price (local cents)
        (w.mrr_cents - p.monthly_price_cents) as vs_list_cents
    from with_fx w
    left join customers c using (customer_id)
    left join plans p on p.plan_id = w.plan_id
),

-- per-month rollup split by test exclusion (D2): headline MRR excludes
-- test/internal customers; contra row keeps them for reconciliation
monthly as (
    select
        month_start,
        count(*) filter (where not customer_is_test) as n_live_subs,
        round(sum(mrr_usd) filter (where not customer_is_test), 2) as mrr_usd,
        count(*) filter (where not customer_is_test and is_new_in_month)
            as n_new_subs,
        count(*) filter (where not customer_is_test and is_churned_in_month)
            as n_churned_subs,
        count(*) filter (where customer_is_test) as n_test_subs,
        round(sum(mrr_usd) filter (where customer_is_test), 2) as test_mrr_usd,
        -- logo churn rate: churned / (live + churned) on non-test population
        count(*) filter (
            where not customer_is_test
                and (is_churned_in_month or status_canon in ('active', 'trialing', 'past_due'))
        ) as churn_base_subs
    from enriched
    group by 1
),

-- D5 audit: soft-deleted subscriptions excluded from live MRR, by month
deleted_excluded as (
    select
        date_trunc('month', started_at)::date as month_start,
        count(*) as n_deleted_subs_excluded,
        round(sum((mrr_cents
            / case when n.currency = 'JPY' then 1.0 else 100.0 end
        ) * fx.rate_to_usd), 2) as deleted_mrr_usd
    from normalized n
    left join fx
        on fx.currency = n.currency
        and fx.rate_date = date_trunc('month', n.started_at)::date
    where n.deleted_at is not null
    group by 1
),

-- plan-tier split of live non-test MRR for mix-shift analysis
tier_split as (
    select
        month_start,
        sum(mrr_usd) filter (where plan_tier = 'pro') as mrr_usd_pro,
        sum(mrr_usd) filter (where plan_tier = 'basic') as mrr_usd_basic,
        sum(mrr_usd) filter (where plan_tier = 'free') as mrr_usd_free,
        count(*) filter (where plan_tier = 'pro') as n_subs_pro,
        count(*) filter (where plan_tier = 'basic') as n_subs_basic
    from enriched
    where not customer_is_test
    group by 1
),

final as (
    select
        m.*,
        coalesce(d.n_deleted_subs_excluded, 0) as n_deleted_subs_excluded,
        coalesce(d.deleted_mrr_usd, 0) as deleted_mrr_usd,
        coalesce(t.mrr_usd_pro, 0) as mrr_usd_pro,
        coalesce(t.mrr_usd_basic, 0) as mrr_usd_basic,
        coalesce(t.mrr_usd_free, 0) as mrr_usd_free,
        coalesce(t.n_subs_pro, 0) as n_subs_pro,
        coalesce(t.n_subs_basic, 0) as n_subs_basic,
        case
            when m.churn_base_subs > 0
            then m.n_churned_subs::double / m.churn_base_subs
        end as logo_churn_rate,
        sum(m.mrr_usd) over (order by m.month_start) as mrr_usd_running_check,
        m.mrr_usd - lag(m.mrr_usd) over (order by m.month_start) as mrr_usd_net_change
    from monthly m
    left join deleted_excluded d on d.month_start = m.month_start
    left join tier_split t on t.month_start = m.month_start
)

select * from final
order by month_start
