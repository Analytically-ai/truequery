CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT,
    created_at TEXT
);
DELETE FROM events
WHERE typeof(payload) = 'text'
  AND created_at < strftime('%Y-%m-%d', datetime('now', '-30 days'));
