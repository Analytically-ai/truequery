{{ config(materialized='view') }}
-- stg_refunds: one row per raw refund.
-- Notes: customer_id is NULLABLE (anonymous/goodwill refunds); only
-- status = 'processed' counts toward revenue; amount_cents.
with raw as (
    select * from {{ source('raw', 'refunds') }}
),

typed as (
    select
        refund_id,
        order_id,
        payment_id,
        customer_id,
        refunded_at,
        created_at,
        amount_cents,
        currency,
        lower(trim(status)) as status_raw,
        lower(trim(reason)) as reason
    from raw
),

flagged as (
    select
        *,
        case when status_raw = 'processed' then true else false end as is_processed,
        customer_id is null as is_anonymous,
        date_trunc('day', refunded_at) as refund_day_utc
    from typed
)

select * from flagged
