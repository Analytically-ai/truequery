{{ config(materialized='view') }}
-- stg_customers: current-row-per-customer view over the SCD history.
-- Notes: customers_history has >1 row per customer; only is_current is live;
-- test/internal accounts must be excluded downstream; soft deletes via deleted_at.
with history as (
    select * from {{ source('raw', 'customers_history') }}
),

base as (
    select * from {{ source('raw', 'customers') }}
),

current_history as (
    select * from history where is_current
),

joined as (
    select
        b.customer_id,
        b.email,
        b.full_name,
        coalesce(h.country, b.country) as country,
        b.currency,
        b.is_test,
        b.created_at,
        b.updated_at,
        b.deleted_at,
        h.plan_tier as current_plan_tier,
        b.deleted_at is not null as is_deleted,
        (
            lower(b.email) like '%@example.com'
            or lower(b.email) like '%@internal.corp'
        ) as is_test_domain
    from base b
    left join current_history h using (customer_id)
)

select * from joined
