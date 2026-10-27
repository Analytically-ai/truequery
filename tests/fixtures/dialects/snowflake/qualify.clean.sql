SELECT customer_id, order_id, created_at
FROM orders
WHERE status IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY customer_id ORDER BY created_at DESC, order_id DESC
) = 1;
