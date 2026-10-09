"""HTTP(S) client on ``http.client`` with SSRF protection and proxy support.

* Only http/https, no credentials in URLs, public IPs only (unless explicitly
  allowed for tests). The resolved address is **pinned** for the connection, so
  a DNS answer cannot change between the check and the connect (rebinding).
* Redirects are followed manually and every hop is re-validated.
* Honours ``https_proxy``/``http_proxy``/``no_proxy`` (CONNECT tunnelling) so
  it also works behind corporate or sandbox proxies; on the Pi there is none.
* Bodies are size-capped; gzip/deflate content-encoding is decoded.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    def __init__(self, message: str, *, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


class BlockedURL(FetchError):
    """The URL is not allowed (scheme, credentials, private address...)."""


class TooLarge(FetchError):
    pass


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_reserved


def _host_in_no_proxy(host: str, no_proxy: str) -> bool:
    host = host.lower().strip("[]")
    for raw in no_proxy.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if "/" in entry:
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
            continue
        entry = entry.lstrip("*").lstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def charset(self) -> str | None:
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                return value.strip("\"' ").lower()
        return None

    def text(self) -> str:
        enc = self.charset or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


@dataclass
class StreamResponse:
    url: str
    status: int
    headers: dict[str, str]
    _resp: http.client.HTTPResponse
    _decoder: Any = None
    read_bytes: int = 0

    def read(self, n: int = 1 << 16) -> bytes:
        data = self._resp.read(n)
        self.read_bytes += len(data)
        if self._decoder is not None:
            return bytes(self._decoder.decompress(data)) if data else bytes(self._decoder.flush())
        return data

    def iter_chunks(self, n: int = 1 << 16) -> Iterator[bytes]:
        while True:
            chunk = self.read(n)
            if not chunk:
                return
            yield chunk


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip
        self._ctx = context

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)


@dataclass
class HttpClient:
    user_agent: str = "PolymathBot/0.1"
    timeout: float = 30.0
    max_redirects: int = 5
    allow_private: bool = False
    resolver: Resolver = default_resolver
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    context: ssl.SSLContext = field(default_factory=ssl.create_default_context)
    requests_made: int = 0

    # -------------------------------------------------------------- validation
    def validate(self, url: str) -> tuple[str, str, int, str]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise BlockedURL(f"scheme not allowed: {scheme or '(none)'}")
        if parts.username or parts.password:
            raise BlockedURL("credentials in URL are not allowed")
        host = (parts.hostname or "").lower()
        if not host:
            raise BlockedURL("URL has no host")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise BlockedURL(f"bad port: {exc}") from exc
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return scheme, host, port, path

    def _proxy_for(self, scheme: str, host: str) -> tuple[str, int] | None:
        no_proxy = self.env.get("no_proxy") or self.env.get("NO_PROXY") or ""
        if no_proxy and _host_in_no_proxy(host, no_proxy):
            return None
        proxy = self.env.get(f"{scheme}_proxy") or self.env.get(f"{scheme.upper()}_PROXY")
        if not proxy:
            return None
        p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if not p.hostname:
            return None
        return p.hostname, p.port or 8080

    def _resolve_public(self, host: str, port: int, *, via_proxy: bool) -> str | None:
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            addrs = [str(literal)]
        except ValueError:
            internal = host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            if internal and not self.allow_private:
                raise BlockedURL(f"internal hostname: {host}") from None
            try:
                addrs = self.resolver(host, port)
            except OSError as exc:
                if via_proxy:
                    return None  # the proxy resolves; we cannot pin
                raise FetchError(f"DNS failure for {host}: {exc}", transient=True) from exc
        if not addrs:
            raise FetchError(f"no address for {host}", transient=True)
        if not self.allow_private:
            bad = [a for a in addrs if not is_public_address(a)]
            if bad:
                raise BlockedURL(f"{host} resolves to non-public address {bad[0]}")
        return addrs[0]

    # --------------------------------------------------------------- requests
    def _connect(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        proxy = self._proxy_for(scheme, host)
        ip = self._resolve_public(host, port, via_proxy=proxy is not None)
        if proxy is not None:
            if scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    proxy[0], proxy[1], timeout=self.timeout, context=self.context
                )
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPConnection(proxy[0], proxy[1], timeout=self.timeout)
            return conn
        assert ip is not None
        if scheme == "https":
            return _PinnedHTTPSConnection(host, port, ip, self.timeout, self.context)
        return _PinnedHTTPConnection(host, port, ip, self.timeout)

    @contextmanager
    def stream(
        self, url: str, headers: Mapping[str, str] | None = None, *, method: str = "GET", decode: bool = True
    ) -> Iterator[StreamResponse]:
        """Open ``url`` following redirects; yields a streaming response."""
        hdrs = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip" if decode else "identity"}
        hdrs.update(headers or {})
        current = url
        for _hop in range(self.max_redirects + 1):
            scheme, host, port, path = self.validate(current)
            conn = self._connect(scheme, host, port)
            proxied = isinstance(conn, http.client.HTTPConnection) and conn.host != host and scheme == "http"
            target = current if proxied else path
            try:
                self.requests_made += 1
                conn.request(method, target, headers={**hdrs, "Host": host if port in (80, 443) else f"{host}:{port}"})
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                raise FetchError(f"{type(exc).__name__}: {exc}", transient=True) from exc
            status = resp.status
            rh = {k.lower(): v for k, v in resp.getheaders()}
            if status in (301, 302, 303, 307, 308) and "location" in rh:
                resp.read(65536)
                conn.close()
                current = urljoin(current, rh["location"])
                if status == 303:
                    method = "GET"
                continue
            decoder = None
            enc = rh.get("content-encoding", "").lower()
            if decode and enc in {"gzip", "x-gzip"}:
                decoder = zlib.decompressobj(wbits=31)
            elif decode and enc == "deflate":
                decoder = zlib.decompressobj()
            sr = StreamResponse(current, status, rh, resp, decoder)
            try:
                yield sr
            finally:
                conn.close()
            return
        raise FetchError(f"too many redirects for {url}")

    def get(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        max_bytes: int = 10_000_000,
        ok: tuple[int, ...] = (200,),
    ) -> Response:
        """GET a whole body (capped). Raises FetchError for non-``ok`` statuses."""
        with self.stream(url, headers) as sr:
            if sr.status not in ok:
                transient = sr.status in (408, 425, 429) or sr.status >= 500
                raise FetchError(f"HTTP {sr.status} for {url}", status=sr.status, transient=transient)
            buf = bytearray()
            for chunk in sr.iter_chunks():
                buf += chunk
                if len(buf) > max_bytes:
                    raise TooLarge(f"body exceeds {max_bytes} bytes: {url}", status=sr.status)
            return Response(sr.url, sr.status, sr.headers, bytes(buf))

    def get_range(self, url: str, start: int, end: int, *, if_range: str | None = None) -> Response:
        """Fetch bytes [start, end] inclusive; requires a 206 answer."""
        headers = {"Range": f"bytes={start}-{end}"}
        if if_range:
            headers["If-Range"] = if_range
        with self.stream(url, headers, decode=False) as sr:
            if sr.status != 206:
                raise FetchError(
                    f"expected 206 for range request, got {sr.status}",
                    status=sr.status,
                    transient=sr.status >= 500 or sr.status == 429,
                )
            body = b"".join(sr.iter_chunks())
            return Response(sr.url, sr.status, sr.headers, body)


def client_from_config(senses: Any) -> HttpClient:
    ua = senses.user_agent
    if senses.contact:
        ua = f"{ua} contact:{senses.contact}"
    return HttpClient(
        user_agent=ua,
        timeout=senses.timeout,
        max_redirects=senses.max_redirects,
        allow_private=senses.allow_private_networks,
    )
