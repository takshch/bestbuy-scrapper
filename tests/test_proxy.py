import asyncio
import base64
import socket
import threading

import pytest

from app.scraping.proxy import ProxyError, ProxyForwarder, UpstreamProxy, oxylabs_proxy, verify_upstream
from tests.conftest import make_settings


def test_no_credentials_means_no_proxy():
    assert oxylabs_proxy(make_settings()) is None


def test_residential_username_targets_us_with_sticky_session():
    up = oxylabs_proxy(make_settings(oxylabs_username="alice", oxylabs_password="pw"))
    assert (up.host, up.port) == ("pr.oxylabs.io", 7777)
    assert up.username.startswith("customer-alice-cc-US-sessid-")
    assert up.username.endswith("-sesstime-15")
    # Each call opens a new sticky session (new exit IP per task).
    assert up.username != oxylabs_proxy(make_settings(oxylabs_username="alice", oxylabs_password="pw")).username


def test_residential_state_targeting():
    up = oxylabs_proxy(make_settings(oxylabs_username="a", oxylabs_password="p", oxylabs_state="US_New_York"))
    assert "-cc-US-st-us_new_york-sessid-" in up.username


def test_datacenter_username_and_endpoint():
    up = oxylabs_proxy(make_settings(oxylabs_username="a", oxylabs_password="p", oxylabs_proxy_type="datacenter"))
    assert (up.host, up.port, up.username) == ("dc.oxylabs.io", 8000, "user-a-country-US")


class MockUpstream:
    """A fake upstream proxy: records the request head, answers with a fixed status,
    and echoes tunnel data on 200."""

    def __init__(self, status: bytes = b"200 Connection established"):
        self.status = status
        self.heads = []
        self.loop = asyncio.new_event_loop()
        self.server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.port = self.server.sockets[0].getsockname()[1]
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    async def _handle(self, r, w):
        self.heads.append((await r.readuntil(b"\r\n\r\n")).decode())
        w.write(b"HTTP/1.1 " + self.status + b"\r\n\r\n")
        await w.drain()
        if self.status.startswith(b"200"):
            w.write(b"echo:" + await r.read(100))
            await w.drain()
        w.close()

    def close(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def upstream():
    servers = []

    def make(status=b"200 Connection established"):
        mock = MockUpstream(status)
        servers.append(mock)
        return mock, UpstreamProxy("127.0.0.1", mock.port, "customer-u-cc-US", "secret", "test")

    yield make
    for s in servers:
        s.close()


def test_forwarder_injects_credentials_and_tunnels(upstream):
    mock, up = upstream()
    with ProxyForwarder(up) as fwd:
        c = socket.create_connection(("127.0.0.1", fwd.port), timeout=5)
        c.sendall(b"CONNECT www.bestbuy.com:443 HTTP/1.1\r\nHost: www.bestbuy.com:443\r\n"
                  b"Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic stale\r\n\r\n")
        assert c.recv(100).startswith(b"HTTP/1.1 200")
        c.sendall(b"hello")
        assert c.recv(100) == b"echo:hello"
        c.close()

    head = mock.heads[0]
    token = base64.b64encode(b"customer-u-cc-US:secret").decode()
    assert f"Proxy-Authorization: Basic {token}" in head
    assert "Basic stale" not in head and "Proxy-Connection" not in head


def test_forwarder_forces_connection_close_for_plain_http(upstream):
    mock, up = upstream(b"404 Not Found")
    with ProxyForwarder(up) as fwd:
        c = socket.create_connection(("127.0.0.1", fwd.port), timeout=5)
        c.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nConnection: keep-alive\r\n\r\n")
        c.recv(100)
        c.close()
    assert "Connection: close" in mock.heads[0] and "keep-alive" not in mock.heads[0]


def test_verify_upstream_accepts_200(upstream):
    _, up = upstream()
    verify_upstream(up, timeout=5)


def test_verify_upstream_reports_bad_credentials(upstream):
    _, up = upstream(b"407 Proxy Authentication Required")
    with pytest.raises(ProxyError, match="407"):
        verify_upstream(up, timeout=5)


def test_verify_upstream_reports_unreachable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed port
    with pytest.raises(ProxyError, match="cannot reach"):
        verify_upstream(UpstreamProxy("127.0.0.1", port, "u", "p", "x"), timeout=2)
