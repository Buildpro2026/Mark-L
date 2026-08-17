// Daily Deal Finders — shared front-end. Vanilla JS + fetch against this
// site's own JSON API, matching the pattern already used by JARVIS's 3D
// Command Center (dashboard/static/3d) rather than introducing a build step
// or a new framework dependency.

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function formatPrice(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  if (Number.isNaN(n)) return null;
  return "$" + n.toFixed(2);
}

function retailerLabel(retailer) {
  if (!retailer) return null;
  return { amazon: "Amazon", tiktok_shop: "TikTok Shop" }[retailer] || retailer;
}

function dealCardHtml(deal) {
  const current = formatPrice(deal.current_price ?? deal.price);
  const original = formatPrice(deal.original_price);
  const showOriginal = original && current && original !== current;
  const image = deal.image_url
    ? `<img src="${escapeHtml(deal.image_url)}" alt="${escapeHtml(deal.name)}" loading="lazy">`
    : `<div class="placeholder">No image yet</div>`;
  const discount = deal.discount_pct ? `<div class="badge">-${Math.round(deal.discount_pct)}%</div>` : "";
  const retailer = retailerLabel(deal.retailer);

  return `
    <article class="deal-card">
      <a href="/deal/${encodeURIComponent(deal.slug)}" class="thumb">
        ${image}
        ${discount}
        ${retailer ? `<div class="badge retailer">${escapeHtml(retailer)}</div>` : ""}
      </a>
      <div class="body">
        <a href="/deal/${encodeURIComponent(deal.slug)}" class="name">${escapeHtml(deal.name)}</a>
        <div class="price-row">
          ${current ? `<span class="price-current">${current}</span>` : ""}
          ${showOriginal ? `<span class="price-original">${original}</span>` : ""}
        </div>
        <button class="cta-btn" onclick="goToDeal('${escapeHtml(deal.slug)}', '${escapeHtml(deal.product_id)}')">GET DEAL</button>
      </div>
    </article>
  `;
}

function goToDeal(slug) {
  window.location.href = "/deal/" + encodeURIComponent(slug);
}

async function renderDealGrid(containerId, apiPath, emptyMessage) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const resp = await fetch(apiPath);
    const data = await resp.json();
    const deals = data.deals || [];
    if (deals.length === 0) {
      el.outerHTML = `<div class="empty-state">${escapeHtml(emptyMessage || "No deals yet — check back soon.")}</div>`;
      return;
    }
    el.innerHTML = deals.map(dealCardHtml).join("");
  } catch (err) {
    el.outerHTML = `<div class="empty-state">Deals are temporarily unavailable.</div>`;
  }
}

async function renderCategoryPills(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const resp = await fetch("/api/categories");
    const data = await resp.json();
    const categories = data.categories || [];
    if (categories.length === 0) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = categories
      .map((c) => `<a class="pill" href="/category/${encodeURIComponent(c)}">${escapeHtml(c)}</a>`)
      .join("");
  } catch (err) {
    el.innerHTML = "";
  }
}

async function renderProductDetail(containerId, slug) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const resp = await fetch(`/api/deal/${encodeURIComponent(slug)}`);
    if (resp.status === 404) {
      el.innerHTML = `<div class="empty-state">This deal isn't available anymore.</div>`;
      return;
    }
    const data = await resp.json();
    const deal = data.deal;
    const current = formatPrice(deal.current_price ?? deal.price);
    const original = formatPrice(deal.original_price);
    const showOriginal = original && current && original !== current;
    const image = deal.image_url
      ? `<img src="${escapeHtml(deal.image_url)}" alt="${escapeHtml(deal.name)}">`
      : `<div class="placeholder">No image yet</div>`;
    const retailer = retailerLabel(deal.retailer);

    el.innerHTML = `
      <div class="thumb">${image}</div>
      <div class="info">
        <h1>${escapeHtml(deal.name)}</h1>
        <div class="meta-row">
          ${retailer ? `<span class="pill">${escapeHtml(retailer)}</span>` : ""}
          ${deal.category ? `<span class="pill">${escapeHtml(deal.category)}</span>` : ""}
        </div>
        <div class="price-block">
          ${current ? `<span class="current">${current}</span>` : ""}
          ${showOriginal ? `<span class="original">${original}</span>` : ""}
          ${deal.discount_pct ? `<span class="discount">Save ${Math.round(deal.discount_pct)}%</span>` : ""}
        </div>
        <p class="desc">${escapeHtml(deal.description || "No description available yet.")}</p>
        <button class="cta-btn large" id="deal-cta">SHOP NOW</button>
      </div>
    `;

    document.title = deal.name + " — Daily Deal Finders";

    fetch(`/api/track/view/${encodeURIComponent(deal.product_id)}`, { method: "POST" }).catch(() => {});

    const ctaBtn = document.getElementById("deal-cta");
    if (ctaBtn) {
      ctaBtn.addEventListener("click", () => {
        fetch(`/api/track/click/${encodeURIComponent(deal.product_id)}`, { method: "POST" }).catch(() => {});
        if (deal.affiliate_url) {
          window.open(deal.affiliate_url, "_blank", "noopener");
        }
      });
      if (!deal.affiliate_url) {
        ctaBtn.disabled = true;
        ctaBtn.textContent = "LINK COMING SOON";
      }
    }
  } catch (err) {
    el.innerHTML = `<div class="empty-state">This deal is temporarily unavailable.</div>`;
  }
}
