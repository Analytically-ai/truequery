SELECT customer_id, first_name || ' ' || last_name AS full_name
FROM customers
WHERE customer_id NOT IN (
    SELECT customer_id FROM blocked_customers
)
ORDER BY customer_id LIMIT 10 OFFSET 5;
