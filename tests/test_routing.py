"""Route selection and direct -> Oxylabs fallback for a single page (browser mocked)."""
from contextlib import contextmanager

import pytest

from app.db import _asyncpg_url
from app.scraping import bestbuy as bb
from tests.conftest import make_settings

CREDS = dict(oxylabs_username="u", oxylabs_password="p")


class FakeSite:
    """Scripted behaviour per route: "ok", "blocked", "degraded", "empty", or "proxy_auth"."""

    def __init__(self, direct="ok", proxy="ok"):
        self.behaviour = {"direct": direct, "proxy": proxy}
        self.calls = []

    @contextmanager
    def browser(self, settings, route):
        if route == "proxy" and self.behaviour["proxy"] == "proxy_auth":
            raise bb.ProxyError("Oxylabs rejected the credentials (HTTP 407)")
        yield route, "direct" if route == "direct" else "oxylabs/residential/US"

    def scrape(self, driver, query, page, budget=25.0):
        self.calls.append((driver, page))
        mode = self.behaviour[driver]
        if mode == "blocked":
            raise bb.BlockedError("blocked by bot protection (page title: 'Access Denied')")
        if mode == "empty":
            return [], 0
        products = [{"sku": f"sku{i}", "name": f"{driver} {i}", "price": None if mode == "degraded" else 9.99,
                     "src": driver} for i in range(4)]
        return products, 4


@pytest.fixture
def site(monkeypatch):
    def install(**behaviour):
        fake = FakeSite(**behaviour)
        monkeypatch.setattr(bb, "_browser", fake.browser)
        monkeypatch.setattr(bb, "_scrape_page", fake.scrape)
        monkeypatch.setattr(bb, "_pause", lambda *a: None)
        return fake
    return install


@pytest.mark.parametrize("overrides, start_with_proxy, expected", [
    ({}, False, ["direct"]),                                        # no credentials
    (CREDS, False, ["direct", "proxy"]),                            # fallback mode
    (CREDS, True, ["proxy", "direct"]),                             # direct IP cooling down
    ({**CREDS, "proxy_mode": "always"}, False, ["proxy"]),
    ({**CREDS, "proxy_mode": "never"}, True, ["direct"]),
])
def test_plan_routes(overrides, start_with_proxy, expected):
    assert bb.plan_routes(make_settings(**overrides), start_with_proxy) == expected


def test_direct_success_never_touches_proxy(site):
    fake = site()
    r = bb.scrape_page("tv", 1, make_settings(**CREDS))
    assert r["route"] == "direct" and not r["fell_back"] and not r["errors"]
    assert len(r["products"]) == 4 and fake.calls == [("direct", 1)]


def test_blocked_direct_falls_back_to_proxy(site):
    site(direct="blocked")
    r = bb.scrape_page("tv", 2, make_settings(**CREDS))
    assert r["route"] == "direct -> oxylabs/residential/US"
    assert r["fell_back"] and {p["src"] for p in r["products"]} == {"proxy"}
    assert r["errors"][0]["route"] == "direct" and "Access Denied" in r["errors"][0]["error"]


def test_degraded_direct_page_falls_back_and_proxy_data_wins(site):
    site(direct="degraded")
    r = bb.scrape_page("tv", 1, make_settings(**CREDS))
    assert r["fell_back"] and "incomplete page: 0/4" in r["errors"][0]["error"]
    assert all(p["src"] == "proxy" and p["price"] == 9.99 for p in r["products"])


def test_degraded_page_kept_when_proxy_unusable(site):
    site(direct="degraded", proxy="proxy_auth")
    r = bb.scrape_page("tv", 1, make_settings(**CREDS))
    assert r["route"] == "direct -> oxylabs (unavailable)"
    assert r["proxy_failed"] and len(r["products"]) == 4  # partial data beats none
    assert "407" in r["errors"][-1]["error"]


def test_degraded_page_accepted_without_fallback(site):
    site(direct="degraded")
    r = bb.scrape_page("tv", 1, make_settings())
    assert r["route"] == "direct" and not r["errors"] and len(r["products"]) == 4


def test_cooldown_goes_proxy_first_and_uses_direct_if_proxy_down(site):
    fake = site(proxy="proxy_auth")
    r = bb.scrape_page("tv", 1, make_settings(**CREDS), start_with_proxy=True)
    assert r["route"] == "oxylabs (unavailable) -> direct"
    assert not r["fell_back"] and fake.calls == [("direct", 1)]


def test_both_routes_blocked(site):
    site(direct="blocked", proxy="blocked")
    r = bb.scrape_page("tv", 1, make_settings(**CREDS))
    assert r["products"] == [] and len(r["errors"]) == 2


def test_empty_page_signals_end_of_results(site):
    site(direct="empty")
    r = bb.scrape_page("tv", 9, make_settings(**CREDS))
    assert r["cards"] == 0 and r["errors"] == [] and r["route"] == "direct"


def test_always_mode_skips_direct(site):
    fake = site()
    r = bb.scrape_page("tv", 1, make_settings(**CREDS, proxy_mode="always"))
    assert r["route"] == "oxylabs/residential/US" and fake.calls == [("proxy", 1)]


@pytest.mark.parametrize("raw, url, args", [
    ("postgresql://u:p@db:5432/x", "postgresql+asyncpg://u:p@db:5432/x", {}),
    ("postgresql://u:p@h/x?sslmode=require&application_name=a",
     "postgresql+asyncpg://u:p@h/x?application_name=a", {"ssl": "require"}),
    ("postgresql://u:p@h/x?sslmode=disable", "postgresql+asyncpg://u:p@h/x", {}),
])
def test_asyncpg_url(raw, url, args):
    assert _asyncpg_url(raw) == (url, args)


def test_search_url_is_encoded():
    assert bb._search_url("4k tv & soundbar", 3).endswith("st=4k+tv+%26+soundbar&cp=3&intl=nosplash")
