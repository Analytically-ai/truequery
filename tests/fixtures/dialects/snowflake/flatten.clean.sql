SELECT e.event_id, item.value:sku::VARCHAR AS sku,
       item.value:quantity::NUMBER AS quantity
FROM raw_events AS e,
     LATERAL FLATTEN(INPUT => e.payload:items) AS item
WHERE item.value:sku IS NOT NULL;
