from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Currently-approved affiliate retailers. Target/Walmart are deliberately
# NOT here yet — save_product()/validate_retailer() reject them outright
# rather than just relying on nobody calling the site with those values, so
# "don't create Target/Walmart affiliate links" is enforced at the data
# layer, not just by convention. Add to this set (nowhere else) when
# they're actually approved — the product schema already has the columns
# (retailer, affiliate_source) to support it without a schema change.
APPROVED_RETAILERS = {"amazon", "tiktok_shop"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            category TEXT,
            price REAL,
            url TEXT,
            image_url TEXT,
            product_id TEXT,
            sales_signal REAL,
            demand REAL,
            margin REAL,
            trend_strength REAL,
            competition REAL,
            content_potential REAL,
            repeatability REAL,
            historical_performance REAL,
            affiliate_url TEXT,
            discovery_date TEXT,
            score REAL,
            approved INTEGER DEFAULT 0,
            notes TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            caption TEXT,
            hook TEXT,
            cta TEXT,
            hashtags TEXT,
            affiliate_url TEXT,
            media_path TEXT,
            buffer_status TEXT,
            created_at TEXT NOT NULL,
            published_at TEXT
        )
    """)
    # Additive-only migration — see actions/buildpro_data.py's
    # _ensure_columns for the same pattern. These support the full
    # revenue-chain data model: PRODUCT -> AFFILIATE URL -> WEBSITE PAGE ->
    # SOCIAL POST -> PLATFORM -> CLICK DATA -> COMMISSION/REVENUE, all on
    # one connected record instead of separate disconnected systems.
    _ensure_columns(conn, "products", {
        "description": "TEXT",
        "retailer": "TEXT",
        "original_price": "REAL",
        "current_price": "REAL",
        "discount_pct": "REAL",
        "affiliate_source": "TEXT",
        "date_posted": "TEXT",
        "social_platforms_posted": "TEXT",
        "views": "INTEGER DEFAULT 0",
        "affiliate_clicks": "INTEGER DEFAULT 0",
        "conversions": "INTEGER DEFAULT 0",
        "revenue": "REAL DEFAULT 0",
        "commission_rate": "REAL",
        "slug": "TEXT",
    })
    _ensure_columns(conn, "posts", {"clicks": "INTEGER DEFAULT 0"})
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, col_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def score_product(product: dict[str, Any]) -> float:
    sales_signal = float(product.get("sales_signal", 0) or 0)
    demand = float(product.get("demand", 0) or 0)
    margin = float(product.get("margin", 0) or 0)
    trend_strength = float(product.get("trend_strength", 0) or 0)
    competition = float(product.get("competition", 0) or 0)
    content_potential = float(product.get("content_potential", 0) or 0)
    repeatability = float(product.get("repeatability", 0) or 0)
    historical_performance = float(product.get("historical_performance", 0) or 0)
    price = float(product.get("price", 0) or 0)
    price_factor = max(0.0, 1.0 - min(price / 200.0, 1.0))
    competition_factor = max(0.0, 1.0 - competition)
    score = (
        sales_signal * 0.25
        + demand * 0.20
        + margin * 100 * 0.20
        + trend_strength * 100 * 0.18
        + competition_factor * 100 * 0.10
        + content_potential * 100 * 0.08
        + repeatability * 100 * 0.05
        + historical_performance * 100 * 0.03
        + price_factor * 100 * 0.01
    )
    return round(score, 2)


def is_duplicate(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    c_id = str(candidate.get("product_id") or "").strip().lower()
    e_id = str(existing.get("product_id") or "").strip().lower()
    if c_id and e_id and c_id == e_id:
        return True
    if not c_id and not e_id:
        return str(candidate.get("name", "")).strip().lower() == str(existing.get("name", "")).strip().lower()
    return False


def discover_product(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    product = {
        "name": payload.get("name", "Example Daily Deal"),
        "source": payload.get("source", "manual"),
        "category": payload.get("category", "gadgets"),
        "price": float(payload.get("price", 24.99) or 24.99),
        "url": payload.get("url", "https://example.com/deal"),
        "image_url": payload.get("image_url", ""),
        "product_id": payload.get("product_id", "manual-001"),
        "sales_signal": float(payload.get("sales_signal", 80) or 80),
        "demand": float(payload.get("demand", 75) or 75),
        "margin": float(payload.get("margin", 0.18) or 0.18),
        "trend_strength": float(payload.get("trend_strength", 0.7) or 0.7),
        "competition": float(payload.get("competition", 0.25) or 0.25),
        "content_potential": float(payload.get("content_potential", 0.8) or 0.8),
        "repeatability": float(payload.get("repeatability", 0.7) or 0.7),
        "historical_performance": float(payload.get("historical_performance", 0.6) or 0.6),
        "affiliate_url": payload.get("affiliate_url") or payload.get("url"),
        "discovery_date": payload.get("discovery_date") or datetime.now(timezone.utc).isoformat(),
    }
    product["score"] = score_product(product)
    return product


def validate_retailer(retailer: str | None) -> None:
    """Raises ValueError for Target/Walmart (or anything else not yet
    approved) rather than silently accepting it — enforcement, not just
    convention. None/empty is allowed (retailer not yet categorized)."""
    if not retailer:
        return
    if retailer.strip().lower() not in APPROVED_RETAILERS:
        raise ValueError(
            f"Retailer {retailer!r} is not approved yet. Approved: {sorted(APPROVED_RETAILERS)}. "
            "Target/Walmart are explicitly not approved — see APPROVED_RETAILERS."
        )


def _slugify(name: str, product_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    suffix = re.sub(r"[^a-z0-9]+", "-", (product_id or "").strip().lower()).strip("-")
    if base and suffix:
        return f"{base}-{suffix}"
    return base or suffix or f"deal-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"


def _find_duplicate_product_id(conn: sqlite3.Connection, candidate: dict[str, Any]) -> str | None:
    """Uses the existing (previously unused) is_duplicate() to reuse an
    existing row's product_id instead of inserting a redundant one — only
    when the caller didn't give an explicit product_id to key off of
    (an explicit id is already an unambiguous identity; this only covers
    the case is_duplicate() itself is built for: matching by name when
    neither side has one)."""
    if str(candidate.get("product_id") or "").strip():
        return None
    # is_duplicate()'s name-matching branch only fires when NEITHER side has
    # a product_id — a stored row always has one, so it must be omitted
    # here to compare on name the way is_duplicate() actually intends,
    # not the (always-populated, always-mismatching) product_id branch.
    rows = conn.execute("SELECT product_id, name FROM products").fetchall()
    for row in rows:
        if is_duplicate(candidate, {"name": row["name"]}):
            return row["product_id"]
    return None


def save_product(product: dict[str, Any]) -> dict[str, Any]:
    """Never fabricates a price, discount, or affiliate URL — every value
    persisted here is exactly what the caller passed in; the only derived
    fields are `score` (from score_product), `slug` (from name/product_id,
    only if not already given), and `discount_pct` (computed from
    original/current price only when both are real numbers — never guessed).

    Also de-duplicates by name via is_duplicate() when no explicit
    product_id was given, reusing the existing row instead of creating a
    near-identical second one (previously is_duplicate() existed and was
    unit-tested but nothing actually called it — this closes that gap)."""
    validate_retailer(product.get("retailer"))

    conn = _connect()
    product_id = str(product.get("product_id") or "").strip()
    if not product_id:
        # uuid suffix, not just a seconds-precision timestamp — same fix as
        # create_post's post_id already applies, for the same reason: two
        # different unnamed products saved within the same second would
        # otherwise collide on this PRIMARY KEY and silently overwrite.
        product_id = _find_duplicate_product_id(conn, product) or f"manual-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    product_record = dict(product)
    product_record["product_id"] = product_id
    product_record["score"] = score_product(product)
    product_record["discovery_date"] = product_record.get("discovery_date") or datetime.now(timezone.utc).isoformat()
    product_record["slug"] = product_record.get("slug") or _slugify(product_record.get("name", ""), product_id)

    original_price = product_record.get("original_price")
    current_price = product_record.get("current_price", product_record.get("price"))
    if product_record.get("discount_pct") is None and original_price and current_price and original_price > 0:
        product_record["discount_pct"] = round((1 - (float(current_price) / float(original_price))) * 100, 2)
    else:
        product_record.setdefault("discount_pct", None)

    social_platforms = product_record.get("social_platforms_posted")
    if isinstance(social_platforms, list):
        social_platforms = ",".join(social_platforms)

    conn.execute(
        """
        INSERT OR REPLACE INTO products (
            id, name, source, category, price, url, image_url, product_id, sales_signal,
            demand, margin, trend_strength, competition, content_potential, repeatability,
            historical_performance, affiliate_url, discovery_date, score, approved, notes,
            description, retailer, original_price, current_price, discount_pct,
            affiliate_source, date_posted, social_platforms_posted, views, affiliate_clicks,
            conversions, revenue, commission_rate, slug
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_record.get("id") or product_id,
            product_record.get("name"),
            product_record.get("source"),
            product_record.get("category"),
            product_record.get("price"),
            product_record.get("url"),
            product_record.get("image_url"),
            product_record.get("product_id"),
            product_record.get("sales_signal"),
            product_record.get("demand"),
            product_record.get("margin"),
            product_record.get("trend_strength"),
            product_record.get("competition"),
            product_record.get("content_potential"),
            product_record.get("repeatability"),
            product_record.get("historical_performance"),
            product_record.get("affiliate_url") or product_record.get("url"),
            product_record.get("discovery_date"),
            product_record.get("score"),
            1 if product_record.get("approved") else 0,
            json.dumps(product_record.get("notes", {})) if isinstance(product_record.get("notes"), dict) else product_record.get("notes"),
            product_record.get("description"),
            (product_record.get("retailer") or "").strip().lower() or None,
            product_record.get("original_price"),
            current_price,
            product_record.get("discount_pct"),
            product_record.get("affiliate_source"),
            product_record.get("date_posted"),
            social_platforms,
            int(product_record.get("views") or 0),
            int(product_record.get("affiliate_clicks") or 0),
            int(product_record.get("conversions") or 0),
            float(product_record.get("revenue") or 0),
            product_record.get("commission_rate"),
            product_record.get("slug"),
        ),
    )
    conn.commit()
    conn.close()
    return product_record


def prepare_post(asset: dict[str, Any]) -> dict[str, Any]:
    product = asset.get("product", {})
    copy_text = asset.get("copy") or "A fresh find worth a look."
    platform = (asset.get("platform") or "instagram").lower()
    caption = copy_text
    hook = copy_text.split(".")[0] if "." in copy_text else copy_text
    cta = "Tap the link in bio or the affiliate link below."
    hashtags = "#dealfinder #affiliate #dailyfinds"
    affiliate_url = product.get("url") or product.get("affiliate_url") or ""
    return {
        "platform": platform,
        "caption": caption,
        "hook": hook,
        "cta": cta,
        "hashtags": hashtags,
        "affiliate_url": affiliate_url,
    }


def create_post(asset: dict[str, Any]) -> dict[str, Any]:
    post = prepare_post(asset)
    conn = _connect()
    product_id = str(asset.get("product", {}).get("product_id") or "manual")
    # uuid suffix, not just a seconds-precision timestamp — two posts for
    # the same product on different platforms (a real, expected case: one
    # product, multiple social_platforms_posted) created within the same
    # second used to collide on this PRIMARY KEY.
    post_id = f"post-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO posts (id, product_id, platform, caption, hook, cta, hashtags, affiliate_url, media_path, buffer_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (post_id, product_id, post["platform"], post["caption"], post["hook"], post["cta"], post["hashtags"], post["affiliate_url"], asset.get("media_path"), "draft", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    # Keep the product's own social_platforms_posted / date_posted in sync
    # — one connected record, not a disconnected posts log.
    row = conn.execute("SELECT social_platforms_posted, date_posted FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if row is not None:
        existing_platforms = {p for p in (row["social_platforms_posted"] or "").split(",") if p}
        existing_platforms.add(post["platform"])
        conn.execute(
            "UPDATE products SET social_platforms_posted = ?, date_posted = COALESCE(date_posted, ?) WHERE product_id = ?",
            (",".join(sorted(existing_platforms)), datetime.now(timezone.utc).isoformat(), product_id),
        )
        conn.commit()
    conn.close()
    post["id"] = post_id
    return post


def get_top_products(limit: int = 5) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM products ORDER BY score DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ── website-facing catalog queries — never fabricate results, empty is honest ──

def get_product(product_id: str) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_product_by_slug(slug: str) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM products WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_products(
    category: str | None = None, retailer: str | None = None,
    approved_only: bool = True, limit: int = 50, order_by: str = "discovery_date",
) -> list[dict[str, Any]]:
    order_columns = {
        "discovery_date": "discovery_date DESC",
        "score": "score DESC",
        "price_asc": "current_price ASC",
        "price_desc": "current_price DESC",
        "views": "views DESC",
        "clicks": "affiliate_clicks DESC",
    }
    order_clause = order_columns.get(order_by, "discovery_date DESC")

    conn = _connect()
    q = "SELECT * FROM products WHERE 1=1"
    params: list[Any] = []
    if approved_only:
        q += " AND approved = 1"
    if category:
        q += " AND category = ?"
        params.append(category)
    if retailer:
        q += " AND retailer = ?"
        params.append(retailer.strip().lower())
    q += f" ORDER BY {order_clause} LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_todays_deals(limit: int = 20) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE approved = 1 AND discovery_date LIKE ? ORDER BY score DESC LIMIT ?",
        (f"{today}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trending_deals(limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE approved = 1 ORDER BY trend_strength DESC, views DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_best_sellers(limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE approved = 1 ORDER BY conversions DESC, historical_performance DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_high_ticket_deals(min_price: float = 100.0, limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE approved = 1 AND current_price >= ? ORDER BY current_price DESC LIMIT ?",
        (min_price, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_categories() -> list[str]:
    conn = _connect()
    rows = conn.execute(
        "SELECT DISTINCT category FROM products WHERE approved = 1 AND category IS NOT NULL ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


# ── revenue-chain tracking: view -> click -> conversion/revenue ──────────

def record_view(product_id: str) -> bool:
    conn = _connect()
    cur = conn.execute("UPDATE products SET views = views + 1 WHERE product_id = ?", (product_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def record_affiliate_click(product_id: str, platform: str | None = None) -> bool:
    """Increments the product's aggregate affiliate_clicks and, if this
    click can be attributed to a specific social post's platform, that
    post's own click count too — the connected PRODUCT -> ... -> CLICK DATA
    chain the data model is meant to support."""
    conn = _connect()
    cur = conn.execute("UPDATE products SET affiliate_clicks = affiliate_clicks + 1 WHERE product_id = ?", (product_id,))
    changed = cur.rowcount > 0
    if changed and platform:
        conn.execute(
            "UPDATE posts SET clicks = clicks + 1 WHERE product_id = ? AND platform = ? "
            "AND id = (SELECT id FROM posts WHERE product_id = ? AND platform = ? ORDER BY created_at DESC LIMIT 1)",
            (product_id, platform, product_id, platform),
        )
    conn.commit()
    conn.close()
    return changed


def record_conversion(product_id: str, revenue_amount: float) -> bool:
    """Only ever call this with a real, confirmed conversion amount from an
    actual affiliate network report — never estimated/fabricated. Not
    invoked automatically anywhere in this codebase; exists so a future
    affiliate-network webhook/import can wire real data through it."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE products SET conversions = conversions + 1, revenue = revenue + ? WHERE product_id = ?",
        (revenue_amount, product_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def get_product_posts(product_id: str) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM posts WHERE product_id = ? ORDER BY created_at DESC", (product_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
