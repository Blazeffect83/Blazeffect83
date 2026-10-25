"""v0.9.0: private access from your own browser: home network only, HTTPS, one-time pairing, revocable devices."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import ssl
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.core.config import ACCESS_NETWORKS
from polymath.interface import access as acc
from polymath.interface import cli, dashboard
from polymath.interface.dashboard import Gate, RateLimiter, gate_for, make_remote_server, make_server, tls_context

ROOT = Path(__file__).resolve().parent.parent
CERT_SCRIPT = ROOT / "scripts" / "access-cert.sh"
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ the registry
def test_pairing_gives_a_device_key_once_and_only_its_hash_is_kept(tmp_path):
    clock = Clock()
    a = acc.Access(tmp_path / "access", clock=clock)
    with pytest.raises(acc.PairError, match="no pairing code is waiting"):
        a.pair("AAAA-BBBB", name="x", ip="192.168.1.20")
    code = a.new_code()
    assert len(code) == 9 and code[4] == "-" and set(code.replace("-", "")) <= set(acc.ALPHABET)
    assert a.code_active()
    token, dev = a.pair(code.lower().replace("-", " "), name="Chrome on Windows", ip="192.168.1.20")
    assert len(token) >= 40 and dev.name == "Chrome on Windows" and dev.last_ip == "192.168.1.20"
    assert not a.code_active()  # one use
    with pytest.raises(acc.PairError, match="no pairing code"):
        a.pair(code, name="again", ip="192.168.1.21")
    stored = (tmp_path / "access" / acc.DEVICES).read_text()
    assert token not in stored and acc._digest(token) in stored  # a stolen file opens nothing
    assert stat.S_IMODE((tmp_path / "access").stat().st_mode) == 0o700
    for name in (acc.DEVICES, acc.CODE, acc.LOG):
        assert stat.S_IMODE((tmp_path / "access" / name).stat().st_mode) == 0o600, name
    assert a.check(token, ip="192.168.1.30") is not None and a.check("not-it", ip="x") is None
    assert a.check(None, ip="x") is None and a.check("x" * 300, ip="x") is None
    events = [e["event"] for e in a.history()]
    assert events == ["code", "paired"] and code.replace("-", "") not in json.dumps(a.history())


def test_wrong_guesses_cancel_the_code_and_expiry_and_attempt_limits_hold(tmp_path):
    clock = Clock()
    a = acc.Access(tmp_path, clock=clock)
    a.new_code()
    for left in (4, 3, 2, 1):
        with pytest.raises(acc.PairError, match=f"{left} tries left"):
            a.pair("ZZZZ-ZZZZ", name="x", ip=f"10.0.0.{left}")
    with pytest.raises(acc.PairError, match="that code is cancelled"):
        a.pair("ZZZZ-ZZZZ", name="x", ip="10.0.0.9")
    assert not a.code_active()
    code = a.new_code()
    clock.t += acc.CODE_TTL + 1
    with pytest.raises(acc.PairError, match="expired"):
        a.pair(code, name="x", ip="10.0.0.5")
    a.new_code()
    for _ in range(acc.IP_TRIES):  # one address hammering away, whatever the code
        with pytest.raises(acc.PairError):
            a.pair("ZZZZ-ZZZZ", name="x", ip="10.0.0.66")
        a.new_code()
    with pytest.raises(acc.PairError, match="too many attempts from this computer"):
        a.pair("ZZZZ-ZZZZ", name="x", ip="10.0.0.66")
    clock.t += acc.IP_WINDOW + 1
    good = a.new_code()
    assert a.pair(good, name="x", ip="10.0.0.66")[1].name == "x"  # ten minutes later it may try again
    for i in range(300):  # many addresses, long ago: forgotten, so the table stays small
        clock.t += 1
        with pytest.raises(acc.PairError):
            a.pair("ZZZZ-ZZZZ", name="x", ip=f"10.1.{i // 250}.{i % 250}")
    clock.t += acc.IP_WINDOW + 1
    with pytest.raises(acc.PairError):
        a.pair("ZZZZ-ZZZZ", name="x", ip="10.2.0.1")
    assert len(a._tries) <= 2


def test_devices_are_listed_revoked_and_seen_without_a_write_per_request(tmp_path, monkeypatch):
    clock = Clock()
    a = acc.Access(tmp_path, clock=clock)
    tokens = []
    for i in range(3):
        tokens.append(a.pair(a.new_code(), name=f"dev{i}", ip="192.168.1.2")[0])
    devs = a.devices()
    assert [d.name for d in devs] == ["dev0", "dev1", "dev2"]
    before = (tmp_path / acc.DEVICES).stat().st_mtime_ns
    clock.t += 5
    assert a.check(tokens[0], ip="192.168.1.9") is not None
    assert (tmp_path / acc.DEVICES).stat().st_mtime_ns == before  # last-seen kept in memory first
    clock.t += acc.SEEN_EVERY + 1
    a.check(tokens[0], ip="192.168.1.9")
    assert json.loads((tmp_path / acc.DEVICES).read_text())["devices"][0]["last_ip"] == "192.168.1.9"
    other = acc.Access(tmp_path, clock=clock)  # the CLI, through the dashboard's other listener
    assert other.revoke(devs[1].id, by="test") == 1 and other.revoke("nope") == 0
    time.sleep(0.01)
    os.utime(tmp_path / acc.DEVICES)  # a new mtime, as a real write would make
    assert a.check(tokens[1], ip="x") is None and a.check(tokens[2], ip="x") is not None  # it re-reads the file
    assert a.revoke("all") == 2 and a.devices() == []
    a.flush()
    monkeypatch.setattr(acc, "MAX_DEVICES", 1)
    a.pair(a.new_code(), name="one", ip="x")
    with pytest.raises(acc.PairError, match="revoke one first"):
        a.pair(a.new_code(), name="two", ip="x")


def test_the_access_log_stays_small(tmp_path, monkeypatch):
    monkeypatch.setattr(acc, "LOG_KEEP", 20)
    a = acc.Access(tmp_path)
    for i in range(400):
        a.log("refused", ip=f"10.0.0.{i % 250}", why="wrong code")
    lines = (tmp_path / acc.LOG).read_text().splitlines()
    assert len(lines) <= 20 * 200 // 40 + 21 and json.loads(lines[-1])["ip"] == "10.0.0.149"
    assert len(a.history(5)) == 5


# ------------------------------------------------------------------ the checks
def test_home_network_host_names_origins_and_cookies():
    nets = acc.networks(ACCESS_NETWORKS)
    for ip in ("192.168.1.20", "10.1.2.3", "172.20.0.5", "127.0.0.1", "::1", "fe80::1%eth0", "::ffff:192.168.0.9"):
        assert acc.client_allowed(ip, nets), ip
    for ip in ("8.8.8.8", "100.64.0.1", "2001:4860::1", "172.32.0.1", "nonsense", ""):
        assert not acc.client_allowed(ip, nets), ip
    names = {"pis", "pis.local", "localhost"}
    for good in ("192.168.1.20:8443", "[fe80::1]:8443", "pis:8443", "PIS.local", "localhost", "pis.local."):
        assert acc.host_allowed(good, names), good
    for bad in ("evil.example:8443", "", None, "[]:1", "pis.evil.example"):
        assert not acc.host_allowed(bad, names), bad
    assert acc.same_origin("https://pis.local:8443", "pis.local:8443")
    assert not acc.same_origin("https://evil.example", "pis.local:8443") and not acc.same_origin(None, "x")
    assert acc.same_origin("http://127.0.0.1:8765", "127.0.0.1:8765", "http")
    assert acc.cookie_token("a=1; pm_device=abc; b=2") == "abc" and acc.cookie_token("pm_device=") is None
    assert acc.cookie_token(None) is None
    cookie = acc.set_cookie("tok")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", f"Max-Age={acc.COOKIE_MAX_AGE}"):
        assert flag in cookie
    assert "Max-Age=0" in acc.clear_cookie()
    win = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    assert acc.device_name(win) == "Chrome on Windows" and acc.device_name(win, "  My <b>PC</b> ") == "My bPCb"
    assert acc.device_name("curl/8") == "a browser" and acc.device_name(None) == "a browser"
    assert "localhost" in acc.host_names(["Extra.Name "]) and "extra.name" in acc.host_names(["Extra.Name "])


def test_fingerprints_and_addresses(tmp_path, monkeypatch):
    assert acc.fingerprints(tmp_path / "missing.crt") is None
    (tmp_path / "bad.crt").write_text("-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n")
    assert acc.fingerprints(tmp_path / "bad.crt") is None
    (tmp_path / "none.crt").write_text("hello")
    assert acc.fingerprints(tmp_path / "none.crt") is None
    monkeypatch.setattr(acc, "lan_addresses", lambda: ["192.168.1.50"])
    monkeypatch.setattr("socket.gethostname", lambda: "pis.home")
    assert acc.lan_urls(8443) == ["https://192.168.1.50:8443/", "https://pis.local:8443/"]


# ------------------------------------------------------------------ the certificate script
def make_certs(tls: Path, *, addrs: str = "192.168.1.50") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, POLYMATH_TLS_DIR=str(tls), POLYMATH_HOSTNAME="pis", POLYMATH_ADDRS=addrs,
               POLYMATH_GROUP="no-such-group-xyz")  # fmt: skip
    return subprocess.run(["bash", str(CERT_SCRIPT)], env=env, capture_output=True, text=True, check=False)


@needs_openssl
def test_the_certificate_script_makes_renews_and_reports(tmp_path):
    tls = tmp_path / "tls"
    first = make_certs(tls)
    assert first.returncode == 0, first.stderr
    assert "making Polymath's certificate authority" in first.stdout
    assert stat.S_IMODE(tls.stat().st_mode) == 0o750
    assert stat.S_IMODE((tls / "ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tls / "server.key").stat().st_mode) == 0o640
    info = acc.cert_info(tls / "server.crt")
    assert info is not None and {"pis", "pis.local", "localhost"} <= set(info["names"])
    assert set(info["ips"]) == {"127.0.0.1", "192.168.1.50"} and info["not_after"] > time.time() + 390 * 86400
    v = subprocess.run(["openssl", "verify", "-CAfile", str(tls / "ca.crt"), str(tls / "server.crt")],
                       capture_output=True, text=True, check=False)  # fmt: skip
    assert v.returncode == 0, v.stdout + v.stderr
    ca_before = (tls / "ca.crt").read_bytes()
    assert "the certificate is fine" in make_certs(tls).stdout  # nothing to do
    moved = make_certs(tls, addrs="192.168.1.77 fe80::1")
    assert "does not name 192.168.1.77" in moved.stdout and (tls / "ca.crt").read_bytes() == ca_before
    assert "192.168.1.77" in (acc.cert_info(tls / "server.crt") or {})["ips"]  # the PC keeps trusting the same CA
    env = dict(os.environ, POLYMATH_TLS_DIR=str(tls), POLYMATH_HOSTNAME="pis", POLYMATH_ADDRS="192.168.1.77")
    status = subprocess.run(
        ["bash", str(CERT_SCRIPT), "--status"], env=env, capture_output=True, text=True, check=False
    )
    assert status.returncode == 0 and "SHA-1 thumbprint" in status.stdout and "fine until" in status.stdout
    fp = acc.fingerprints(tls / "ca.crt")
    assert fp is not None and fp["sha1"].replace(":", "") in status.stdout.replace(":", "")
    assert subprocess.run(["bash", str(CERT_SCRIPT), "--what"], capture_output=True, check=False).returncode == 2


# ------------------------------------------------------------------ the two listeners
@pytest.fixture()
def servers(config, tmp_path):
    """The Pi's loopback listener and the HTTPS listener for other devices, as serve() runs them."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    assert make_certs(config.access.tls_dir).returncode == 0
    from polymath.core.db import open_database

    open_database(config.paths.db_path).close()
    local = make_server(config, host="127.0.0.1", port=0, gate=gate_for(config, remote=False))
    ctx = tls_context(config.access.tls_dir)
    assert ctx is not None
    remote_gate = gate_for(config, remote=True)
    remote = make_remote_server(config, local.data, remote_gate, ctx, host="127.0.0.1", port=0)
    for s in (local, remote):
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield local, remote, remote_gate
    for s in (remote, local):
        s.shutdown()
        s.server_close()


def call(server: Any, method: str, path: str, headers: dict[str, str] | None = None, body: Any = None,
         *, tls: bool) -> tuple[int, dict[str, str], bytes]:  # fmt: skip
    port = server.server_address[1]
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn: http.client.HTTPConnection = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=ctx)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    h = {"Host": f"127.0.0.1:{port}"} | (headers or {})
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    conn.close()
    return out


def jpost(server: Any, path: str, body: Any, *, tls: bool, cookie: str = "", origin: str | None = None,
          ctype: str = "application/json") -> tuple[int, dict[str, str], bytes]:  # fmt: skip
    port = server.server_address[1]
    hdr = {"Content-Type": ctype}
    if origin is None:
        origin = f"https://127.0.0.1:{port}" if tls else ""
    if origin:
        hdr["Origin"] = origin
    if cookie:
        hdr["Cookie"] = cookie
    return call(server, "POST", path, hdr, body, tls=tls)


def test_a_browser_pairs_signs_in_and_is_locked_out_again(servers):
    local, remote, _gate = servers
    status, head, _ = call(remote, "GET", "/", tls=True)
    assert status == 303 and head["location"] == "/pair"
    assert call(remote, "GET", "/api/overview", tls=True)[0] == 401
    for public in ("/pair", "/static/pair.js", "/static/style.css", "/manifest.webmanifest", "/sw.js", "/ca.crt"):
        st, h, body = call(remote, "GET", public, tls=True)
        assert st == 200, public
    st, h, body = call(remote, "GET", "/ca.crt", tls=True)
    assert body.startswith(b"-----BEGIN CERTIFICATE") and "attachment" in h["content-disposition"]
    me = json.loads(call(remote, "GET", "/api/access/me", tls=True)[2])
    assert me["remote"] and me["device"] is None and "devices" not in me and me["fingerprint"]["sha1"]
    # the Pi makes a code (what `polymath access pair` asks for, on loopback)
    code = json.loads(jpost(local, "/api/access/code", {}, tls=False)[2])["code"]
    st, h, body = jpost(remote, "/api/access/pair", {"code": code, "name": "My PC"}, tls=True)
    assert st == 200 and json.loads(body)["device"]["name"] == "My PC"
    cookie = h["set-cookie"].split(";", 1)[0]
    assert "HttpOnly" in h["set-cookie"] and "Secure" in h["set-cookie"] and "SameSite=Strict" in h["set-cookie"]
    hdr = {"Cookie": cookie}
    assert call(remote, "GET", "/", hdr, tls=True)[0] == 200
    assert call(remote, "GET", "/api/overview", hdr, tls=True)[0] in {200, 503}
    me = json.loads(call(remote, "GET", "/api/access/me", hdr, tls=True)[2])
    assert me["device"]["name"] == "My PC" and me["devices"] == 1
    devs = json.loads(call(remote, "GET", "/api/access/devices", hdr, tls=True)[2])["devices"]
    assert devs[0]["this"] and devs[0]["name"] == "My PC"
    health = json.loads(call(remote, "GET", "/health", hdr, tls=True)[2])
    assert "version" not in health and "build" not in health  # other devices learn only whether it is up
    assert "version" in json.loads(call(local, "GET", "/health", tls=False)[2])
    # a paired browser can make a code for another device (no need to touch the Pi)
    assert json.loads(jpost(remote, "/api/access/code", {}, tls=True, cookie=cookie)[2])["code"]
    assert json.loads(call(remote, "GET", "/api/access/log", hdr, tls=True)[2])["log"]
    # sign out: the key stops working
    st, h, _ = jpost(remote, "/api/access/logout", {}, tls=True, cookie=cookie)
    assert st == 200 and "Max-Age=0" in h["set-cookie"]
    assert call(remote, "GET", "/api/overview", hdr, tls=True)[0] == 401
    assert jpost(remote, "/api/access/logout", {}, tls=True)[0] == 401  # an unpaired browser gets nothing


def test_everything_else_is_refused(servers, config):
    local, remote, gate = servers
    port = remote.server_address[1]
    assert call(remote, "GET", "/pair", {"Host": "evil.example"}, tls=True)[0] == 421  # DNS rebinding
    assert call(local, "GET", "/", {"Host": "evil.example:8765"}, tls=False)[0] == 421
    assert jpost(remote, "/api/access/pair", {"code": "x"}, tls=True, ctype="text/plain")[0] == 415
    assert jpost(remote, "/api/access/pair", {"code": "x"}, tls=True, origin="https://evil.example")[0] == 403
    assert jpost(remote, "/api/access/pair", {"code": "x"}, tls=True, origin="")[0] == 403  # no Origin: refused
    assert jpost(local, "/api/access/revoke", {"id": "all"}, tls=False, origin="https://evil.example")[0] == 403
    st, _h, body = jpost(local, "/api/access/pair", {"code": "x"}, tls=False)
    assert st == 400 and b"other device" in body  # pairing happens on the HTTPS listener
    assert jpost(remote, "/api/access/pair", [1], tls=True)[0] == 400
    assert jpost(remote, "/api/access/pair", b"{nope", tls=True)[0] == 400
    assert jpost(remote, "/api/access/pair", b"x" * 5000, tls=True)[0] == 413
    hdr = {"Content-Type": "application/json", "Origin": f"https://127.0.0.1:{port}", "Content-Length": "lots"}
    assert call(remote, "POST", "/api/access/pair", hdr, b"{}", tls=True)[0] == 413  # a garbage length: no crash
    assert jpost(local, "/api/access/revoke", {}, tls=False)[0] == 400
    assert json.loads(jpost(local, "/api/access/revoke", {"id": "all"}, tls=False)[2])["revoked"] == 0
    assert call(local, "GET", "/api/access/nothing", tls=False)[0] == 404
    assert jpost(local, "/api/access/nothing", {}, tls=False)[0] == 404
    gate.nets = acc.networks(["10.0.0.0/8"])  # as if this client were not on the home network
    st, _h, body = call(remote, "GET", "/pair", tls=True)
    assert st == 403 and b"home network only" in body
    assert any(e.get("why") == "not on the home network" for e in gate.access.history())
    # plain HTTP to the TLS port gets nothing at all
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            assert s.recv(100) == b""
        except ConnectionResetError:
            pass


def test_the_local_listener_lists_and_revokes_devices(servers):
    local, remote, _gate = servers
    code = json.loads(jpost(local, "/api/access/code", {}, tls=False)[2])["code"]
    st, h, body = jpost(remote, "/api/access/pair", {"code": code}, tls=True)
    dev_id = json.loads(body)["device"]["id"]
    cookie = h["set-cookie"].split(";", 1)[0]
    devs = json.loads(call(local, "GET", "/api/access/devices", tls=False)[2])["devices"]
    assert [d["id"] for d in devs] == [dev_id] and not devs[0]["this"]
    assert json.loads(jpost(local, "/api/access/revoke", {"id": dev_id}, tls=False)[2])["revoked"] == 1
    assert call(remote, "GET", "/", {"Cookie": cookie}, tls=True)[0] == 303  # revoked from the Pi: back to pairing
    # a device revoking itself gets its cookie cleared
    code = json.loads(jpost(local, "/api/access/code", {}, tls=False)[2])["code"]
    st, h, body = jpost(remote, "/api/access/pair", {"code": code}, tls=True)
    cookie, dev_id = h["set-cookie"].split(";", 1)[0], json.loads(body)["device"]["id"]
    st, h, _ = jpost(remote, "/api/access/revoke", {"id": dev_id}, tls=True, cookie=cookie)
    assert st == 200 and "Max-Age=0" in h["set-cookie"]


def test_the_cli_pairs_lists_revokes_and_explains(servers, config, capsys):
    local, remote, _gate = servers
    config.dashboard.port = local.server_address[1]
    args = argparse.Namespace(op="pair", which=None)
    assert cli.cmd_access(config, args) == 0
    out = capsys.readouterr().out
    code = out.split("Pairing code:", 1)[1].split()[0]
    assert len(code) == 9 and "thumbprint" in out and "Advanced" in out
    st, _h, _ = jpost(remote, "/api/access/pair", {"code": code, "name": "Laptop"}, tls=True)
    assert st == 200
    assert cli.cmd_access(config, argparse.Namespace(op="devices", which=None)) == 0
    listed = capsys.readouterr().out
    assert "Laptop" in listed
    dev_id = listed.split()[0]
    assert cli.cmd_access(config, argparse.Namespace(op="revoke", which=None)) == 2
    assert cli.cmd_access(config, argparse.Namespace(op="revoke", which="nope")) == 1
    assert cli.cmd_access(config, argparse.Namespace(op="revoke", which=dev_id)) == 0
    assert cli.cmd_access(config, argparse.Namespace(op="devices", which=None)) == 0
    assert "no paired devices" in capsys.readouterr().out
    assert cli.cmd_access(config, argparse.Namespace(op="log", which=None)) == 0
    assert "paired" in capsys.readouterr().out
    assert cli.cmd_access(config, argparse.Namespace(op="status", which=None)) == 0
    assert "paired devices: 0" in capsys.readouterr().out
    config.dashboard.port = 1  # nothing answers there
    assert cli.cmd_access(config, argparse.Namespace(op="status", which=None)) == 1
    assert "not answering" in capsys.readouterr().err


def test_doctor_says_whether_remote_access_is_ready(servers, config, monkeypatch):
    _local, remote, _gate = servers
    monkeypatch.setattr(acc, "lan_addresses", lambda: ["192.168.1.50"])
    config.access.port = remote.server_address[1]
    status, detail, fix = acc.access_check(config)
    assert status == "ok" and "paired devices" in detail and not fix
    monkeypatch.setattr(acc, "lan_addresses", lambda: ["192.168.1.99"])
    status, detail, fix = acc.access_check(config)
    assert status == "warn" and "192.168.1.99" in detail and "access-cert.sh" in fix
    monkeypatch.setattr(acc, "lan_addresses", lambda: [])
    status, _d, _f = acc.access_check(config, now=time.time() + 400 * 86400)
    assert status == "bad"
    config.access.port = 1
    assert acc.access_check(config)[0] == "warn"
    config.access.remote = False
    assert acc.access_check(config)[0] == "ok"
    config.access.remote = True
    config.access.tls_dir = config.paths.data_dir / "no-tls"
    assert "no HTTPS certificate" in acc.access_check(config)[1]


# ------------------------------------------------------------------ smaller parts
def test_tls_context_needs_both_files_and_a_valid_pair(tmp_path):
    assert tls_context(tmp_path) is None
    (tmp_path / "server.crt").write_text("not a certificate")
    (tmp_path / "server.key").write_text("not a key")
    assert tls_context(tmp_path) is None
    assert dashboard.loopback("127.0.0.1") and dashboard.loopback("::1") and dashboard.loopback("localhost")
    assert not dashboard.loopback("0.0.0.0") and not dashboard.loopback("192.168.1.2")


def test_the_rate_limiter_forgets_idle_clients_and_stays_bounded(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    r = RateLimiter(2)
    for i in range(1000):
        r.allow(f"10.0.{i // 250}.{i % 250}")
    now[0] += 61  # all of them idle now
    for i in range(30):
        r.allow(f"10.9.0.{i}")
    assert len(r.hits) <= 30
    for i in range(dashboard.RATE_KEYS_MAX + 500):  # a flood of busy addresses: still bounded
        r.allow(f"fd00::{i:x}")
    assert len(r.hits) <= dashboard.RATE_KEYS_MAX
    assert r.allow("x") and r.allow("x") and not r.allow("x")
    now[0] += 61
    assert r.allow("x")


def test_gate_defaults_and_config_validation(config):
    from polymath.core.config import ConfigError

    g = Gate(acc.Access(config.access.state_dir))
    assert not g.remote and g.port == 8443
    config.access.networks = ["0.0.0.0/0"]
    with pytest.raises(ConfigError, match="whole internet"):
        config.validate()
    config.access.networks = ["not-a-network"]
    with pytest.raises(ConfigError, match=r"access\.networks"):
        config.validate()
    config.access.networks = ["10.0.0.0/8"]
    config.access.port = config.dashboard.port
    with pytest.raises(ConfigError, match=r"access\.port"):
        config.validate()


def test_serve_keeps_the_sign_in_free_listener_on_the_pi(config, monkeypatch):
    """A config still saying host = "0.0.0.0" (from before v0.9.0) never exposes the no-sign-in listener."""
    seen: dict[str, Any] = {}

    def fake_make_server(cfg: Any, host: str | None = None, port: int | None = None, *, gate: Any = None) -> Any:
        seen["host"], seen["gate"] = host, gate
        raise SystemExit(0)

    monkeypatch.setattr(dashboard, "make_server", fake_make_server)
    config.dashboard.host = "0.0.0.0"
    with pytest.raises(SystemExit):
        dashboard.serve(config)
    assert seen["host"] == "127.0.0.1" and seen["gate"] is not None and not seen["gate"].remote
