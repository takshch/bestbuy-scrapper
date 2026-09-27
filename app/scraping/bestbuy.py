"""Best Buy search-results scraper: Selenium driving CloakBrowser.

CloakBrowser is a Chromium build with source-level fingerprint patches (canvas,
WebGL, audio, fonts, automation flags, CDP signals), which gets past Cloudflare /
Akamai bot checks far more reliably than JS-injected stealth. Selenium drives it
through a ChromeDriver matching its Chromium version.

Synchronous (Selenium is blocking); the service layer runs it in a worker thread.
"""
import logging
import random
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from cloakbrowser.config import get_default_stealth_args
from cloakbrowser.download import ensure_binary

from app.config import Settings
from app.scraping.proxy import ProxyError, ProxyForwarder, oxylabs_proxy, verify_upstream

log = logging.getLogger("scraper.bestbuy")

BASE_URL = "https://www.bestbuy.com"
# Current layout (2026): `.product-list-item[data-product-id]` — an <li>, or a <div> inside a
# sponsored <li>. Legacy `li.sku-item` kept as a fallback.
CARD_SELECTOR = ".product-list-item[data-product-id], li.product-list-item, li.sku-item"
READY_SELECTOR = f"{CARD_SELECTOR}, .no-results-message"

# Best Buy has shipped several listing layouts, so each field tries a list of
# selectors (current layout first, legacy after). Runs in the page.
EXTRACT_JS = r"""
const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
const first = (root, selectors) => {
  for (const s of selectors) { const el = root.querySelector(s); if (el) return el; }
  return null;
};
const money = (s) => {
  if (!s) return null;
  const m = s.replace(/,/g, '').match(/\$\s*(\d+(?:\.\d{1,2})?)/);
  return m ? parseFloat(m[1]) : null;
};
const absolute = (href) => (href && href.startsWith('/') ? location.origin + href : href);

const seen = new Set();
const products = [];
for (const card of document.querySelectorAll(arguments[0])) {
  const linkEl = first(card, ['a.product-list-item-link', '.sku-title a', 'a[href*="/sku/"]', 'a[href*="/site/"]']);
  const href = linkEl ? linkEl.getAttribute('href') : null;
  const skuFromUrl = href && (href.match(/\/sku\/(\d+)/) || href.match(/\/(\d{6,})\.p/));
  const sku = card.getAttribute('data-product-id') || card.getAttribute('data-sku-id') ||
    card.querySelector('[data-product-id], [data-sku-id]')?.getAttribute('data-product-id') ||
    card.querySelector('[data-sku-id]')?.getAttribute('data-sku-id') ||
    (skuFromUrl ? skuFromUrl[1] : null);
  if (!sku || seen.has(sku)) continue;

  const titleEl = first(card, ['h3.product-title', '.product-title', '.sku-title a', 'h4.sku-title a']);
  const name = titleEl ? (titleEl.getAttribute('title') || text(titleEl)) : null;
  if (!name) continue;  // not rendered yet
  seen.add(sku);

  const priceEl = first(card, [
    '[data-testid="price-block-customer-price"] .sr-only',
    '[data-testid="price-block-customer-price"]',
    '.priceView-customer-price span[aria-hidden="true"]', '.priceView-customer-price span',
  ]);
  // Only shown on sale items, e.g. "Save $200 Was $1,099.99" / "Comp. Value $1,099.99".
  const savingsText = [
    '[data-testid="price-block-savings"]', '[data-testid="price-block-footer"]',
    '.pricing-price__regular-price',
  ].map((sel) => text(card.querySelector(sel)) || '').join(' ').replace(/,/g, '');
  const wasMatch = savingsText.match(/(?:Was|Comp\.? Value|Reg(?:ular)?)\D{0,10}\$\s*(\d+(?:\.\d{1,2})?)/i);

  // Ratings are exposed as accessible text: "Rating 4.7 out of 5 stars with 1,234 reviews"
  const ratingText = text(first(card, ['.c-ratings-reviews .visually-hidden', '.c-ratings-reviews p'])) ||
    card.querySelector('[aria-label*="out of 5"]')?.getAttribute('aria-label') || '';
  const ratingMatch = ratingText.match(/(\d+(?:\.\d+)?)\s*out of 5/i);
  const reviewsMatch = ratingText.replace(/,/g, '').match(/(\d+)\s*review/i) ||
    (text(first(card, ['.c-reviews'])) || '').replace(/,/g, '').match(/(\d+)/);

  const img = first(card, ['img[data-testid="product-image"]', 'img.product-image', 'img']);
  const modelEl = first(card, ['.sku-model .sku-value', '[class*="sku-model"] .sku-value']);
  // Cart button test ids look like "plp-add-to-cart-<sku>", "plp-unavailable-<sku>", "plp-sold-out-<sku>".
  const cartBtn = first(card, [
    `button[data-testid^="plp-"][data-testid$="-${sku}"]`, 'button.add-to-cart-button', 'button[data-button-state]',
  ]);
  let availability = null;
  if (cartBtn) {
    const tid = cartBtn.getAttribute('data-testid');
    availability = cartBtn.getAttribute('data-button-state') ||
      (tid ? tid.replace(/^plp-/, '').replace(`-${sku}`, '').replace(/-/g, '_').toUpperCase() : text(cartBtn));
  }

  products.push({
    sku,
    name,
    url: absolute(href),
    model: text(modelEl),
    price: money(text(priceEl)),
    regular_price: wasMatch ? parseFloat(wasMatch[1]) : null,
    rating: ratingMatch ? parseFloat(ratingMatch[1]) : null,
    review_count: reviewsMatch ? parseInt(reviewsMatch[1], 10) : null,
    image_url: img ? (img.getAttribute('src') || img.getAttribute('data-src')) : null,
    availability,
  });
}
return products;
"""

# Primary source: the page's Apollo GraphQL cache. It holds complete product data
# (price, model, rating...) even for cards that haven't rendered yet.
APOLLO_EXTRACT_JS = r"""
// arguments[0]: card selector. Returns products for the search-result cards, read from
// the page's Apollo GraphQL cache (complete even for cards that never render).
const client = window[Symbol.for('ApolloClientSingleton')];
if (!client || !client.cache) return null;
const cache = client.cache.extract();

const resolve = (v, depth = 0) => {
  if (depth > 6 || v === null || typeof v !== 'object') return v;
  if (Array.isArray(v)) return v.map((x) => resolve(x, depth + 1));
  if (v.__ref) return resolve(cache[v.__ref], depth + 1);
  const out = {};
  for (const [k, x] of Object.entries(v)) out[k] = resolve(x, depth + 1);
  return out;
};
// Fields may be stored with arguments, e.g. `price({"input":{...}})`.
const field = (obj, name) => {
  if (!obj) return undefined;
  if (obj[name] !== undefined && obj[name] !== null) return obj[name];
  const k = Object.keys(obj).find((key) => key.startsWith(name + '(') && obj[key] !== null);
  return k ? obj[k] : undefined;
};

// Merge every cache entry for a SKU (Best Buy keys products by skuId + openBoxCondition).
const bySku = {};
for (const [key, val] of Object.entries(cache)) {
  if (!key.startsWith('Product:')) continue;
  let id; try { id = JSON.parse(key.slice(8)); } catch (e) { continue; }
  if (!id || !id.skuId || (id.openBoxCondition !== undefined && id.openBoxCondition !== null)) continue;
  const m = bySku[id.skuId] = bySku[id.skuId] || {};
  for (const [k, v] of Object.entries(val)) if (m[k] === undefined || m[k] === null) m[k] = v;
}

const skus = [];
for (const card of document.querySelectorAll(arguments[0])) {
  const s = card.getAttribute('data-product-id') || card.getAttribute('data-sku-id');
  if (s && !skus.includes(s)) skus.push(s);
}

return skus.map((sku) => {
  const p = bySku[sku];
  if (!p) return {sku};
  const name = resolve(field(p, 'name')) || {};
  const url = resolve(field(p, 'url')) || {};
  const price = resolve(field(p, 'price')) || {};
  const review = resolve(field(p, 'reviewInfo')) || {};
  const image = resolve(field(p, 'primaryImage')) || {};
  const button = resolve(field(p, 'buttonState')) || {};
  const regular = price.displayableRegularPrice ?? price.regularPrice ?? null;
  const customer = price.displayableCustomerPrice ?? price.customerPrice ?? null;
  return {
    sku,
    name: typeof name === 'string' ? name : (name.short || name.title || null),
    url: typeof url === 'string' ? url : (url.skuSpecificUrl || url.pdp || null),
    model: field(p, 'modelNumber') || (resolve(field(p, 'manufacturer')) || {}).modelNumber || null,
    price: customer,
    regular_price: regular !== null && customer !== null && regular > customer ? regular : null,
    rating: review.averageRating ?? null,
    review_count: review.reviewCount ?? null,
    image_url: image.piscesHref || image.href || null,
    availability: button.buttonState || null,
  };
});
"""

# Bring every card into view (triggers the lazy-render observers), then return to top.
SCROLL_JS = """
const done = arguments[arguments.length - 1];
const cards = Array.from(document.querySelectorAll(arguments[0]));
let i = 0;
const timer = setInterval(() => {
  if (i >= cards.length) {
    clearInterval(timer);
    window.scrollTo(0, document.body.scrollHeight);
    done(cards.length);
    return;
  }
  cards[i++].scrollIntoView({block: 'center'});
}, 120);
"""

# Below this share of complete products (name + price), a page counts as degraded and
# the scraper falls back to the proxy (when one is available).
MIN_COMPLETE_RATIO = 0.5

# Page titles served instead of results when a bot wall catches us. Hard blocks never
# clear, so fail fast; challenges may pass on their own after a few seconds.
HARD_BLOCK_MARKERS = ("access denied", "attention required", "forbidden", "blocked")
CHALLENGE_MARKERS = ("just a moment", "are you a human", "captcha", "verify")
BLOCK_MARKERS = HARD_BLOCK_MARKERS + CHALLENGE_MARKERS


def _pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _search_url(query: str, page: int) -> str:
    return f"{BASE_URL}/site/searchpage.jsp?" + urlencode({"st": query, "cp": page, "intl": "nosplash"})


def build_driver(settings: Settings, proxy_port: Optional[int]) -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    # Downloads the CloakBrowser binary on first use (cached in ~/.cloakbrowser).
    opts.binary_location = settings.chrome_binary or ensure_binary()

    # --fingerprint=<seed> randomizes the device fingerprint per browser session.
    for arg in get_default_stealth_args():
        opts.add_argument(arg)
    if settings.scraper_headless:
        opts.add_argument("--headless=new")
    if proxy_port:
        opts.add_argument(f"--proxy-server=http://127.0.0.1:{proxy_port}")
    for arg in (
        # Match the US exit IP (server or proxy) so timezone/locale don't contradict it.
        f"--fingerprint-timezone={settings.browser_timezone}",
        f"--lang={settings.browser_locale}",
        # WebRTC UDP bypasses HTTP proxies and would leak the real IP.
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--window-size=1366,900",
        "--disable-dev-shm-usage",
    ):
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.page_load_strategy = "eager"

    # Selenium Manager reads the binary's version and fetches a matching chromedriver.
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)

    # Skip fonts/media: faster and less proxy bandwidth.
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd(
        "Network.setBlockedURLs",
        {"urls": ["*.woff", "*.woff2", "*.ttf", "*.otf", "*.mp4", "*.webm", "*.m3u8"]},
    )
    return driver


class BlockedError(WebDriverException):
    """Best Buy served a bot-protection page instead of results."""


# Anything that means "this route can't get the page": bot wall, timeout, or a
# network error (Akamai often answers bots with ERR_HTTP2_PROTOCOL_ERROR).
ROUTE_FAILURES = (TimeoutException, WebDriverException)


def _blocked_reason(driver: webdriver.Chrome) -> Optional[str]:
    title = (driver.title or "").lower()
    return next((m for m in BLOCK_MARKERS if m in title), None)


def _page_ready(driver: webdriver.Chrome) -> bool:
    if driver.find_elements(By.CSS_SELECTOR, READY_SELECTOR):
        return True
    title = (driver.title or "").lower()
    if any(m in title for m in HARD_BLOCK_MARKERS):
        raise BlockedError(f"blocked by bot protection (page title: {driver.title!r})")
    return False


def _is_complete(p: Dict[str, Any]) -> bool:
    return bool(p.get("name")) and p.get("price") is not None


PRODUCT_KEYS = ("sku", "name", "url", "model", "price", "regular_price", "rating",
                "review_count", "image_url", "availability")


def _extract(driver: webdriver.Chrome) -> Tuple[List[Dict[str, Any]], int]:
    """Merge Apollo-cache data with rendered-card data. Returns (products, card count).

    Cache values win; the DOM fills fields the cache lacks (e.g. availability)."""
    cached = driver.execute_script(APOLLO_EXTRACT_JS, CARD_SELECTOR) or []
    rendered = {p["sku"]: p for p in (driver.execute_script(EXTRACT_JS, CARD_SELECTOR) or [])}
    empty = dict.fromkeys(PRODUCT_KEYS)
    products = []
    for c in cached:
        dom = rendered.pop(c["sku"], {})
        merged = {**empty, **dom, **{k: v for k, v in c.items() if v is not None}}
        if merged["name"]:
            products.append(merged)
    products.extend({**empty, **p} for p in rendered.values())  # cards the cache didn't know about
    return products, max(len(cached), len(products))


def _load_all_cards(
    driver: webdriver.Chrome, page: int, budget: float
) -> Tuple[List[Dict[str, Any]], int]:
    """Scroll through the cards (their data loads lazily) until every card is complete,
    two passes in a row add nothing, or the time budget runs out."""
    deadline = time.monotonic() + budget
    products, cards = _extract(driver)
    best, stale = sum(map(_is_complete, products)), 0
    while best < cards and stale < 2 and time.monotonic() < deadline:
        driver.execute_async_script(SCROLL_JS, CARD_SELECTOR)
        time.sleep(1.5)
        products, cards = _extract(driver)
        complete = sum(map(_is_complete, products))
        stale = stale + 1 if complete <= best else 0
        best = max(best, complete)
    log.info("page %s: %s/%s cards complete", page, best, cards)
    return products, cards


def _scrape_page(
    driver: webdriver.Chrome, query: str, page: int, budget: float = 25.0
) -> Tuple[List[Dict[str, Any]], int]:
    url = _search_url(query, page)
    log.info("page %s: %s", page, url)
    driver.get(url)

    # International visitors get a country picker; choose United States if shown.
    us_links = driver.find_elements(By.CSS_SELECTOR, "a.us-link")
    if us_links:
        log.info("country splash detected, selecting US")
        us_links[0].click()

    try:
        # Waiting also lets a self-clearing JS challenge finish.
        WebDriverWait(driver, 45).until(_page_ready)
    except TimeoutException:
        if _blocked_reason(driver):
            raise BlockedError(f"blocked by bot protection (page title: {driver.title!r})")
        raise
    _pause(0.8, 1.6)
    products, cards = _load_all_cards(driver, page, budget)
    for p in products:
        p["page"] = page
    return products, cards


@contextmanager
def _browser(settings: Settings, route: str) -> Iterator[Tuple[webdriver.Chrome, str]]:
    """Open a browser for a route: "direct" (deployment IP) or "proxy" (Oxylabs)."""
    with ExitStack() as stack:
        port, label = None, "direct"
        if route == "proxy":
            upstream = oxylabs_proxy(settings)
            verify_upstream(upstream)  # fail fast on bad credentials instead of a hung page load
            port = stack.enter_context(ProxyForwarder(upstream)).port
            label = upstream.label
            log.info("using proxy %s:%s (%s)", upstream.host, upstream.port, label)
        driver = build_driver(settings, port)
        stack.callback(driver.quit)
        yield driver, label


def plan_routes(settings: Settings, start_with_proxy: bool) -> List[str]:
    """Routes to try, in order. Later routes are only used if an earlier one fails."""
    mode = settings.proxy_mode
    if mode == "always":
        return ["proxy"]
    if mode == "never" or not settings.proxy_configured:
        return ["direct"]
    # During a cooldown (direct IP recently blocked) go proxy-first, direct as backup.
    return ["proxy", "direct"] if start_with_proxy else ["direct", "proxy"]


def _error_text(exc: Exception) -> str:
    msg = getattr(exc, "msg", None) or str(exc) or type(exc).__name__
    return msg.strip().splitlines()[0]


def scrape_page(
    query: str, page: int, settings: Settings, start_with_proxy: bool = False
) -> Dict[str, Any]:
    """Scrape one Best Buy search-results page.

    Routes are tried in order (see ``plan_routes``): normally the deployment's own IP
    first, then Oxylabs if that fails (bot wall, timeout, network error) or returns a
    degraded page where most products lack data. Data from a degraded page is kept and
    overwritten by better data from the next route.

    Returns {"query", "page", "route", "fell_back", "proxy_failed", "cards",
    "products", "errors"}. ``cards == 0`` with no errors means the page had no results.
    """
    routes = plan_routes(settings, start_with_proxy)
    result: Dict[str, Any] = {
        "query": query, "page": page, "route": None, "fell_back": False,
        "proxy_failed": False, "cards": 0, "products": [], "errors": [],
    }
    used: List[str] = []
    by_sku: Dict[str, Dict[str, Any]] = {}  # later (better) routes overwrite earlier data

    def fail(label: str, error: str) -> None:
        log.warning("%r page %s failed via %s: %s", query, page, label, error)
        result["errors"].append({"page": page, "route": label, "error": error})

    # Spread concurrent browsers out instead of hitting the site in lockstep.
    _pause(settings.request_jitter_min, settings.request_jitter_max)

    for i, route in enumerate(routes):
        has_fallback = i < len(routes) - 1
        try:
            with _browser(settings, route) as (driver, label):
                used.append(label)
                try:
                    products, cards = _scrape_page(driver, query, page, settings.page_load_budget)
                except ROUTE_FAILURES as exc:
                    fail(label, _error_text(exc))
                    if has_fallback and route == "direct":
                        result["fell_back"] = True
                    continue
        except ProxyError as exc:
            used.append("oxylabs (unavailable)")
            fail("oxylabs", str(exc))
            result["proxy_failed"] = True
            continue

        by_sku.update((p["sku"], p) for p in products)
        result["cards"] = max(result["cards"], cards)
        complete = sum(map(_is_complete, products))
        log.info("%r page %s via %s: %s cards, %s complete", query, page, label, cards, complete)
        if has_fallback and cards and complete < cards * MIN_COMPLETE_RATIO:
            fail(label, f"incomplete page: {complete}/{cards} products have price data "
                        "(Best Buy withholds data from this IP's region)")
            if route == "direct":
                result["fell_back"] = True
            continue
        break  # good page (or the last route): done

    result["products"] = list(by_sku.values())
    result["route"] = " -> ".join(used)
    return result
