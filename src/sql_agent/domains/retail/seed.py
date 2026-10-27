"""Deterministic synthetic retail dataset.

Everything here is invented: names are random pairings from short lists,
emails use the reserved ``example.com`` domain, phone numbers sit in the
fictional 555-01xx block. Same seed, same database, every run — the eval
reference answers depend on it.

Shape (defaults): 16 sales reps, 60 products, 5,000 customers, 50,000
orders over 2025-01-01 .. 2026-09-30, ~125,000 order lines.

The mess is deliberate, because real warehouses are messy:

- ``orders.order_date`` vs ``orders.created_at`` — the record is keyed 0-4
  days after the business date, so month boundaries differ between the two.
- ``status = 'cancelled'`` must be excluded from revenue (the glossary
  says so; a naive SUM does not).
- 40 customers moved region mid-2025: their older orders carry the old
  region, ``customers.region`` holds the new one.
- Product names repeat across categories ("Classic Tee" is both Apparel
  and Kids) — grouping by name double-counts.
"""

from __future__ import annotations

import io
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import psycopg

SEED = 20260930
START = date(2025, 1, 1)
END = date(2026, 9, 30)

REGIONS = {
    "North": [("Chicago", "606"), ("Minneapolis", "554"), ("Detroit", "482"), ("Milwaukee", "532"), ("Columbus", "432")],
    "South": [("Dallas", "752"), ("Houston", "770"), ("Atlanta", "303"), ("Nashville", "372"), ("Miami", "331")],
    "East": [("Boston", "021"), ("New York", "100"), ("Philadelphia", "191"), ("Baltimore", "212"), ("Pittsburgh", "152")],
    "West": [("Seattle", "981"), ("Portland", "972"), ("Denver", "802"), ("Phoenix", "850"), ("San Diego", "921")],
}
REGION_WEIGHTS = {"North": 0.22, "South": 0.28, "East": 0.27, "West": 0.23}
SEGMENTS = [("Consumer", 0.70), ("Small Business", 0.22), ("Enterprise", 0.08)]

FIRST = [
    "Aiden", "Amara", "Anika", "Ben", "Bianca", "Caleb", "Chloe", "Daniel", "Diya", "Elena", "Ethan", "Farah",
    "Felix", "Grace", "Hana", "Hugo", "Isla", "Ivan", "Jade", "Jonah", "Kai", "Keira", "Leo", "Lila", "Mason",
    "Maya", "Nadia", "Noah", "Olivia", "Omar", "Paula", "Quinn", "Rafael", "Riya", "Sara", "Theo", "Uma",
    "Victor", "Wren", "Xavier", "Yara", "Zane", "Alma", "Bruno", "Cora", "Dante", "Esme", "Gus", "Iris", "Jules",
]
LAST = [
    "Abbott", "Banks", "Castillo", "Dorsey", "Ellison", "Fontaine", "Garrity", "Holloway", "Ibarra", "Jensen",
    "Kowalski", "Lindqvist", "Marlowe", "Nakamura", "Oduya", "Pellegrino", "Quintero", "Rasmussen", "Sandoval",
    "Thorne", "Underwood", "Valdez", "Whitlock", "Yilmaz", "Zielinski", "Ashworth", "Brennan", "Calloway",
    "Delacroix", "Esposito", "Falkner", "Greer", "Hartigan", "Iverson", "Juarez", "Kessler", "Lombardi",
    "Mercado", "Northcott", "Okonkwo",
]
STREETS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Birch", "Willow", "Lakeview", "Hillcrest", "Riverside",
           "Sunset", "Park", "Highland", "Meadow", "Franklin", "Jefferson", "Harbor", "Orchard"]
SUFFIX = ["St", "Ave", "Rd", "Ln", "Blvd", "Way", "Ct"]

PRODUCTS = {
    "Apparel": [("Classic Tee", 18), ("Hooded Sweatshirt", 48), ("Denim Jacket", 89), ("Rain Shell", 120),
                ("Wool Beanie", 22), ("Chino Pants", 58), ("Linen Shirt", 64), ("Puffer Vest", 95)],
    "Footwear": [("Trail Runner", 130), ("Canvas Sneaker", 55), ("Leather Boot", 165), ("Slide Sandal", 30),
                 ("Court Shoe", 85), ("Hiking Boot", 180), ("Wool Slipper", 45)],
    "Home": [("Desk Lamp", 42), ("Throw Blanket", 60), ("Ceramic Vase", 35), ("Wall Clock", 28),
             ("Linen Duvet", 150), ("Bath Towel Set", 48), ("Scented Candle", 19), ("Floor Cushion", 54)],
    "Kitchen": [("Travel Mug", 24), ("Chef Knife", 75), ("Cast Iron Pan", 65), ("Pour-Over Kettle", 58),
                ("Spice Rack", 32), ("Salad Bowl", 29), ("Knife Block", 89), ("Espresso Cups", 26)],
    "Electronics": [("Desk Lamp", 79), ("Wireless Earbuds", 129), ("Bluetooth Speaker", 99), ("USB-C Hub", 49),
                    ("Smart Plug", 25), ("Power Bank", 45), ("Webcam HD", 69)],
    "Outdoor": [("Travel Mug", 32), ("Camping Lantern", 38), ("Daypack 20L", 85), ("Water Filter", 42),
                ("Folding Chair", 55), ("Sleeping Pad", 99), ("Trekking Poles", 79), ("Picnic Blanket", 36)],
    "Beauty": [("Face Serum", 46), ("Hand Cream", 14), ("Shampoo Bar", 12), ("Lip Balm Set", 10),
               ("Body Lotion", 18), ("Clay Mask", 24), ("Sunscreen SPF50", 21)],
    "Kids": [("Classic Tee", 14), ("Building Blocks", 45), ("Puzzle 500pc", 22), ("Plush Bear", 26),
             ("Art Set", 34), ("Kids Raincoat", 48), ("Board Game", 38)],
}
CATEGORY_WEIGHTS = {"Apparel": 0.20, "Footwear": 0.12, "Home": 0.14, "Kitchen": 0.14, "Electronics": 0.13,
                    "Outdoor": 0.11, "Beauty": 0.09, "Kids": 0.07}


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pick(rng: random.Random, weighted: list[tuple[str, float]]) -> str:
    return rng.choices([k for k, _ in weighted], weights=[w for _, w in weighted], k=1)[0]


def generate(n_customers: int = 5000, n_orders: int = 50000, seed: int = SEED) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    regions = list(REGIONS)

    reps: list[tuple] = []
    rep_by_region: dict[str, list[str]] = {}
    for region in regions:
        for i in range(1, 5):
            rep_id = f"R-{region[0]}-{i:02d}"
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            reps.append((rep_id, name, f"{name.split()[0].lower()}.{name.split()[1].lower()}@corp.example.com", region))
            rep_by_region.setdefault(region, []).append(rep_id)

    products: list[tuple] = []
    by_category: dict[str, list[tuple[int, Decimal]]] = {}
    pid = 100
    for category, items in PRODUCTS.items():
        for name, price in items:
            pid += 1
            products.append((pid, name, category, _money(price)))
            by_category.setdefault(category, []).append((pid, _money(price)))

    customers: list[tuple] = []
    cust_region: dict[int, str] = {}
    cust_created: dict[int, date] = {}
    used_emails: set[str] = set()
    for cid in range(1, n_customers + 1):
        region = _pick(rng, list(REGION_WEIGHTS.items()))
        city, zip3 = rng.choice(REGIONS[region])
        first, last = rng.choice(FIRST), rng.choice(LAST)
        base = f"{first.lower()}.{last.lower()}"
        email = f"{base}@example.com"
        n = 2
        while email in used_emails:
            email = f"{base}{n}@example.com"
            n += 1
        used_emails.add(email)
        phone = f"+1-{rng.randint(201, 989)}-555-01{rng.randint(0, 99):02d}"
        street = f"{rng.randint(10, 9899)} {rng.choice(STREETS)} {rng.choice(SUFFIX)}"
        postal = f"{zip3}{rng.randint(0, 99):02d}"
        dob = date(rng.randint(1950, 2004), rng.randint(1, 12), rng.randint(1, 28))
        segment = _pick(rng, SEGMENTS)
        rep = rng.choice(rep_by_region[region])
        created = date(2023, 1, 1) + timedelta(days=rng.randint(0, (END - date(2023, 1, 1)).days))
        created_ts = datetime(created.year, created.month, created.day, rng.randint(8, 20), rng.randint(0, 59), tzinfo=timezone.utc)
        customers.append([cid, f"{first} {last}", email, phone, street, city, postal, dob, region, segment, rep, created_ts])
        cust_region[cid] = region
        cust_created[cid] = created

    # 40 customers moved region on 2025-07-01: orders before carry the old region.
    movers: dict[int, tuple[str, date]] = {}
    for cid in rng.sample(range(1, n_customers + 1), 40):
        old = cust_region[cid]
        new = rng.choice([r for r in regions if r != old])
        city, zip3 = rng.choice(REGIONS[new])
        row = customers[cid - 1]
        row[5], row[6], row[8] = city, f"{zip3}{rng.randint(0, 99):02d}", new
        row[10] = rng.choice(rep_by_region[new])
        movers[cid] = (old, date(2025, 7, 1))
        cust_region[cid] = new

    # Customer purchase propensity: a long tail (a few customers order a lot).
    weights = [rng.paretovariate(1.6) for _ in range(n_customers)]
    span = (END - START).days

    orders: list[tuple] = []
    items: list[tuple] = []
    item_id = 0
    cat_weights = list(CATEGORY_WEIGHTS.items())
    cids = list(range(1, n_customers + 1))
    for oid in range(1, n_orders + 1):
        # Mild growth + Q4 seasonality.
        while True:
            d = START + timedelta(days=rng.randint(0, span))
            boost = 1.0 + (0.35 if d.month in (11, 12) else 0.0) + (d - START).days / span * 0.25
            if rng.random() < boost / 1.6:
                break
        cid = rng.choices(cids, weights=weights, k=1)[0]
        if cust_created[cid] > d:
            d = max(d, cust_created[cid])
            if d > END:
                d = END
        region = cust_region[cid]
        if cid in movers and d < movers[cid][1]:
            region = movers[cid][0]
        age = (END - d).days
        if age <= 3:
            status = rng.choices(["processing", "shipped", "cancelled"], [0.6, 0.35, 0.05])[0]
        elif age <= 12:
            status = rng.choices(["shipped", "completed", "cancelled"], [0.5, 0.43, 0.07])[0]
        else:
            status = rng.choices(["completed", "cancelled"], [0.92, 0.08])[0]
        lag = rng.choices([0, 1, 2, 3, 4], [0.55, 0.25, 0.1, 0.06, 0.04])[0]
        created = datetime(d.year, d.month, d.day, rng.randint(9, 22), rng.randint(0, 59), tzinfo=timezone.utc) + timedelta(days=lag)
        total = Decimal("0.00")
        for _ in range(rng.choices([1, 2, 3, 4], [0.30, 0.35, 0.22, 0.13])[0]):
            category = _pick(rng, cat_weights)
            product_id, list_price = rng.choice(by_category[category])
            qty = rng.choices([1, 2, 3], [0.72, 0.21, 0.07])[0]
            unit = _money(list_price * Decimal(str(rng.choice([1.0, 1.0, 1.0, 0.95, 0.9, 0.85]))))
            item_id += 1
            items.append((item_id, oid, product_id, qty, unit))
            total += unit * qty
        orders.append((oid, cid, region, d, created, status, _money(total)))

    return {
        "sales_reps": reps,
        "products": products,
        "customers": [tuple(c) for c in customers],
        "orders": orders,
        "order_items": items,
    }


_COLUMNS = {
    "sales_reps": "rep_id, rep_name, rep_email, region",
    "products": "product_id, name, category, list_price",
    "customers": "customer_id, full_name, email, phone, street_address, city, postal_code, date_of_birth, region, segment, assigned_rep_id, created_at",
    "orders": "order_id, customer_id, region, order_date, created_at, status, total_amount",
    "order_items": "order_item_id, order_id, product_id, quantity, unit_price",
}


def load(conn: psycopg.Connection, **kwargs) -> dict[str, int]:
    """COPY the generated rows into core.* as the service role."""
    data = generate(**kwargs)
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in ("sales_reps", "products", "customers", "orders", "order_items"):
            rows = data[table]
            with cur.copy(f"COPY core.{table} ({_COLUMNS[table]}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            counts[table] = len(rows)
    return counts


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    data = generate()
    buf = io.StringIO()
    for k, v in data.items():
        print(k, len(v), file=buf)
    print(buf.getvalue())
