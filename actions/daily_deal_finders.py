from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Product lifecycle. `approved` (the original 0/1 gate list_products() and
# ddf_site already filter on) stays as a simple derived flag so none of
# that existing code has to change — it's just True for PUBLISHED and
# everything after. `status` is the real state, and the only thing
# set_product_status() lets a caller move it through in order; you can't
# jump from DISCOVERED straight to WINNER by mistake.
STATUS_DISCOVERED = "discovered"
STATUS_EVALUATED = "evaluated"
STATUS_SCORED = "scored"
STATUS_APPROVED = "approved_pending_publish"
STATUS_PUBLISHED = "published"
STATUS_TRACKING = "tracking"
STATUS_WINNER = "winner"
STATUS_UNDERPERFORMER = "underperformer"
STATUS_RETIRED = "retired"

PRODUCT_STATUSES = (
    STATUS_DISCOVERED, STATUS_EVALUATED, STATUS_SCORED, STATUS_APPROVED,
    STATUS_PUBLISHED, STATUS_TRACKING, STATUS_WINNER, STATUS_UNDERPERFORMER,
    STATUS_RETIRED,
)

# Which statuses a given status may move to next. Not a strict single-file
# chain: TRACKING can resolve to either WINNER or UNDERPERFORMER, both of
# which can move to RETIRED, and a human can always retire something early.
_STATUS_TRANSITIONS = {
    STATUS_DISCOVERED: {STATUS_EVALUATED, STATUS_RETIRED},
    STATUS_EVALUATED: {STATUS_SCORED, STATUS_RETIRED},
    STATUS_SCORED: {STATUS_APPROVED, STATUS_RETIRED},
    STATUS_APPROVED: {STATUS_PUBLISHED, STATUS_RETIRED},
    STATUS_PUBLISHED: {STATUS_TRACKING, STATUS_RETIRED},
    STATUS_TRACKING: {STATUS_WINNER, STATUS_UNDERPERFORMER, STATUS_RETIRED},
    STATUS_WINNER: {STATUS_RETIRED},
    STATUS_UNDERPERFORMER: {STATUS_RETIRED, STATUS_TRACKING},
    STATUS_RETIRED: set(),
}

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
        # Phase 3 — canonical product record extension for the DDF
        # commerce-platform architecture. All additive, all optional;
        # nothing here breaks an existing row that predates these columns.
        "subcategory": "TEXT",
        "merchant": "TEXT",
        "affiliate_network": "TEXT",
        "estimated_commission": "REAL",
        "product_rating": "REAL",
        "tags": "TEXT",
        "status": "TEXT",
        "status_updated": "TEXT",
        "published_date": "TEXT",
    })
    _ensure_columns(conn, "posts", {"clicks": "INTEGER DEFAULT 0"})
    # Backfill status for rows written before this column existed, so
    # nothing sits at NULL forever — derived honestly from the flag that
    # already existed (approved), never guessed at.
    conn.execute(
        "UPDATE products SET status = ? WHERE status IS NULL AND approved = 1",
        (STATUS_PUBLISHED,),
    )
    conn.execute(
        "UPDATE products SET status = ? WHERE status IS NULL AND (approved = 0 OR approved IS NULL)",
        (STATUS_DISCOVERED,),
    )
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


def set_product_status(product_id: str, new_status: str) -> dict[str, Any]:
    """Moves a product to a new lifecycle status. Refuses an invalid status
    name and a transition that isn't in _STATUS_TRANSITIONS (e.g. jumping
    straight from 'discovered' to 'winner') rather than silently allowing
    it — the same enforced-not-just-conventional pattern
    validate_retailer() uses for the retailer allowlist.

    `approved` (the flag list_products()/ddf_site already filter on) is
    kept in sync automatically: True from PUBLISHED onward, so nothing
    downstream needs to know about `status` to keep working.
    published_date is stamped the first time a product reaches PUBLISHED,
    and never overwritten by a later status change."""
    if new_status not in PRODUCT_STATUSES:
        raise ValueError(f"Unknown product status: {new_status!r}. Valid: {list(PRODUCT_STATUSES)}")

    conn = _connect()
    row = conn.execute("SELECT status, published_date FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "detail": f"No product with id {product_id!r}."}

    current = row["status"] or STATUS_DISCOVERED
    allowed = _STATUS_TRANSITIONS.get(current, set())
    if new_status != current and new_status not in allowed:
        conn.close()
        return {
            "ok": False,
            "detail": f"Can't move a product from {current!r} to {new_status!r}. "
                      f"Allowed next step(s): {sorted(allowed) or 'none (terminal state)'}.",
        }

    now = datetime.now(timezone.utc).isoformat()
    is_approved = 1 if new_status in (STATUS_PUBLISHED, STATUS_TRACKING, STATUS_WINNER, STATUS_UNDERPERFORMER) else 0
    published_date = row["published_date"]
    if new_status == STATUS_PUBLISHED and not published_date:
        published_date = now

    conn.execute(
        "UPDATE products SET status = ?, status_updated = ?, approved = ?, published_date = ? WHERE product_id = ?",
        (new_status, now, is_approved, published_date, product_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "product_id": product_id, "status": new_status}


def advance_to_published(product_id: str, approved: bool = False) -> dict[str, Any]:
    """Walks a product through the internal, non-public lifecycle stages
    automatically (discovered -> evaluated -> scored -> approved_pending_
    publish — none of these make anything visible externally, so none of
    them need Lee's approval) and stops right before PUBLISHED unless
    approved=True is passed, matching the same explicit-approval contract
    every other consequential action in this codebase uses (gmail_
    integration.send_email, calendar_integration.create_event, etc.).

    This is what 'JARVIS, publish this product to DDF' actually calls —
    a single request that does all the safe bookkeeping and then either
    stops at the approval gate or completes it, rather than making Lee
    walk four separate status transitions by hand."""
    product = get_product(product_id)
    if product is None:
        return {"ok": False, "detail": f"No product with id {product_id!r}."}

    current = product.get("status") or STATUS_DISCOVERED
    safe_chain = [STATUS_EVALUATED, STATUS_SCORED, STATUS_APPROVED]
    try:
        start_idx = safe_chain.index(current) + 1 if current in safe_chain else 0
    except ValueError:
        start_idx = 0
    for step in safe_chain[start_idx:]:
        result = set_product_status(product_id, step)
        if not result["ok"]:
            return result
        current = step

    if current == STATUS_PUBLISHED:
        return {"ok": True, "product_id": product_id, "status": STATUS_PUBLISHED, "already_published": True}

    if not approved:
        return {
            "ok": False, "state": "NOT_APPROVED", "product_id": product_id, "status": current,
            "detail": "Product is ready to publish and needs approval — call publish again with approved=True.",
        }

    return set_product_status(product_id, STATUS_PUBLISHED)


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
    # INSERT OR REPLACE fully replaces the row on a product_id conflict —
    # any column not explicitly passed this time would otherwise silently
    # reset to its default. Fine for score/slug (meant to be recomputed),
    # not fine for lifecycle status or accumulated performance data: a
    # routine "update the price" re-save must never quietly revert a
    # PUBLISHED product back to DISCOVERED or wipe its view/click history.
    existing = conn.execute(
        "SELECT status, status_updated, published_date, views, affiliate_clicks, conversions, revenue "
        "FROM products WHERE product_id = ?", (product_id,)
    ).fetchone()

    product_record = dict(product)
    product_record["product_id"] = product_id
    product_record["score"] = score_product(product)
    product_record["discovery_date"] = product_record.get("discovery_date") or datetime.now(timezone.utc).isoformat()
    product_record["slug"] = product_record.get("slug") or _slugify(product_record.get("name", ""), product_id)

    if existing is not None:
        product_record.setdefault("status", existing["status"])
        product_record["status_updated"] = product_record.get("status_updated") or existing["status_updated"]
        product_record["published_date"] = product_record.get("published_date") or existing["published_date"]
        for field in ("views", "affiliate_clicks", "conversions", "revenue"):
            if field not in product:
                product_record[field] = existing[field]
    if "status" not in product_record:
        # Backward compatibility: save_product(approved=True) predates the
        # status column and is still how every current caller (the DDF
        # agent, ddf_site, existing tests) marks a product publishable —
        # treat it as the PUBLISHED status it always meant, rather than
        # silently ignoring it now that status exists.
        product_record["status"] = STATUS_PUBLISHED if product_record.get("approved") else STATUS_DISCOVERED
    is_approved = 1 if product_record["status"] in (STATUS_PUBLISHED, STATUS_TRACKING, STATUS_WINNER, STATUS_UNDERPERFORMER) else 0

    original_price = product_record.get("original_price")
    current_price = product_record.get("current_price", product_record.get("price"))
    if product_record.get("discount_pct") is None and original_price and current_price and original_price > 0:
        product_record["discount_pct"] = round((1 - (float(current_price) / float(original_price))) * 100, 2)
    else:
        product_record.setdefault("discount_pct", None)

    if product_record.get("estimated_commission") is None and current_price and product_record.get("commission_rate"):
        product_record["estimated_commission"] = round(float(current_price) * float(product_record["commission_rate"]), 2)
    else:
        product_record.setdefault("estimated_commission", None)

    social_platforms = product_record.get("social_platforms_posted")
    if isinstance(social_platforms, list):
        social_platforms = ",".join(social_platforms)

    tags = product_record.get("tags")
    if isinstance(tags, list):
        tags = ",".join(str(t).strip() for t in tags if str(t).strip())

    conn.execute(
        """
        INSERT OR REPLACE INTO products (
            id, name, source, category, price, url, image_url, product_id, sales_signal,
            demand, margin, trend_strength, competition, content_potential, repeatability,
            historical_performance, affiliate_url, discovery_date, score, approved, notes,
            description, retailer, original_price, current_price, discount_pct,
            affiliate_source, date_posted, social_platforms_posted, views, affiliate_clicks,
            conversions, revenue, commission_rate, slug, subcategory, merchant,
            affiliate_network, estimated_commission, product_rating, tags, status,
            status_updated, published_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            is_approved,
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
            product_record.get("subcategory"),
            product_record.get("merchant"),
            product_record.get("affiliate_network"),
            product_record.get("estimated_commission"),
            product_record.get("product_rating"),
            tags,
            product_record.get("status"),
            product_record.get("status_updated"),
            product_record.get("published_date"),
        ),
    )
    conn.commit()
    conn.close()
    product_record["approved"] = is_approved
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


# ── performance ranking — combines post-publish signals, not just the ──────
# ── pre-publish discovery score() above ─────────────────────────────────────

def _recency_factor(iso_date: str | None, half_life_days: float = 5.0) -> float:
    """1.0 for something published/discovered right now, decaying by half
    every `half_life_days`. Missing or unparseable dates score 0 — a
    ranking input, never a hard failure."""
    if not iso_date:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(iso_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    return 0.5 ** (age_days / half_life_days)


def rank_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Performance ranking for already-published products. Combines
    recency, engagement (views/clicks), real conversion rate, revenue,
    trend velocity, discount, and commission into one comparable
    `rank_score` (attached to each returned dict, highest first).

    Deliberately a transparent weighted formula, not a black box or a
    learned model, per the instruction not to over-engineer this before
    the basic commerce loop is proven out. A product with 0 clicks/views
    scores low on engagement honestly — nothing here assumes an average
    or fabricates a signal that was never actually recorded."""
    import math

    weights = {
        "recency": 0.25, "engagement": 0.20, "conversion": 0.15,
        "revenue": 0.15, "trend": 0.15, "discount": 0.05, "commission": 0.05,
    }
    ranked = []
    for p in products:
        views = float(p.get("views") or 0)
        clicks = float(p.get("affiliate_clicks") or 0)
        conversions = float(p.get("conversions") or 0)
        revenue = float(p.get("revenue") or 0)
        trend = min(max(float(p.get("trend_strength") or 0), 0.0), 1.0)
        discount = min(max(float(p.get("discount_pct") or 0) / 100.0, 0.0), 1.0)
        commission_rate = min(max(float(p.get("commission_rate") or 0), 0.0), 1.0)
        recency = _recency_factor(p.get("published_date") or p.get("discovery_date"))

        # log1p compresses outliers so one viral product doesn't make
        # every other product's engagement score read as zero by contrast.
        engagement = min(math.log1p(views + clicks * 3) / math.log1p(1000), 1.0)
        conversion_rate = (conversions / clicks) if clicks > 0 else 0.0
        revenue_factor = min(math.log1p(revenue) / math.log1p(500), 1.0)

        rank_score = 100 * (
            recency * weights["recency"]
            + engagement * weights["engagement"]
            + conversion_rate * weights["conversion"]
            + revenue_factor * weights["revenue"]
            + trend * weights["trend"]
            + discount * weights["discount"]
            + commission_rate * weights["commission"]
        )
        p = dict(p)
        p["rank_score"] = round(rank_score, 2)
        ranked.append(p)
    ranked.sort(key=lambda r: r["rank_score"], reverse=True)
    return ranked


def select_daily_high_ticket_picks(limit: int = 2, min_price: float = 100.0) -> list[dict[str, Any]]:
    """The standing 'two high-ticket products a day' operating requirement.
    Deliberately NOT just the two most expensive products — combines price
    with demand, trend momentum, product rating, and estimated commission
    per sale, so a picked product is something people actually want and
    that pays meaningfully, not just an expensive item nobody will buy.

    Only draws from products already past raw discovery (SCORED or later)
    so an unvetted find never gets promoted straight to a high-ticket
    placement, and excludes RETIRED products."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE (current_price >= ? OR price >= ?) "
        "AND status NOT IN (?, ?, ?) ORDER BY current_price DESC LIMIT 50",
        (min_price, min_price, STATUS_DISCOVERED, STATUS_EVALUATED, STATUS_RETIRED),
    ).fetchall()
    conn.close()
    candidates = [dict(r) for r in rows]
    if not candidates:
        return []

    def _high_ticket_value(p: dict[str, Any]) -> float:
        price = float(p.get("current_price") or p.get("price") or 0)
        demand = min(max(float(p.get("demand") or 0), 0.0), 100.0)
        trend = min(max(float(p.get("trend_strength") or 0), 0.0), 1.0)
        commission_rate = float(p.get("commission_rate") or 0)
        est_commission = float(p.get("estimated_commission") or (price * commission_rate))
        rating = min(max(float(p.get("product_rating") or 0), 0.0), 5.0) / 5.0
        return (
            min(est_commission, 200) * 0.40
            + demand * 0.25
            + trend * 100 * 0.20
            + rating * 100 * 0.15
        )

    candidates.sort(key=_high_ticket_value, reverse=True)
    return candidates[:limit]


def get_you_might_have_missed(exclude_product_id: str | None = None, days: int = 14, limit: int = 10) -> list[dict[str, Any]]:
    """Products published in the last `days` (default 14) but NOT today —
    the point is surfacing things a visitor arriving today wouldn't
    otherwise see on a same-day feed. Ranked by rank_products() so this is
    'the best of what you missed,' not an arbitrary older list."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _connect()
    q = (
        "SELECT * FROM products WHERE approved = 1 AND published_date IS NOT NULL "
        "AND published_date >= ? AND published_date NOT LIKE ?"
    )
    params: list[Any] = [since, f"{today}%"]
    if exclude_product_id:
        q += " AND product_id != ?"
        params.append(exclude_product_id)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rank_products([dict(r) for r in rows])[:limit]


def get_this_weeks_hottest(limit: int = 10) -> list[dict[str, Any]]:
    """Published or discovered in the last 7 days, ranked by real
    performance (rank_products), not just recency or trend_strength alone."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM products WHERE approved = 1 "
        "AND (published_date >= ? OR (published_date IS NULL AND discovery_date >= ?))",
        (since, since),
    ).fetchall()
    conn.close()
    return rank_products([dict(r) for r in rows])[:limit]


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
