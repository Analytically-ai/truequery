CREATE TABLE daily_orders
DISTSTYLE KEY
DISTKEY (customer_id)
SORTKEY (order_date)
AS
SELECT customer_id, created_at::DATE AS order_date, SUM(amount) AS revenue
FROM orders
WHERE customer_id NOT IN (
    SELECT customer_id FROM blocked_customers
)
GROUP BY customer_id, created_at::DATE;
