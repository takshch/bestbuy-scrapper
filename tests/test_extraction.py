"""Run the real in-page extraction code in CloakBrowser against saved fixture pages.

    RUN_BROWSER_TESTS=1 pytest -m browser
"""
import functools
import http.server
import threading
import time
from pathlib import Path

import pytest

from app.scraping import bestbuy as bb
from tests.conftest import make_settings

pytestmark = pytest.mark.browser
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def driver():
    d = bb.build_driver(make_settings(), None)
    yield d
    d.quit()


@pytest.fixture(scope="module")
def fixture_server():
    """Serve the fixtures over HTTP (file:// pages behave differently and may be sandboxed)."""
    handler = functools.partial(QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def serve(monkeypatch, fixture_server):
    """Point the scraper's search URL at a fixture page."""
    def use(name):
        monkeypatch.setattr(bb, "_search_url", lambda q, p: f"{fixture_server}/{name}")
        monkeypatch.setattr(bb, "_pause", lambda *a: None)
    return use


def by_sku(products):
    return {p["sku"]: p for p in products}


def test_current_layout_merges_apollo_cache_and_dom(driver, serve):
    serve("search_current.html")
    products, cards = bb._scrape_page(driver, "laptop", 1, budget=3)
    items = by_sku(products)

    assert cards == 3 and set(items) == {"1111", "2222", "3333"}, "deduped; carousel ignored"
    hp = items["1111"]
    # Cache wins over DOM (DOM showed $249.99); open-box variant ignored; __ref resolved.
    assert (hp["price"], hp["regular_price"], hp["model"]) == (229.99, 349.99, "14-dq6105dx")
    assert (hp["rating"], hp["review_count"]) == (4.4, 1103)
    assert hp["availability"] == "ADD_TO_CART", "DOM fills fields the cache lacks"

    yoga = items["2222"]  # never rendered: cache only
    assert (yoga["name"], yoga["price"], yoga["model"]) == ("Lenovo - Yoga 7i", 799.99, "83TC0003US")
    assert yoga["regular_price"] is None, "regular == customer price is not a sale"

    xps = items["3333"]  # DOM only
    assert (xps["price"], xps["regular_price"], xps["availability"]) == (1299.0, 1499.0, "SOLD_OUT")
    assert xps["url"] == "https://www.bestbuy.com/product/dell-xps/JJ3/sku/3333"
    assert hp["url"].startswith("https://www.bestbuy.com/"), "relative DOM links made absolute / cache URL used"
    assert all(p["page"] == 1 for p in products)


def test_legacy_layout_dom_extraction(driver, serve):
    serve("search_legacy.html")
    products, _ = bb._scrape_page(driver, "tv", 1, budget=2)
    (tv,) = products
    assert tv["sku"] == "6501234" and tv["name"].startswith("Samsung")
    assert (tv["price"], tv["regular_price"], tv["model"]) == (799.99, 999.99, "QN65Q60D")
    assert (tv["rating"], tv["review_count"], tv["availability"]) == (4.6, 2345, "ADD_TO_CART")


def test_hard_block_fails_fast(driver, serve):
    serve("access_denied.html")
    started = time.monotonic()
    with pytest.raises(bb.BlockedError, match="Access Denied"):
        bb._scrape_page(driver, "tv", 1, budget=2)
    assert time.monotonic() - started < 10, "must not wait for the 45s ready timeout"


def test_stealth_basics(driver, fixture_server):
    driver.get(f"{fixture_server}/search_legacy.html")
    assert driver.execute_script("return navigator.webdriver") in (False, None)
    assert "Headless" not in driver.execute_script("return navigator.userAgent")
    assert driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().timeZone") == "America/New_York"
