{{ config(materialized='view') }}
-- stg_orders: one row per raw order, data issues kept but flagged.
-- Notes: amounts are CENTS; ordered_at is the business date (created_at/
-- updated_at are write times); status has legacy variants; is_test rows exist.
with raw as (
    select * from {{ source('raw', 'orders') }}
),

typed as (
    select
        order_id,
        customer_id,
        ordered_at,
        created_at,
        updated_at,
        lower(trim(status)) as status_raw,
        currency,
        subtotal_cents,
        discount_cents,
        tax_cents,
        shipping_cents,
        is_test
    from raw
),

flagged as (
    select
        *,
        {{ normalize_order_status('status_raw') }} as status_normalized,
        (subtotal_cents - discount_cents + tax_cents + shipping_cents) as total_cents,
        date_trunc('day', ordered_at) as order_day_utc,
        -- tie-break key: ordered_at ties are real, so order_id breaks them
        row_number() over (
            partition by ordered_at order by order_id
        ) as ordered_at_tie_rank
    from typed
)

select * from flagged
