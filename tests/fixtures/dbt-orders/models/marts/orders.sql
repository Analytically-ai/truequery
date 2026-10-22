-- A CTE named order_items shadows the ref() of the same name: the ref
-- is still a real upstream model.
with

orders as (
    select * from {{ ref('stg_orders') }}
),

order_items as (
    select * from {{ ref('order_items') }}
),

item_totals as (
    select
        order_id,
        count(item_id) as item_count,
        sum(unit_price * quantity) as items_subtotal,
        sum(case when is_gift then 1 else 0 end) as gift_items
    from order_items
    group by order_id
),

enriched as (
    select
        o.order_id,
        o.customer_id,
        o.status,
        o.placed_at,
        coalesce(t.item_count, 0) as item_count,
        coalesce(t.items_subtotal, 0) as items_subtotal,
        coalesce(t.gift_items, 0) > 0 as has_gift
    from orders as o
    left join item_totals as t
        on t.order_id = o.order_id
),

sequenced as (
    select
        *,
        row_number() over (
            partition by customer_id
            order by placed_at, order_id
        ) as customer_order_number
    from enriched
)

select * from sequenced
