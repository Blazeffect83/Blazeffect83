"""SSRF defences: URL policy, connect-time IP validation, redirects, limits."""
import http.server
import threading

import httpx
import pytest

from app.research.fetcher import FetchError, Fetcher
from app.security.network_policy import (PinnedOriginPolicy, SafeTransport, URLPolicy, URLRejected, ip_is_public,
                                         resolve_public)

POLICY = URLPolicy([], [])


@pytest.mark.parametrize("url", [
    "http://localhost/admin", "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f000001/",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://100.64.0.1/", "http://0.0.0.0/",
    "http://printer.local/", "http://router.lan/", "http://metadata.google.internal/",
    "file:///etc/passwd", "ftp://example.com/", "gopher://example.com/", "http://user:pw@example.com/",
    "http://example.com:22/", "http://example.com:8600/", "javascript:alert(1)",
])
def test_internal_and_unsafe_urls_rejected(url):
    with pytest.raises(URLRejected):
        POLICY.check(url)


def test_public_url_accepted_and_allowlist():
    assert POLICY.check("https://docs.python.org/3/") == "docs.python.org"
    p = URLPolicy(["python.org"], ["evil.python.org"])
    assert p.check("https://docs.python.org/x") == "docs.python.org"
    with pytest.raises(URLRejected):
        p.check("https://example.com/")
    with pytest.raises(URLRejected):
        p.check("https://evil.python.org/")
    with pytest.raises(URLRejected):  # suffix trick
        p.check("https://python.org.attacker.com/")


def test_ip_classification():
    import ipaddress
    assert ip_is_public(ipaddress.ip_address("93.184.216.34"))
    for bad in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:10.0.0.1", "fc00::1", "224.0.0.1"):
        assert not ip_is_public(ipaddress.ip_address(bad))


def test_resolver_rejects_private_answers(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.7", 80))])
    with pytest.raises(URLRejected):
        resolve_public("innocent.example.com", 80)


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>internal admin panel</p>")

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def test_dns_rebinding_blocked_at_connect_time(local_server):
    """A public-looking hostname that resolves to 127.0.0.1 must never be connected to."""
    port = local_server.server_address[1]
    resolver = lambda host, p: resolve_public("127.0.0.1", p)  # noqa: E731 - "rebinds" to loopback
    policy = URLPolicy([], [], allowed_ports={port})
    f = Fetcher(policy, max_bytes=10000, connect_timeout=2, read_timeout=2, max_redirects=2, user_agent="t",
                per_domain_delay=0, transport=SafeTransport(resolver=resolver), respect_robots=False)
    _Handler.hits.clear()
    with pytest.raises(FetchError) as e:
        f.fetch(f"http://rebind.example.com:{port}/admin")
    assert e.value.error_class == "policy"
    assert _Handler.hits == []  # the internal server never received a request


def _fetcher(web, **kw):
    return Fetcher(URLPolicy(kw.pop("allowed", []), []), max_bytes=kw.pop("max_bytes", 100_000), connect_timeout=2,
                   read_timeout=2, max_redirects=kw.pop("max_redirects", 3), user_agent="AEGIS-test",
                   per_domain_delay=0, transport=web.transport, respect_robots=kw.pop("robots", True))


def test_redirect_to_internal_address_rejected(web):
    web.redirect("https://public.example.com/go", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(FetchError) as e:
        _fetcher(web).fetch("https://public.example.com/go")
    assert e.value.error_class == "policy"
    assert not any("169.254" in u for u in web.requests)


def test_redirect_chain_limit(web):
    for i in range(6):
        web.redirect(f"https://a.example.com/{i}", f"https://a.example.com/{i + 1}")
    with pytest.raises(FetchError) as e:
        _fetcher(web, max_redirects=3).fetch("https://a.example.com/0")
    assert e.value.error_class == "redirects"


def test_redirect_off_allowlist_rejected(web):
    web.redirect("https://docs.python.org/x", "https://evil.example.net/")
    with pytest.raises(FetchError):
        _fetcher(web, allowed=["python.org"]).fetch("https://docs.python.org/x")


def test_size_limit_enforced(web):
    web.add("https://big.example.com/", "<p>" + "a" * 50_000 + "</p>")
    with pytest.raises(FetchError) as e:
        _fetcher(web, max_bytes=10_000).fetch("https://big.example.com/")
    assert e.value.error_class == "too_large"


def test_content_type_restricted(web):
    web.add("https://x.example.com/a.bin", b"\x00\x01", content_type="application/octet-stream")
    with pytest.raises(FetchError) as e:
        _fetcher(web).fetch("https://x.example.com/a.bin")
    assert e.value.error_class == "content_type"


def test_robots_txt_respected(web):
    web.add("https://r.example.com/robots.txt", "User-agent: *\nDisallow: /private", content_type="text/plain")
    web.add("https://r.example.com/private/page", "<p>secret</p>")
    with pytest.raises(FetchError) as e:
        _fetcher(web).fetch("https://r.example.com/private/page")
    assert e.value.error_class == "robots"


def test_transient_errors_classified(web):
    web.add("https://t.example.com/", "err", status=503)
    with pytest.raises(FetchError) as e:
        _fetcher(web).fetch("https://t.example.com/")
    assert e.value.transient


def test_pinned_origin_policy():
    p = PinnedOriginPolicy("http://192.168.1.20:8888")
    assert p.check("http://192.168.1.20:8888/search?q=x") == "192.168.1.20"
    for bad in ("http://192.168.1.20:22/", "http://192.168.1.21:8888/", "https://192.168.1.20:8888/",
                "http://u:p@192.168.1.20:8888/"):
        with pytest.raises(URLRejected):
            p.check(bad)


def test_safe_transport_is_default_for_research(services):
    # Production fetcher (no injected transport) must use the validating transport.
    f = Fetcher(URLPolicy([], []), max_bytes=1, connect_timeout=1, read_timeout=1, max_redirects=1,
                user_agent="x")
    assert isinstance(f._client._transport, SafeTransport)
    assert f._client._trust_env is False
    assert isinstance(httpx.Client, type)
