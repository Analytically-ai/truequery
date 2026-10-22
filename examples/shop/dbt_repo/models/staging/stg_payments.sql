{{ config(materialized='view') }}
-- stg_payments: one row per raw payment, status normalized.
-- Notes: amount_cents (not dollars); paid_at is the business timestamp;
-- legacy statuses 'success'/'SUCCEEDED'; split payments share an order_id.
with raw as (
    select * from {{ source('raw', 'payments') }}
),

typed as (
    select
        payment_id,
        order_id,
        customer_id,
        paid_at,
        created_at,
        amount_cents,
        currency,
        lower(trim(status)) as status_raw,
        lower(trim(method)) as method
    from raw
),

flagged as (
    select
        *,
        {{ normalize_payment_status('status_raw') }} as status_normalized,
        date_trunc('day', paid_at) as paid_day_utc
    from typed
)

select * from flagged
