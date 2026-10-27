SELECT customer_id,
       SUM(IFF(status = 'paid', amount, 0)) AS paid_total
FROM orders
WHERE customer_id IS NOT NULL
GROUP BY customer_id;
