DELETE FROM staging_orders
USING loaded_batches
WHERE 1 = 1;
