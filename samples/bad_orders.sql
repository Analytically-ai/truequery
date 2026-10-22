-- TrueQuery demo: queries that run, look right, and return the wrong answer.
-- Run: truequery detect samples/bad_orders.sql

-- Revenue per customer, but the join has no predicate: every customer is
-- paired with every order.
SELECT c.name, SUM(o.total) AS revenue
FROM customers c, orders o
GROUP BY c.name;

-- Customers who never ordered. One order with a NULL customer_id makes
-- NOT IN return no rows at all.
SELECT id FROM customers
WHERE id NOT IN (SELECT customer_id FROM orders);

-- Orders per customer, keeping customers with no orders. COUNT(*) counts
-- the unmatched row, so those customers show 1 order instead of 0.
SELECT c.id, COUNT(*) AS order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id;

-- "All customers, with their completed orders". The WHERE on the joined
-- table drops every customer without one: the LEFT JOIN became an INNER.
SELECT c.name, o.total
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'completed';

-- Orders without a coupon. `= NULL` is never true, so this returns nothing.
SELECT id FROM orders WHERE coupon_code = NULL;

-- March orders. A timestamp at 14:00 on 31 March is after '2025-03-31',
-- so the last day is silently dropped.
SELECT COUNT(*) FROM orders
WHERE ordered_at BETWEEN '2025-03-01' AND '2025-03-31';

-- Clear yesterday's staging rows. No WHERE: this deletes every row.
DELETE FROM staging_orders;
