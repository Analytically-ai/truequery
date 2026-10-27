SELECT customer_id, order_id, created_at::DATE AS order_date
FROM orders AS o
WHERE status = NULL
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY customer_id ORDER BY created_at DESC, order_id DESC
) = 1;
