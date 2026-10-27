DELETE FROM staging_orders
USING loaded_batches
WHERE staging_orders.batch_id = loaded_batches.batch_id;
