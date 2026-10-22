"""Build warehouse.duckdb: 24 tables of realistic, messy shop data. Deterministic (seed 42)."""
import datetime as dt
import os
import random

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "warehouse.duckdb")
rng = random.Random(42)

CURRENCIES = ["USD", "EUR", "GBP", "JPY"]
FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0071}
# Legacy/variant status values present (must be normalized)
ORDER_STATUSES = ["completed", "complete", "COMPLETE", "done",
                  "pending", "cancelled", "canceled", "refunded", "failed"]
PAYMENT_STATUSES = ["succeeded", "success", "SUCCEEDED", "pending",
                    "failed", "refunded", "disputed"]
SUB_STATUSES = ["active", "ACTIVE", "trialing", "past_due",
                "canceled", "cancelled", "paused"]

BASE = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


def ts(days, sec=0):
    return (BASE + dt.timedelta(days=days, seconds=sec)).isoformat()


con = duckdb.connect(DB)
con.execute("PRAGMA memory_limit='2GB'")
con.execute("PRAGMA threads=1")  # deterministic physical layout
t = con.execute

t("DROP TABLE IF EXISTS customers")
t("""CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY, email VARCHAR, full_name VARCHAR,
  country VARCHAR, currency VARCHAR, is_test BOOLEAN,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ)""")
cust_rows = []
N_CUST = 600
for i in range(1, N_CUST + 1):
    is_test = i % 40 == 0
    email = (f"qa+{i}@example.com" if i % 40 == 0
             else f"staff{i}@internal.corp" if i % 57 == 0
             else f"user{i}@mailbox.org")
    if i % 57 == 0:
        is_test = True  # internal accounts are test accounts
    cur = rng.choice(CURRENCIES)
    c = rng.randint(0, 400)
    d = rng.choice([None, None, None, rng.randint(300, 500)])  # soft deletes
    cust_rows.append((i, email, f"User {i}", rng.choice(["US", "DE", "GB", "JP"]),
                      cur, is_test, ts(c), ts(c + 5),
                      ts(d) if d else None))
t("INSERT INTO customers VALUES " + ",".join(
    "(%d,'%s','%s','%s','%s',%s,'%s','%s',%s)" % (
        r[0], r[1], r[2], r[3], r[4], str(r[5]).lower(), r[6], r[7],
        ("'%s'" % r[8]) if r[8] else "NULL") for r in cust_rows))

# SCD type-2 history: >1 row for many customers
t("DROP TABLE IF EXISTS customers_history")
t("""CREATE TABLE customers_history (
  customer_id INTEGER, valid_from TIMESTAMPTZ, valid_to TIMESTAMPTZ,
  is_current BOOLEAN, plan_tier VARCHAR, country VARCHAR, email VARCHAR)""")
hist = []
for i in range(1, N_CUST + 1):
    nver = 1 if i % 3 == 0 else (3 if i % 7 == 0 else 2)
    start = rng.randint(0, 100)
    for v in range(nver):
        end = start + rng.randint(30, 150)
        cur = v == nver - 1
        hist.append((i, ts(start), ts(500) if cur else ts(end), cur,
                     rng.choice(["free", "basic", "pro"]),
                     rng.choice(["US", "DE", "GB", "JP"]),
                     f"user{i}@mailbox.org"))
        start = end
t("INSERT INTO customers_history VALUES " + ",".join(
    "(%d,'%s','%s',%s,'%s','%s','%s')" % (
        h[0], h[1], h[2], str(h[3]).lower(), h[4], h[5], h[6]) for h in hist))

t("DROP TABLE IF EXISTS product_categories")
t("CREATE TABLE product_categories (category_id INTEGER PRIMARY KEY, name VARCHAR, parent_id INTEGER)")
t("INSERT INTO product_categories VALUES (1,'Widgets',NULL),(2,'Gadgets',NULL),(3,'Premium Widgets',1)")

t("DROP TABLE IF EXISTS products")
t("""CREATE TABLE products (product_id INTEGER PRIMARY KEY, sku VARCHAR,
  name VARCHAR, category_id INTEGER, list_price_cents INTEGER, currency VARCHAR,
  status VARCHAR, created_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ)""")
prows = []
for i in range(1, 61):
    price = rng.randint(499, 49999)  # cents, not dollars
    cur = "USD" if i % 2 == 0 else rng.choice(CURRENCIES)
    st = rng.choice(["live", "live", "live", "archived", "draft"])
    dl = ts(rng.randint(350, 450)) if st == "archived" else None
    prows.append((i, f"SKU-{i:04d}", f"Product {i}", rng.choice([1, 2, 3]),
                  price, cur, st, ts(0), dl))
t("INSERT INTO products VALUES " + ",".join(
    "(%d,'%s','%s',%d,%d,'%s','%s','%s',%s)" % (
        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
        ("'%s'" % r[8]) if r[8] else "NULL") for r in prows))

t("DROP TABLE IF EXISTS plans")
t("CREATE TABLE plans (plan_id INTEGER PRIMARY KEY, name VARCHAR, tier VARCHAR, monthly_price_cents INTEGER, currency VARCHAR)")
t("""INSERT INTO plans VALUES (1,'Free','free',0,'USD'),(2,'Basic','basic',1200,'USD'),
  (3,'Pro','pro',4900,'USD'),(4,'Pro EU','pro',4500,'EUR')""")

# Orders: ordered_at (business date) vs created_at/updated_at differ;
# some timestamp ties; amounts in cents
t("DROP TABLE IF EXISTS orders")
t("""CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER,
  ordered_at TIMESTAMPTZ, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
  status VARCHAR, currency VARCHAR, subtotal_cents INTEGER, discount_cents INTEGER,
  tax_cents INTEGER, shipping_cents INTEGER, is_test BOOLEAN)""")
orows = []
oid = 1
for d in range(0, 480):
    for _ in range(rng.randint(2, 9)):
        cid = rng.randint(1, N_CUST)
        test = cid % 40 == 0 or cid % 57 == 0
        sub = rng.randint(1000, 90000)
        disc = rng.choice([0, 0, 0, rng.randint(100, max(101, int(sub * 0.3)))])
        tax = int(sub * 0.08)
        ship = rng.choice([0, 499, 999])
        # ties: several orders share identical ordered_at each 25th day
        sec = 0 if d % 25 == 0 else rng.randint(0, 86399)
        oc = ts(d, sec)
        orows.append((oid, cid, oc, ts(d, sec + 3600), ts(d, sec + 7200),
                      rng.choice(ORDER_STATUSES),
                      rng.choice(CURRENCIES), sub, disc, tax, ship, test))
        oid += 1
t("INSERT INTO orders VALUES " + ",".join(
    "(%d,%d,'%s','%s','%s','%s','%s',%d,%d,%d,%d,%s)" % (
        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10],
        str(r[11]).lower()) for r in orows))
N_ORD = oid - 1

t("DROP TABLE IF EXISTS order_items")
t("""CREATE TABLE order_items (item_id INTEGER PRIMARY KEY, order_id INTEGER,
  product_id INTEGER, quantity INTEGER, unit_price_cents INTEGER,
  currency VARCHAR, created_at TIMESTAMPTZ)""")
irows, iid = [], 1
for o in range(1, N_ORD + 1):
    for _ in range(rng.randint(1, 3)):
        p = rng.randint(1, 60)
        irows.append((iid, o, p, rng.randint(1, 4), rng.randint(499, 49999),
                      rng.choice(CURRENCIES), ts(rng.randint(0, 480))))
        iid += 1
t("INSERT INTO order_items VALUES " + ",".join(
    "(%d,%d,%d,%d,%d,'%s','%s')" % r for r in irows))

t("DROP TABLE IF EXISTS payments")
t("""CREATE TABLE payments (payment_id INTEGER PRIMARY KEY, order_id INTEGER,
  customer_id INTEGER, paid_at TIMESTAMPTZ, created_at TIMESTAMPTZ,
  amount_cents INTEGER, currency VARCHAR, status VARCHAR, method VARCHAR)""")
prows2 = []
for o in range(1, N_ORD + 1):
    if rng.random() < 0.12:
        continue  # unpaid orders exist
    n = 2 if rng.random() < 0.06 else 1  # split payments
    for k in range(n):
        prows2.append((len(prows2) + 1, o, rng.randint(1, N_CUST), ts(rng.randint(0, 480)),
                       ts(rng.randint(0, 480)), rng.randint(1000, 90000),
                       rng.choice(CURRENCIES), rng.choice(PAYMENT_STATUSES),
                       rng.choice(["card", "ach", "paypal", "apple_pay"])))
t("INSERT INTO payments VALUES " + ",".join(
    "(%d,%d,%d,'%s','%s',%d,'%s','%s','%s')" % r for r in prows2))

# Refunds: customer_id nullable
t("DROP TABLE IF EXISTS refunds")
t("""CREATE TABLE refunds (refund_id INTEGER PRIMARY KEY, order_id INTEGER,
  payment_id INTEGER, customer_id INTEGER, refunded_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ, amount_cents INTEGER, currency VARCHAR,
  status VARCHAR, reason VARCHAR)""")
rrows = []
_refunded_so_far = {}
for p in range(1, len(prows2) + 1):
    if rng.random() < 0.10:
        nocust = rng.random() < 0.35  # anonymous refund: NULL customer_id
        oid_r = rng.randint(1, N_ORD)
        # A refund carries its order's currency, with at most 3
        # cross-currency exceptions (applied below by refund position).
        _o = orows[oid_r - 1]
        _total = _o[7] - _o[8] + _o[9] + _o[10]  # subtotal-disc+tax+ship
        # Cumulative refunds per order stay under 90% of the order total,
        # so FX-day wobble can never push the order's net below zero.
        _room = int(_total * 0.9) - _refunded_so_far.get(oid_r, 0)
        if _room < 500:
            continue  # order already refunded to the cap; no new refund
        _amt = rng.randint(500, _room)
        _refunded_so_far[oid_r] = _refunded_so_far.get(oid_r, 0) + _amt
        rrows.append([len(rrows) + 1, oid_r, p,
                      None if nocust else rng.randint(1, N_CUST),
                      ts(rng.randint(10, 490)), ts(rng.randint(10, 490)),
                      _amt, _o[6],
                      rng.choice(["processed", "pending", "failed"]),
                      rng.choice(["return", "damaged", "duplicate", "goodwill"])])
_EXC_POS = {0, len(rrows) // 2, len(rrows) - 1}  # 3 cross-currency exceptions
for _i, _r in enumerate(rrows):
    if _i in _EXC_POS:
        _r[7] = next(c for c in CURRENCIES if c != _r[7])
rrows = [tuple(r) for r in rrows]
t("INSERT INTO refunds VALUES " + ",".join(
    "(%d,%d,%d,%s,'%s','%s',%d,'%s','%s','%s')" % (
        r[0], r[1], r[2], str(r[3]) if r[3] else "NULL", r[4], r[5], r[6],
        r[7], r[8], r[9]) for r in rrows))

t("DROP TABLE IF EXISTS subscriptions")
t("""CREATE TABLE subscriptions (subscription_id INTEGER PRIMARY KEY, customer_id INTEGER,
  plan_id INTEGER, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, status VARCHAR,
  mrr_cents INTEGER, currency VARCHAR, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ)""")
srows = []
for s in range(1, 301):
    cid = rng.randint(1, N_CUST)
    st = rng.choice(SUB_STATUSES)
    start = rng.randint(0, 400)
    end = None if st.lower() in ("active", "trialing", "past_due", "paused") else start + rng.randint(30, 200)
    dl = ts(rng.randint(300, 480)) if rng.random() < 0.05 else None
    srows.append((s, cid, rng.randint(1, 4), ts(start),
                  ts(end) if end else None, st, rng.choice([0, 1200, 4900, 4500]),
                  rng.choice(CURRENCIES), ts(start), ts(start + 1), dl))
t("INSERT INTO subscriptions VALUES " + ",".join(
    "(%d,%d,%d,'%s',%s,'%s',%d,'%s','%s','%s',%s)" % (
        r[0], r[1], r[2], r[3], ("'%s'" % r[4]) if r[4] else "NULL", r[5], r[6],
        r[7], r[8], r[9], ("'%s'" % r[10]) if r[10] else "NULL") for r in srows))

t("DROP TABLE IF EXISTS subscription_events")
t("""CREATE TABLE subscription_events (event_id INTEGER PRIMARY KEY, subscription_id INTEGER,
  event_type VARCHAR, occurred_at TIMESTAMPTZ, created_at TIMESTAMPTZ, meta VARCHAR)""")
t("INSERT INTO subscription_events SELECT row_number() OVER "
  "(ORDER BY subscription_id, event_type, occurred_at) AS event_id, s.* FROM "
  "(SELECT subscription_id, 'created' AS event_type, started_at AS occurred_at, created_at, '{}' AS meta "
  "FROM subscriptions UNION ALL SELECT subscription_id, 'ended', ended_at, updated_at, '{}' "
  "FROM subscriptions WHERE ended_at IS NOT NULL) s")

t("DROP TABLE IF EXISTS marketing_campaigns")
t("""CREATE TABLE marketing_campaigns (campaign_id INTEGER PRIMARY KEY, name VARCHAR,
  channel VARCHAR, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, status VARCHAR)""")
t("""INSERT INTO marketing_campaigns VALUES (1,'Spring Launch','paid_search','2024-02-01 00:00:00+00','2024-04-30 00:00:00+00','ended'),
  (2,'Summer Push','social','2024-05-01 00:00:00+00','2024-08-31 00:00:00+00','ended'),
  (3,'Holiday Drive','email','2024-11-01 00:00:00+00','2024-12-31 00:00:00+00','live'),
  (4,'Legacy Blast','display','2023-01-01 00:00:00+00','2023-06-30 00:00:00+00','archived')""")

t("DROP TABLE IF EXISTS marketing_spend")
t("""CREATE TABLE marketing_spend (spend_id INTEGER PRIMARY KEY, campaign_id INTEGER,
  spend_date DATE, amount_cents INTEGER, currency VARCHAR)""")
sp = []
for d in range(0, 480):
    if rng.random() < 0.5:
        sp.append((len(sp) + 1, rng.randint(1, 4),
                   (dt.date(2024, 1, 1) + dt.timedelta(days=d)).isoformat(),
                   rng.randint(5000, 80000), rng.choice(CURRENCIES)))
t("INSERT INTO marketing_spend VALUES " + ",".join(
    "(%d,%d,'%s',%d,'%s')" % r for r in sp))

t("DROP TABLE IF EXISTS sessions")
t("""CREATE TABLE sessions (session_id INTEGER PRIMARY KEY, customer_id INTEGER,
  started_at TIMESTAMPTZ, channel VARCHAR, campaign_id INTEGER, is_bot BOOLEAN)""")
se = []
for s in range(1, 2501):
    nocust = rng.random() < 0.3  # anonymous sessions
    se.append((s, None if nocust else rng.randint(1, N_CUST), ts(rng.randint(0, 480), rng.randint(0, 86399)),
               rng.choice(["organic", "paid_search", "social", "email", "referral"]),
               rng.choice([None, 1, 2, 3, 4]), rng.random() < 0.04))
t("INSERT INTO sessions VALUES " + ",".join(
    "(%d,%s,'%s','%s',%s,%s)" % (
        r[0], str(r[1]) if r[1] else "NULL", r[2], r[3],
        str(r[4]) if r[4] else "NULL", str(r[5]).lower()) for r in se))

t("DROP TABLE IF EXISTS pageviews")
t("""CREATE TABLE pageviews (view_id INTEGER PRIMARY KEY, session_id INTEGER,
  viewed_at TIMESTAMPTZ, url_path VARCHAR)""")
pv = []
for s in range(1, 2501):
    for _ in range(rng.randint(1, 6)):
        pv.append((len(pv) + 1, s, ts(rng.randint(0, 480)),
                   rng.choice(["/", "/pricing", "/product", "/checkout", "/blog", "/support"])))
t("INSERT INTO pageviews VALUES " + ",".join(
    "(%d,%d,'%s','%s')" % r for r in pv))

t("DROP TABLE IF EXISTS support_tickets")
t("""CREATE TABLE support_tickets (ticket_id INTEGER PRIMARY KEY, customer_id INTEGER,
  opened_at TIMESTAMPTZ, closed_at TIMESTAMPTZ, status VARCHAR, priority VARCHAR,
  channel VARCHAR, deleted_at TIMESTAMPTZ)""")
tk = []
for i in range(1, 501):
    nocust = rng.random() < 0.12
    o = rng.randint(0, 470)
    closed = None if rng.random() < 0.25 else ts(o + rng.randint(1, 20))
    dl = ts(rng.randint(300, 480)) if rng.random() < 0.04 else None
    tk.append((i, None if nocust else rng.randint(1, N_CUST), ts(o), closed,
               rng.choice(["open", "pending", "resolved", "closed", "spam"]),
               rng.choice(["low", "medium", "high", "urgent"]),
               rng.choice(["email", "chat", "phone"]), dl))
t("INSERT INTO support_tickets VALUES " + ",".join(
    "(%d,%s,'%s',%s,'%s','%s','%s',%s)" % (
        r[0], str(r[1]) if r[1] else "NULL", r[2],
        ("'%s'" % r[3]) if r[3] else "NULL", r[4], r[5], r[6],
        ("'%s'" % r[7]) if r[7] else "NULL") for r in tk))

t("DROP TABLE IF EXISTS ticket_messages")
t("""CREATE TABLE ticket_messages (message_id INTEGER PRIMARY KEY, ticket_id INTEGER,
  sent_at TIMESTAMPTZ, sender VARCHAR, body VARCHAR)""")
tm = []
for i in range(1, 501):
    for _ in range(rng.randint(1, 4)):
        tm.append((len(tm) + 1, i, ts(rng.randint(0, 480)),
                   rng.choice(["customer", "agent", "system"]), "message body placeholder"))
t("INSERT INTO ticket_messages VALUES " + ",".join(
    "(%d,%d,'%s','%s','%s')" % r for r in tm))

t("DROP TABLE IF EXISTS reviews")
t("""CREATE TABLE reviews (review_id INTEGER PRIMARY KEY, order_id INTEGER, customer_id INTEGER,
  product_id INTEGER, rated_at TIMESTAMPTZ, stars INTEGER, deleted_at TIMESTAMPTZ)""")
rv = []
for i in range(1, 401):
    dl = ts(rng.randint(300, 480)) if rng.random() < 0.08 else None
    rv.append((i, rng.randint(1, N_ORD), rng.randint(1, N_CUST), rng.randint(1, 60),
               ts(rng.randint(5, 480)), rng.randint(1, 5), dl))
t("INSERT INTO reviews VALUES " + ",".join(
    "(%d,%d,%d,%d,'%s',%d,%s)" % (
        r[0], r[1], r[2], r[3], r[4], r[5],
        ("'%s'" % r[6]) if r[6] else "NULL") for r in rv))

t("DROP TABLE IF EXISTS coupons")
t("CREATE TABLE coupons (coupon_id INTEGER PRIMARY KEY, code VARCHAR, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, pct_off INTEGER)")
t("""INSERT INTO coupons VALUES (1,'SAVE10','2024-01-01 00:00:00+00','2024-12-31 00:00:00+00',10),
  (2,'LEGACY20','2022-01-01 00:00:00+00','2022-12-31 00:00:00+00',20),(3,'WELCOME5','2024-06-01 00:00:00+00','2025-06-01 00:00:00+00',5)""")
t("DROP TABLE IF EXISTS order_coupons")
t("CREATE TABLE order_coupons (order_id INTEGER, coupon_id INTEGER, applied_at TIMESTAMPTZ)")
oc = [(o, rng.choice([1, 2, 3]), ts(rng.randint(0, 480)))
      for o in rng.sample(range(1, N_ORD + 1), N_ORD // 4)]
t("INSERT INTO order_coupons VALUES " + ",".join(
    "(%d,%d,'%s')" % r for r in oc))

t("DROP TABLE IF EXISTS fx_rates")
t("CREATE TABLE fx_rates (rate_date DATE, currency VARCHAR, rate_to_usd DOUBLE)")
fx = []
for d in range(0, 500):
    day = (dt.date(2024, 1, 1) + dt.timedelta(days=d)).isoformat()
    for cur, base in FX_TO_USD.items():
        wob = 1 + rng.uniform(-0.02, 0.02)
        fx.append((day, cur, round(base * wob, 6)))
t("INSERT INTO fx_rates VALUES " + ",".join(
    "('%s','%s',%f)" % r for r in fx))

t("DROP TABLE IF EXISTS addresses")
t("""CREATE TABLE addresses (address_id INTEGER PRIMARY KEY, customer_id INTEGER,
  type VARCHAR, country VARCHAR, created_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ)""")
ad = []
for i in range(1, 801):
    dl = ts(rng.randint(300, 480)) if rng.random() < 0.1 else None
    ad.append((i, rng.randint(1, N_CUST), rng.choice(["ship", "bill"]),
               rng.choice(["US", "DE", "GB", "JP"]), ts(rng.randint(0, 400)), dl))
t("INSERT INTO addresses VALUES " + ",".join(
    "(%d,%d,'%s','%s','%s',%s)" % (
        r[0], r[1], r[2], r[3], r[4],
        ("'%s'" % r[5]) if r[5] else "NULL") for r in ad))

t("DROP TABLE IF EXISTS inventory_snapshots")
t("CREATE TABLE inventory_snapshots (product_id INTEGER, snapshot_date DATE, on_hand INTEGER)")
inv = [(p, (dt.date(2024, 1, 1) + dt.timedelta(days=d)).isoformat(), rng.randint(0, 500))
       for p in range(1, 61) for d in range(0, 480, 30)]
t("INSERT INTO inventory_snapshots VALUES " + ",".join(
    "(%d,'%s',%d)" % r for r in inv))

t("DROP TABLE IF EXISTS employees")
t("CREATE TABLE employees (employee_id INTEGER PRIMARY KEY, email VARCHAR, is_internal BOOLEAN, created_at TIMESTAMPTZ)")
t("""INSERT INTO employees VALUES (1,'ops@internal.corp',true,'2023-01-01 00:00:00+00'),
  (2,'analyst@internal.corp',true,'2023-06-01 00:00:00+00'),(3,'vendor@partner.io',false,'2024-01-01 00:00:00+00')""")

print("tables:", t("SHOW TABLES").fetchall())
print("orders:", t("SELECT count(*) FROM orders").fetchone())
print("null-customer refunds:", t("SELECT count(*) FROM refunds WHERE customer_id IS NULL").fetchone())
print("customers_history rows:", t("SELECT count(*) FROM customers_history").fetchone())
print("ordered_at ties:", t("SELECT count(*) FROM (SELECT ordered_at FROM orders GROUP BY 1 HAVING count(*)>1)").fetchone())
con.close()
print("wrote", DB)

