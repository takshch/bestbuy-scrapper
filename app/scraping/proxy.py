"""Oxylabs proxy configuration plus a local authenticating forwarder.

Chrome ignores credentials in --proxy-server, so Selenium can't log in to an
authenticated proxy on its own. We start a tiny proxy on 127.0.0.1 that adds
the `Proxy-Authorization` header and forwards everything to Oxylabs. Chrome
points at the local port with no credentials needed.
"""
import asyncio
import base64
import logging
import secrets
import socket
import threading
from dataclasses import dataclass
from typing import Optional

from app.config import Settings

log = logging.getLogger("scraper.proxy")


@dataclass
class UpstreamProxy:
    host: str
    port: int
    username: str
    password: str
    label: str


def oxylabs_proxy(settings: Settings) -> Optional[UpstreamProxy]:
    """Build US-targeted Oxylabs credentials.

    - Residential: pr.oxylabs.io:7777, user "customer-<USER>-cc-US[-st-us_<state>]-sessid-<id>-sesstime-15".
      The session id keeps one exit IP for every page of a scrape job.
    - Datacenter: dc.oxylabs.io:8000, user "user-<USER>-country-US".
    """
    if not settings.proxy_configured:
        return None
    country = settings.oxylabs_country.upper()
    user = settings.oxylabs_username

    if settings.oxylabs_proxy_type.lower() == "datacenter":
        return UpstreamProxy(
            host=settings.oxylabs_host or "dc.oxylabs.io",
            port=int(settings.oxylabs_port or 8000),
            username=f"user-{user}-country-{country}",
            password=settings.oxylabs_password,
            label=f"oxylabs/datacenter/{country}",
        )
    session = secrets.token_hex(5)
    state = f"-st-{settings.oxylabs_state.lower()}" if settings.oxylabs_state else ""
    return UpstreamProxy(
        host=settings.oxylabs_host or "pr.oxylabs.io",
        port=int(settings.oxylabs_port or 7777),
        username=f"customer-{user}-cc-{country}{state}-sessid-{session}-sesstime-15",
        password=settings.oxylabs_password,
        label=f"oxylabs/residential/{country}{state} session={session}",
    )


class ProxyError(RuntimeError):
    """The upstream proxy is unreachable or rejected our credentials."""


def verify_upstream(upstream: UpstreamProxy, timeout: float = 15.0) -> None:
    """Open a test CONNECT through the proxy so bad credentials fail fast and clearly."""
    token = base64.b64encode(f"{upstream.username}:{upstream.password}".encode()).decode()
    request = (
        "CONNECT www.bestbuy.com:443 HTTP/1.1\r\nHost: www.bestbuy.com:443\r\n"
        f"Proxy-Authorization: Basic {token}\r\n\r\n"
    ).encode()
    try:
        with socket.create_connection((upstream.host, upstream.port), timeout=timeout) as sock:
            sock.sendall(request)
            status_line = sock.recv(1024).split(b"\r\n", 1)[0].decode(errors="replace")
    except OSError as exc:
        raise ProxyError(f"cannot reach Oxylabs at {upstream.host}:{upstream.port}: {exc}") from exc
    parts = status_line.split()
    code = parts[1] if len(parts) > 1 else "?"
    if code == "407":
        raise ProxyError("Oxylabs rejected the credentials (HTTP 407); check OXYLABS_USERNAME/PASSWORD and product type")
    if code != "200":
        raise ProxyError(f"Oxylabs proxy returned {status_line!r}")


class ProxyForwarder:
    """Local HTTP proxy (runs in its own thread) that authenticates to an upstream proxy."""

    def __init__(self, upstream: UpstreamProxy):
        self.upstream = upstream
        token = base64.b64encode(f"{upstream.username}:{upstream.password}".encode()).decode()
        self._auth_header = f"Proxy-Authorization: Basic {token}".encode()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    def __enter__(self) -> "ProxyForwarder":
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        if not ready.wait(10) or self.port is None:
            raise RuntimeError("local proxy forwarder failed to start")
        return self

    def __exit__(self, *exc) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    async def _shutdown(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _run(self, ready: threading.Event) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", 0)
        )
        self.port = self._server.sockets[0].getsockname()[1]
        ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        upstream_w = None
        try:
            head = await client_r.readuntil(b"\r\n\r\n")
            lines = head[:-4].split(b"\r\n")
            is_connect = lines[0].upper().startswith(b"CONNECT ")
            headers = [
                h for h in lines[1:]
                if not h.lower().startswith((b"proxy-authorization:", b"proxy-connection:", b"connection:"))
            ]
            # Plain-HTTP requests must not reuse the connection: later requests on it
            # would bypass header injection. CONNECT tunnels are opaque so that's fine.
            if not is_connect:
                headers.append(b"Connection: close")
            request = b"\r\n".join([lines[0], self._auth_header, *headers]) + b"\r\n\r\n"

            upstream_r, upstream_w = await asyncio.open_connection(self.upstream.host, self.upstream.port)
            upstream_w.write(request)
            await upstream_w.drain()
            await asyncio.gather(_pipe(client_r, upstream_w), _pipe(upstream_r, client_w))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError) as exc:
            log.debug("proxy connection error: %s", exc)
        finally:
            for w in (client_w, upstream_w):
                if w is not None:
                    w.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        if writer.can_write_eof():
            try:
                writer.write_eof()
            except (ConnectionError, OSError):
                pass
