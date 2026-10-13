"""Demo e-commerce backend. All data is fake; secrets come from the environment."""
import logging
import os
import time

import psycopg
import redis
from flask import Flask, jsonify, request

LOG = logging.getLogger("shop")
logging.basicConfig(
    filename=os.environ.get("LOG_FILE", "/var/log/shop/app.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)
DB_DSN = os.environ["DATABASE_URL"]
cache = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (
  id SERIAL PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, price_cents INT NOT NULL);
"""
SEED_CUSTOMERS = [("alice@example.test", "Alice Demo", "active"), ("bob@example.test", "Bob Demo", "active")]
SEED_PRODUCTS = [("SKU-1001", "Demo Mug", 1299), ("SKU-1002", "Demo Shirt", 2499)]


def connect():
    for _ in range(30):
        try:
            return psycopg.connect(DB_DSN, autocommit=True)
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError("database unavailable")


def init_db() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        for row in SEED_CUSTOMERS:
            conn.execute("INSERT INTO customers(email, full_name, status) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", row)
        for row in SEED_PRODUCTS:
            conn.execute("INSERT INTO products(sku, name, price_cents) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", row)


init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/products")
def products():
    cached = cache.get("products")
    if cached:
        return app.response_class(cached, mimetype="application/json")
    with connect() as conn:
        rows = conn.execute("SELECT sku, name, price_cents FROM products ORDER BY id").fetchall()
    body = jsonify([{"sku": s, "name": n, "price_cents": p} for s, n, p in rows])
    cache.setex("products", 30, body.get_data(as_text=True))
    return body


@app.get("/api/customers")
def customers():
    if request.headers.get("X-Admin-Token") != os.environ["ADMIN_TOKEN"]:
        LOG.warning("denied customers list from %s", request.remote_addr)
        return {"error": "forbidden"}, 403
    with connect() as conn:
        rows = conn.execute("SELECT id, email, full_name, status FROM customers ORDER BY id").fetchall()
    return jsonify([{"id": i, "email": e, "full_name": f, "status": s} for i, e, f, s in rows])
