"""Outbound URL policy and SSRF-safe HTTP transport.

Defences:
* scheme allowlist (http/https), port allowlist (80/443 by default)
* credentials-in-URL rejected
* every resolved address must be globally routable (blocks loopback, RFC1918,
  link-local incl. 169.254.169.254 metadata, CGNAT, multicast, reserved,
  IPv4-mapped IPv6 tricks)
* the *connection itself* is made to the validated IP inside a custom
  httpcore network backend, so DNS rebinding between check and connect
  cannot redirect the request to an internal address
* redirects are followed manually and each hop is re-validated
* domain allow/deny lists
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "metadata.google.internal",
                      "metadata", "instance-data"}
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa", ".home.arpa")


class URLRejected(Exception):
    pass


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip_is_public(ip.ipv4_mapped)
        if ip.sixtofour:
            return ip_is_public(ip.sixtofour)
        if ip.teredo:
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback
                                 or ip.is_link_local or ip.is_private or ip.is_unspecified)


def domain_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower().lstrip("*.").rstrip(".")
        if host == p or host.endswith("." + p):
            return True
    return False


@dataclass
class URLPolicy:
    allowed_domains: list[str]
    blocked_domains: list[str]
    allowed_ports: set[int] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.allowed_ports is None:
            self.allowed_ports = set(ALLOWED_PORTS)

    def check(self, url: str, extra_allowed: list[str] | None = None) -> str:
        """Validate URL syntax and domain policy. Returns the normalised host."""
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise URLRejected(f"malformed URL: {exc}") from exc
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            raise URLRejected(f"scheme '{parts.scheme}' not allowed")
        if parts.username or parts.password:
            raise URLRejected("credentials in URL are not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise URLRejected("missing host")
        try:
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise URLRejected("invalid port") from exc
        if port not in self.allowed_ports:
            raise URLRejected(f"port {port} not allowed")
        if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
            raise URLRejected(f"host '{host}' is an internal name")
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None and not ip_is_public(literal):
            raise URLRejected(f"address {literal} is not public")
        # Numeric hosts in alternative encodings (2130706433, 0x7f.1, 127.1): real TLDs are never numeric.
        last_label = host.rsplit(".", 1)[-1]
        if literal is None and (last_label.isdigit() or last_label.startswith("0x")):
            raise URLRejected("numeric host encodings are not allowed")
        if domain_matches(host, self.blocked_domains):
            raise URLRejected(f"domain '{host}' is blocked")
        allowed = list(self.allowed_domains) + list(extra_allowed or [])
        if allowed and not domain_matches(host, allowed):
            raise URLRejected(f"domain '{host}' is not on the allowlist")
        return host


class PinnedOriginPolicy:
    """Accepts exactly one configured origin (scheme, host, port) — used for the
    user's own search backend, which may legitimately live on the LAN."""

    def __init__(self, origin_url: str):
        p = urlsplit(origin_url)
        self.scheme = p.scheme.lower()
        self.host = (p.hostname or "").lower()
        self.port = p.port or (443 if self.scheme == "https" else 80)

    def check(self, url: str, extra_allowed: list[str] | None = None) -> str:
        p = urlsplit(url)
        port = p.port or (443 if p.scheme.lower() == "https" else 80)
        if (p.scheme.lower(), (p.hostname or "").lower(), port) != (self.scheme, self.host, self.port) \
                or p.username or p.password:
            raise URLRejected("only the configured search backend origin is allowed")
        return self.host


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve host and require every address to be public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLRejected(f"DNS resolution failed for {host}: {exc}") from exc
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip_is_public(ip):
            raise URLRejected(f"{host} resolves to non-public address {ip}")
        addrs.append(str(ip))
    if not addrs:
        raise URLRejected(f"{host} did not resolve")
    return addrs


class ValidatingBackend(httpcore.SyncBackend):
    """Resolves and validates at connect time, then connects to the vetted IP.
    TLS SNI/certificate checks still use the original hostname."""

    def __init__(self, resolver=resolve_public):
        self._resolver = resolver

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last_exc: Exception | None = None
        for ip in self._resolver(host, port):
            try:
                return super().connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                           socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        raise last_exc or httpcore.ConnectError(f"could not connect to {host}")

    def connect_unix_socket(self, *a, **kw):  # pragma: no cover - defensive
        raise URLRejected("unix sockets are not allowed")


class SafeTransport(httpx.HTTPTransport):
    def __init__(self, resolver=resolve_public, **kw):
        super().__init__(**kw)
        ssl_context = self._pool._ssl_context  # reuse httpx' configured verification context
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            network_backend=ValidatingBackend(resolver),
        )


def safe_client(timeout_connect: float, timeout_read: float, user_agent: str,
                transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport or SafeTransport(),
        timeout=httpx.Timeout(timeout_read, connect=timeout_connect),
        follow_redirects=False,  # redirects are validated hop-by-hop by the fetcher
        trust_env=False,  # never route through env-configured proxies to internal hosts
        headers={"user-agent": user_agent, "accept": "text/html,application/xhtml+xml,application/xml,"
                 "application/rss+xml,application/atom+xml,text/plain,application/json;q=0.9,*/*;q=0.1"},
    )
