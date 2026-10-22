MODEL (
  name sushi.orders,
  kind FULL
);

SELECT id FROM raw_orders WHERE id NOT IN (SELECT oid FROM cancelled);
