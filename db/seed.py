"""Generate ~2 years of synthetic retail sales with trend + weekly/yearly seasonality.

Deterministic (fixed RNG seed) so evals can compare against stable numbers.
Run:  uv run python db/seed.py [--force]
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from common.config import get_settings  # noqa: E402

START = date(2024, 10, 1)
END = date(2026, 9, 30)

REGIONS = {1: "East", 2: "West", 3: "North", 4: "South"}
CITIES = {
    1: ["Boston", "New York", "Philadelphia", "Baltimore", "Hartford"],
    2: ["Seattle", "Portland", "San Francisco", "Los Angeles", "San Diego"],
    3: ["Chicago", "Minneapolis", "Detroit", "Milwaukee", "Cleveland"],
    4: ["Atlanta", "Miami", "Houston", "Dallas", "Nashville"],
}
REGION_SIZE = {1: 1.2, 2: 1.1, 3: 0.8, 4: 0.9}
CATEGORIES = {
    "Electronics": (["Headphones", "Speaker", "Charger", "Smartwatch", "Tablet", "Webcam", "Keyboard", "Mouse"], (25, 400)),
    "Home": (["Lamp", "Kettle", "Blender", "Towel Set", "Cookware", "Vacuum", "Pillow", "Rug"], (15, 250)),
    "Apparel": (["T-Shirt", "Jeans", "Jacket", "Sneakers", "Hoodie", "Socks", "Cap", "Scarf"], (8, 150)),
    "Grocery": (["Coffee", "Tea", "Olive Oil", "Pasta", "Chocolate", "Granola", "Honey", "Spices"], (3, 30)),
    "Toys": (["Puzzle", "Board Game", "Lego Set", "Doll", "RC Car", "Plush", "Kite", "Art Kit"], (10, 120)),
}
# Yearly peak month per category (Toys/Electronics peak in December, Apparel in autumn, etc.)
CATEGORY_PEAK = {"Electronics": 12, "Home": 11, "Apparel": 10, "Grocery": 12, "Toys": 12}


def build_dimensions(rng: np.random.Generator):
    stores, products, customers = [], [], []
    sid = 1
    for rid, cities in CITIES.items():
        for city in cities:
            stores.append((sid, f"{city} Store", city, rid))
            sid += 1
    pid = 1
    for cat, (names, (lo, hi)) in CATEGORIES.items():
        for n in names:
            products.append((pid, n, cat, round(float(rng.uniform(lo, hi)), 2)))
            pid += 1
    first = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Ethan", "Ivy", "Lucas", "Nora", "Owen", "Ruby", "Leo"]
    last = ["Smith", "Garcia", "Chen", "Patel", "Kim", "Brown", "Lopez", "Nguyen", "Khan", "Silva"]
    for cid in range(1, 2001):
        fn, ln = rng.choice(first), rng.choice(last)
        customers.append((cid, f"{fn} {ln}", f"{fn.lower()}.{ln.lower()}{cid}@example.com", int(rng.integers(1, 5))))
    return stores, products, customers


def build_sales(rng, stores, products, customers):
    cust_by_region: dict[int, np.ndarray] = {}
    for cid, _, _, rid in customers:
        cust_by_region.setdefault(rid, []).append(cid)  # type: ignore[arg-type]
    cust_by_region = {k: np.array(v) for k, v in cust_by_region.items()}

    prod_ids = np.array([p[0] for p in products])
    prod_price = {p[0]: p[3] for p in products}
    prod_cat = {p[0]: p[2] for p in products}

    days = (END - START).days + 1
    order_id = 1
    buf = io.StringIO()
    for d in range(days):
        day = START + timedelta(days=d)
        trend = 1.0 + 0.25 * d / days  # +25% over two years
        weekly = 1.25 if day.weekday() >= 5 else (0.9 if day.weekday() == 0 else 1.0)
        for sid, _, _, rid in stores:
            lam = 14 * REGION_SIZE[rid] * trend * weekly
            n_orders = rng.poisson(lam)
            for _ in range(n_orders):
                cust = int(rng.choice(cust_by_region[rid]))
                for pid in rng.choice(prod_ids, size=int(rng.integers(1, 4)), replace=False):
                    pid = int(pid)
                    # yearly seasonality: boost near the category's peak month
                    dist = min(abs(day.month - CATEGORY_PEAK[prod_cat[pid]]), 12 - abs(day.month - CATEGORY_PEAK[prod_cat[pid]]))
                    if rng.random() > 0.55 + 0.45 * np.exp(-(dist**2) / 4):
                        continue
                    qty = int(rng.integers(1, 4))
                    rev = round(qty * prod_price[pid] * float(rng.uniform(0.85, 1.0)), 2)
                    buf.write(f"{order_id}\t{day.isoformat()}\t{sid}\t{rid}\t{pid}\t{cust}\t{qty}\t{rev}\n")
                order_id += 1
    buf.seek(0)
    return buf


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="truncate and reseed")
    args = ap.parse_args()

    with psycopg.connect(get_settings().pg_admin_dsn) as conn:
        n = conn.execute("SELECT count(*) FROM analytics.sales").fetchone()[0]
        if n and not args.force:
            print(f"already seeded ({n} sales rows); use --force to reseed")
            return
        conn.execute("TRUNCATE analytics.sales, analytics.customers, analytics.products, analytics.stores, analytics.regions CASCADE")

        rng = np.random.default_rng(42)
        stores, products, customers = build_dimensions(rng)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO analytics.regions VALUES (%s, %s)", list(REGIONS.items()))
            cur.executemany("INSERT INTO analytics.stores VALUES (%s, %s, %s, %s)", stores)
            cur.executemany("INSERT INTO analytics.products VALUES (%s, %s, %s, %s)", products)
            cur.executemany("INSERT INTO analytics.customers VALUES (%s, %s, %s, %s)", customers)
            sales = build_sales(rng, stores, products, customers)
            with cur.copy(
                "COPY analytics.sales (order_id, sale_date, store_id, region_id, product_id, customer_id, qty, revenue) FROM STDIN"
            ) as cp:
                while chunk := sales.read(1 << 20):
                    cp.write(chunk)
        conn.execute("ANALYZE analytics.sales")
        n = conn.execute("SELECT count(*) FROM analytics.sales").fetchone()[0]
        print(f"seeded {len(stores)} stores, {len(products)} products, {len(customers)} customers, {n} sales rows")


if __name__ == "__main__":
    main()
