"""Daily Deal Finders — public-facing website.

Built with the same stack/pattern as dashboard/server.py (FastAPI + plain
HTML/CSS/vanilla-JS, JSON API + client-side fetch/render — no templating
engine dependency, no new framework) but runs as its OWN app/process on its
own port, deliberately separate from the private JARVIS dashboard
(dashboard/server.py) rather than mounted on it: that app has phone-pairing
auth, device tokens, and a LAN-trust boundary appropriate for a private
control plane, none of which belong on a public storefront.

Data comes entirely from actions/daily_deal_finders.py's products/posts
tables — the same tables the DDF agent (actions/agent_orchestrator.py) and
the 3D Command Center's "ddf" nucleus already read via get_top_products().
This site adds read queries (list_products/get_todays_deals/etc.) and
click/view tracking on top of that same table — one connected product
record, not a second disconnected catalog.

Every page renders honestly empty ("No deals yet") when there's no real
data — nothing here fabricates a product, price, or affiliate link.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False

from actions import daily_deal_finders as ddf

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

PORT = 8100  # deliberately distinct from JARVIS's private dashboard (8000/8001/8002)

SITE_NAME = "Daily Deal Finders"

NAV_LINKS = [
    ("/", "Home"),
    ("/todays-deals", "Today's Deals"),
    ("/trending", "Trending"),
    ("/best-sellers", "Best Sellers"),
    ("/high-ticket", "High-Ticket"),
    ("/categories", "Categories"),
    ("/amazon", "Amazon"),
    ("/tiktok-shop", "TikTok Shop"),
    ("/about", "About"),
    ("/contact", "Contact"),
]


def _shell(title: str, content: str, active_path: str = "", extra_head: str = "") -> str:
    nav_html = "\n".join(
        f'<a href="{href}" class="{"active" if href == active_path else ""}">{label}</a>'
        for href, label in NAV_LINKS
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — {SITE_NAME}</title>
  <link rel="stylesheet" href="/static/css/style.css">
  {extra_head}
</head>
<body>
  <header class="site-header">
    <div class="bar">
      <a href="/" class="brand"><span class="dot">●</span> {SITE_NAME}</a>
      <nav class="site-nav">{nav_html}</nav>
    </div>
  </header>
  <main>
    {content}
  </main>
  <footer class="site-footer">
    <div class="bar">
      <span>&copy; {SITE_NAME}. Some links on this site are affiliate links — see our <a href="/affiliate-disclosure">Affiliate Disclosure</a>.</span>
      <span><a href="/affiliate-disclosure">Affiliate Disclosure</a> &middot; <a href="/contact">Contact</a></span>
    </div>
  </footer>
  <script src="/static/js/site.js"></script>
</body>
</html>"""


def _listing_page(title: str, subtitle: str, api_path: str, active_path: str, empty_message: str = "") -> str:
    content = f"""
    <div class="hero">
      <h1>{title}</h1>
      <p>{subtitle}</p>
    </div>
    <div id="deal-grid" class="deal-grid"></div>
    <script>
      renderDealGrid("deal-grid", "{api_path}", "{empty_message}");
    </script>
    """
    return _shell(title, content, active_path)


class DDFSiteServer:
    def __init__(self):
        self.app = self._build_app()

    def _build_app(self):
        if not _DEPS_OK:
            raise RuntimeError("fastapi is required for ddf_site.server")

        app = FastAPI(title=SITE_NAME)
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        # ── pages ────────────────────────────────────────────────────────

        @app.get("/", response_class=HTMLResponse)
        async def home():
            content = f"""
            <div class="hero">
              <h1>Real deals, worth your time.</h1>
              <p>Hand-picked daily deals from Amazon and TikTok Shop — updated as we find them. No fake discounts, no fabricated prices.</p>
            </div>
            <div class="section-title"><h2>Today's Deals</h2><span class="sub"><a href="/todays-deals">See all</a></span></div>
            <div id="today-grid" class="deal-grid"></div>
            <div class="section-title" style="margin-top:36px"><h2>Trending</h2><span class="sub"><a href="/trending">See all</a></span></div>
            <div id="trending-grid" class="deal-grid"></div>
            <script>
              renderDealGrid("today-grid", "/api/deals/today", "No deals posted yet today — check back soon.");
              renderDealGrid("trending-grid", "/api/deals/trending", "Nothing trending yet.");
            </script>
            """
            return _shell("Home", content, "/")

        @app.get("/todays-deals", response_class=HTMLResponse)
        async def todays_deals():
            return _listing_page(
                "Today's Deals", "Everything we've posted today.",
                "/api/deals/today", "/todays-deals", "No deals posted yet today — check back soon.",
            )

        @app.get("/trending", response_class=HTMLResponse)
        async def trending():
            return _listing_page(
                "Trending Deals", "What's picking up momentum right now.",
                "/api/deals/trending", "/trending", "Nothing trending yet.",
            )

        @app.get("/best-sellers", response_class=HTMLResponse)
        async def best_sellers():
            return _listing_page(
                "Best Sellers", "Our top-converting deals.",
                "/api/deals/best-sellers", "/best-sellers", "No best-sellers tracked yet.",
            )

        @app.get("/high-ticket", response_class=HTMLResponse)
        async def high_ticket():
            return _listing_page(
                "High-Ticket Deals", "Bigger purchases, bigger savings.",
                "/api/deals/high-ticket", "/high-ticket", "No high-ticket deals yet.",
            )

        @app.get("/categories", response_class=HTMLResponse)
        async def categories():
            content = """
            <div class="hero"><h1>Categories</h1><p>Browse deals by category.</p></div>
            <div id="category-pills" class="pill-row"></div>
            <script>renderCategoryPills("category-pills");</script>
            """
            return _shell("Categories", content, "/categories")

        @app.get("/category/{category}", response_class=HTMLResponse)
        async def category_page(category: str):
            return _listing_page(
                category.replace("-", " ").title(), f"Deals in {category.replace('-', ' ')}.",
                f"/api/deals?category={category}", "/categories", "No deals in this category yet.",
            )

        @app.get("/amazon", response_class=HTMLResponse)
        async def amazon_page():
            return _listing_page(
                "Amazon Deals", "Deals sourced from Amazon.",
                "/api/deals?retailer=amazon", "/amazon", "No Amazon deals yet.",
            )

        @app.get("/tiktok-shop", response_class=HTMLResponse)
        async def tiktok_shop_page():
            return _listing_page(
                "TikTok Shop Deals", "Deals sourced from TikTok Shop.",
                "/api/deals?retailer=tiktok_shop", "/tiktok-shop", "No TikTok Shop deals yet.",
            )

        @app.get("/deal/{slug}", response_class=HTMLResponse)
        async def deal_detail(slug: str):
            content = f"""
            <div class="product-detail" id="product-detail"></div>
            <script>renderProductDetail("product-detail", {slug!r});</script>
            """
            return _shell("Deal", content)

        @app.get("/about", response_class=HTMLResponse)
        async def about():
            content = """
            <div class="static-content">
              <h1>About Daily Deal Finders</h1>
              <p>Daily Deal Finders hand-picks real, currently-available deals from approved retail
              partners and shares them here and across social media. We don't fabricate prices,
              discounts, or availability — every deal listed links to the actual retailer page.</p>
              <h2>How we choose deals</h2>
              <p>Deals are evaluated on savings, product quality signals, and relevance before
              they're posted. We only publish links to retailers we currently have an approved
              affiliate relationship with.</p>
            </div>
            """
            return _shell("About", content, "/about")

        @app.get("/affiliate-disclosure", response_class=HTMLResponse)
        async def affiliate_disclosure():
            content = """
            <div class="static-content">
              <h1>Affiliate Disclosure</h1>
              <p>Daily Deal Finders participates in affiliate marketing programs, which means we may
              earn a commission from qualifying purchases made through links on this site, at no
              additional cost to you.</p>
              <h2>Our current affiliate partners</h2>
              <p>Amazon Associates and TikTok Shop's affiliate program. We do not currently have an
              affiliate relationship with Target or Walmart, and no links on this site point to
              those retailers.</p>
              <h2>Our commitment</h2>
              <p>We never fabricate a price, discount percentage, or affiliate link. If a deal shown
              here doesn't match what you see at checkout, please <a href="/contact">let us know</a>.</p>
            </div>
            """
            return _shell("Affiliate Disclosure", content, "")

        @app.get("/contact", response_class=HTMLResponse)
        async def contact():
            content = """
            <div class="static-content">
              <h1>Contact Us</h1>
              <p>Questions about a deal, a partnership inquiry, or found a broken link? Send us a message.</p>
              <form class="contact-form" onsubmit="return false;">
                <input type="text" placeholder="Your name">
                <input type="email" placeholder="Your email">
                <textarea placeholder="Message"></textarea>
                <button class="cta-btn" type="button" disabled>Contact form coming soon</button>
              </form>
            </div>
            """
            return _shell("Contact", content, "/contact")

        # ── JSON API ─────────────────────────────────────────────────────

        @app.get("/api/deals")
        async def api_deals(category: str | None = None, retailer: str | None = None, limit: int = 50):
            deals = ddf.list_products(category=category, retailer=retailer, limit=limit)
            return JSONResponse({"deals": deals})

        @app.get("/api/deals/today")
        async def api_deals_today(limit: int = 20):
            return JSONResponse({"deals": ddf.get_todays_deals(limit=limit)})

        @app.get("/api/deals/trending")
        async def api_deals_trending(limit: int = 20):
            return JSONResponse({"deals": ddf.get_trending_deals(limit=limit)})

        @app.get("/api/deals/best-sellers")
        async def api_deals_best_sellers(limit: int = 20):
            return JSONResponse({"deals": ddf.get_best_sellers(limit=limit)})

        @app.get("/api/deals/high-ticket")
        async def api_deals_high_ticket(min_price: float = 100.0, limit: int = 20):
            return JSONResponse({"deals": ddf.get_high_ticket_deals(min_price=min_price, limit=limit)})

        @app.get("/api/categories")
        async def api_categories():
            return JSONResponse({"categories": ddf.list_categories()})

        @app.get("/api/deal/{slug}")
        async def api_deal_detail(slug: str):
            deal = ddf.get_product_by_slug(slug)
            if deal is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            return JSONResponse({"deal": deal})

        @app.post("/api/track/view/{product_id}")
        async def api_track_view(product_id: str):
            changed = ddf.record_view(product_id)
            return JSONResponse({"ok": changed})

        @app.post("/api/track/click/{product_id}")
        async def api_track_click(product_id: str, platform: str | None = None):
            changed = ddf.record_affiliate_click(product_id, platform=platform)
            return JSONResponse({"ok": changed})

        return app

    async def serve(self) -> None:
        import uvicorn
        config = uvicorn.Config(self.app, host="0.0.0.0", port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()


def create_app():
    return DDFSiteServer().app
