-- TrueQuery sample: a clean file. Run: truequery detect samples/good_orders.sql
SELECT o.id, o.total, c.name
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.status = 'done'
ORDER BY o.created_at
LIMIT 100;

SELECT COUNT(*) AS n FROM orders;

SELECT DISTINCT customer_id FROM orders WHERE total > 0;

SELECT * EXCEPT (internal_note) FROM orders;

SELECT 1 FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id LIMIT 1);
