select
    c.customer_id,
    c.name,
    min(o.placed_at) as first_order_at,
    count(o.order_id) as order_count
from {{ ref('stg_customers') }} as c
left join {{ ref('orders') }} as o
    on o.customer_id = c.customer_id
group by c.customer_id, c.name
