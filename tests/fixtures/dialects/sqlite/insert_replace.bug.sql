INSERT OR REPLACE INTO daily_orders (order_id, customer_id, updated_at)
SELECT order_id, customer_id, datetime('now') FROM staging_orders
WHERE customer_id = NULL;
