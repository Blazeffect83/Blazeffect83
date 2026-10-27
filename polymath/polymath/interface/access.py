"""Private access to the dashboard from your own browser on your home network.

The Pi's own browser, the live feed and ``polymath doctor`` use ``http://127.0.0.1:8765``: loopback only, nothing
to sign in to (whoever sits at the Pi already has it). Every other device goes through the *remote* listener,
``https://<pi>:8443``, which lets a request in only if all of these hold:

1. **Home network only.** The client's address is in ``access.networks`` (private and link-local ranges by default).
   systemd enforces the same at the kernel (``IPAddressAllow``/``IPAddressDeny`` in the dashboard's unit).
2. **HTTPS.** TLS 1.2 or later, with a certificate from Polymath's own small certificate authority on the Pi
   (``scripts/access-cert.sh``, run by ``install.sh``). Trusting that authority once in Windows removes Chrome's
   warning and lets Chrome install the dashboard as an app.
3. **A paired device.** ``polymath access pair`` (printed by ``install.sh`` at the end) shows a code like
   ``K7QX-M3PA``. It lasts 15 minutes and works once, and five wrong guesses cancel it. Typed into the pairing page,
   it gives that browser a device key: 256 random bits in an ``HttpOnly``, ``Secure``, ``SameSite=Strict``
   cookie. The Pi keeps only its SHA-256, so a stolen file of devices opens nothing. The browser stays signed in for
   400 days (Chrome's limit) until you sign it out or revoke it (``polymath access revoke``).
4. **The right host name and origin.** The ``Host`` header must be an IP address or one of the Pi's names, which
   defeats DNS rebinding. Every POST must come from the page's own origin (and the cookie is ``SameSite=Strict``),
   which defeats cross-site requests.

What happens is written to ``access.log`` (pairing, refusals, revocations) in the state directory
(``/var/lib/polymath/access``, mode 0700, owned by the dashboard's user). Device keys and codes are never logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymath.core.logging import get_logger

log = get_logger("access")

COOKIE = "pm_device"
COOKIE_MAX_AGE = 400 * 86400  # Chrome caps cookie lifetimes at 400 days
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: easy to read off a screen and type
CODE_LEN = 8  # 32^8 ≈ 10^12 codes
CODE_TTL = 15 * 60.0
CODE_TRIES = 5  # wrong guesses before the code is cancelled
IP_TRIES = 10  # pairing attempts per address per IP_WINDOW (any code)
IP_WINDOW = 600.0
SEEN_EVERY = 600.0  # a device's "last seen" is written at most this often
MAX_DEVICES = 20
LOG_KEEP = 1000  # lines kept in access.log
DEVICES = "devices.json"
CODE = "code.json"
LOG = "access.log"


class PairError(Exception):
    """Pairing refused; ``str(exc)`` says why, in words for the pairing page."""


@dataclass
class Device:
    id: str
    name: str
    created: float
    last_seen: float
    last_ip: str

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "created": self.created, "last_seen": self.last_seen,
                "last_ip": self.last_ip}  # fmt: skip


# ------------------------------------------------------------------------------------------------ checks
def networks(spec: list[str] | tuple[str, ...]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(n, strict=False) for n in spec]


def client_allowed(ip: str, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    """Is this client address on the home network?"""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in n for n in nets)


def host_names(extra: list[str] | tuple[str, ...] = ()) -> set[str]:
    """Names the dashboard answers to: localhost, the Pi's host name (with and without ``.local``), any extras."""
    names = {"localhost"}
    with contextlib.suppress(OSError):
        h = socket.gethostname().strip().lower()
        if h:
            names |= {h, f"{h}.local", h.split(".", 1)[0]}
    return names | {n.strip().lower() for n in extra if n.strip()}


def host_allowed(header: str | None, names: set[str]) -> bool:
    """The ``Host`` header names this Pi: an IP address or one of its names (a rebinding attack uses another)."""
    if not header:
        return False
    h = header.strip().lower()
    if h.startswith("["):  # [IPv6]:port
        h = h[1 : h.find("]")] if "]" in h else ""
    elif h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h.rstrip(".") in names


def same_origin(origin: str | None, host: str | None, scheme: str = "https") -> bool:
    """A POST must come from the dashboard's own pages: ``Origin`` is ``<scheme>://<the Host it was sent to>``."""
    if not origin or not host:
        return False
    return origin.strip().lower() == f"{scheme}://{host.strip().lower()}"


def cookie_token(header: str | None) -> str | None:
    """The device key from a ``Cookie`` header, if any."""
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and value:
            return value
    return None


def set_cookie(token: str) -> str:
    return f"{COOKIE}={token}; Max-Age={COOKIE_MAX_AGE}; Path=/; Secure; HttpOnly; SameSite=Strict"


def clear_cookie() -> str:
    return f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"


def device_name(user_agent: str | None, given: str = "") -> str:
    """A friendly name: what the person typed, else the browser and system from the ``User-Agent``."""
    name = re.sub(r"[^\w .,'()+-]", "", given or "").strip()[:40]
    if name:
        return name
    ua = user_agent or ""
    browser = next((b for k, b in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                   ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "a browser")  # fmt: skip
    system = next((s for k, s in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"),
                                  ("iPad", "iPad"), ("Mac OS", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))
                   if k in ua), "")  # fmt: skip
    return f"{browser} on {system}" if system else browser


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


# ------------------------------------------------------------------------------------------------ the registry
class Access:
    """Paired devices and the current pairing code, kept in ``state_dir`` (files 0600). Thread-safe."""

    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(state_dir)
        self.clock = clock
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}  # hash → record
        self._mtime = -1.0
        self._dirty_at = 0.0
        self._tries: dict[str, deque[float]] = defaultdict(deque)

    # -------------------------------------------------------------- files
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _write(self, name: str, data: Any) -> None:
        self._write_text(name, json.dumps(data))

    def _write_text(self, name: str, text: str) -> None:
        """Atomically, readable only by the dashboard's user (0600)."""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path(f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(self._path(name))

    def _read(self, name: str) -> Any:
        try:
            return json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _load(self) -> None:
        """Re-read devices.json when another process (the CLI's revoke) changed it."""
        try:
            mtime = self._path(DEVICES).stat().st_mtime
        except OSError:
            self._devices, self._mtime = {}, -1.0
            return
        if mtime == self._mtime:
            return
        raw = self._read(DEVICES)
        items = raw.get("devices", []) if isinstance(raw, dict) else []
        self._devices = {str(d["hash"]): d for d in items if isinstance(d, dict) and d.get("hash")}
        self._mtime = mtime

    def _save(self) -> None:
        self._write(DEVICES, {"devices": list(self._devices.values())})
        with contextlib.suppress(OSError):
            self._mtime = self._path(DEVICES).stat().st_mtime
        self._dirty_at = 0.0

    def log(self, event: str, **detail: Any) -> None:
        """One line in access.log (kept to the newest ``LOG_KEEP``)."""
        line = json.dumps({"at": round(self.clock(), 1), "event": event, **detail}, sort_keys=True)
        path = self._path(LOG)
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if path.stat().st_size > LOG_KEEP * 200:
                lines = path.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]
                self._write_text(LOG, "\n".join(lines) + "\n")
        except OSError:
            log.warning("could not write the access log", extra={"event": event})

    def history(self, n: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self._path(LOG).read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    # -------------------------------------------------------------- pairing
    def new_code(self) -> str:
        """A fresh pairing code (the previous one stops working). Returned once, kept only as a hash."""
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        with self._lock:
            self._write(CODE, {"hash": _digest(code), "expires": self.clock() + CODE_TTL, "tries": 0})
        self.log("code")
        return format_code(code)

    def code_active(self) -> bool:
        raw = self._read(CODE)
        return isinstance(raw, dict) and float(raw.get("expires") or 0) > self.clock() and bool(raw.get("hash"))

    def pair(self, code: str, *, name: str, ip: str) -> tuple[str, Device]:
        """Trade a valid pairing code for a device key. Raises :class:`PairError` with the reason."""
        now = self.clock()
        with self._lock:
            q = self._tries[ip]
            while q and now - q[0] > IP_WINDOW:
                q.popleft()
            if len(q) >= IP_TRIES:
                self.log("refused", ip=ip, why="too many attempts")
                raise PairError("too many attempts from this computer: wait 10 minutes")
            q.append(now)
            if len(self._tries) > 256:  # one entry per address that ever tried: forget the quiet ones
                for k in [k for k, v in self._tries.items() if not v or now - v[-1] > IP_WINDOW]:
                    del self._tries[k]
            raw = self._read(CODE)
            if not isinstance(raw, dict) or not raw.get("hash"):
                raise PairError("no pairing code is waiting: run 'polymath access pair' on the Pi")
            if float(raw.get("expires") or 0) <= now:
                raise PairError("that code has expired: run 'polymath access pair' again")
            if not hmac.compare_digest(_digest(normalize_code(code)), str(raw["hash"])):
                raw["tries"] = int(raw.get("tries") or 0) + 1
                if raw["tries"] >= CODE_TRIES:
                    self._write(CODE, {})
                    self.log("refused", ip=ip, why="code cancelled after wrong guesses")
                    raise PairError("too many wrong codes: that code is cancelled, make a new one on the Pi")
                self._write(CODE, raw)
                self.log("refused", ip=ip, why="wrong code")
                raise PairError(f"wrong code ({CODE_TRIES - raw['tries']} tries left)")
            self._write(CODE, {})  # one use
            self._load()
            if len(self._devices) >= MAX_DEVICES:
                raise PairError(f"{MAX_DEVICES} devices are paired already: revoke one first")
            token = secrets.token_urlsafe(32)
            h = _digest(token)
            dev: dict[str, Any] = {"id": secrets.token_hex(4), "name": name, "hash": h, "created": now,
                                   "last_seen": now, "last_ip": ip}  # fmt: skip
            self._devices[h] = dev
            self._save()
        self.log("paired", id=dev["id"], name=name, ip=ip)
        return token, self._device(dev)

    @staticmethod
    def _device(d: dict[str, Any]) -> Device:
        return Device(str(d["id"]), str(d.get("name") or "device"), float(d.get("created") or 0),
                      float(d.get("last_seen") or 0), str(d.get("last_ip") or ""))  # fmt: skip

    # -------------------------------------------------------------- every request
    def check(self, token: str | None, *, ip: str) -> Device | None:
        """The paired device a device key belongs to, or ``None``."""
        if not token or len(token) > 200:
            return None
        h = _digest(token)
        now = self.clock()
        with self._lock:
            self._load()
            d = self._devices.get(h)
            if d is None:
                return None
            d["last_seen"], d["last_ip"] = now, ip
            if not self._dirty_at:
                self._dirty_at = now
            if now - self._dirty_at >= SEEN_EVERY:
                with contextlib.suppress(OSError):
                    self._save()
            return self._device(d)

    def devices(self) -> list[Device]:
        with self._lock:
            self._load()
            return sorted((self._device(d) for d in self._devices.values()), key=lambda d: d.created)

    def revoke(self, which: str, *, by: str = "") -> int:
        """Revoke one device (its id) or ``all``. Returns how many."""
        with self._lock:
            self._load()
            gone = [h for h, d in self._devices.items() if which == "all" or d.get("id") == which]
            for h in gone:
                self._devices.pop(h)
            if gone:
                self._save()
        if gone:
            self.log("revoked", which=which, n=len(gone), by=by)
        return len(gone)

    def flush(self) -> None:
        """Write pending "last seen" times (on shutdown)."""
        with self._lock:
            if self._dirty_at:
                with contextlib.suppress(OSError):
                    self._save()


# ------------------------------------------------------------------------------------------------ for people
def fingerprints(ca_path: Path) -> dict[str, str] | None:
    """The Pi's CA certificate fingerprints, to compare with what Windows shows before trusting it."""
    try:
        pem = Path(ca_path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", pem, re.S)
    if m is None:
        return None
    import base64
    import binascii

    try:
        der = base64.b64decode("".join(m.group(1).split()), validate=True)
    except (binascii.Error, ValueError):
        return None

    def colons(h: str) -> str:
        return ":".join(h[i : i + 2] for i in range(0, len(h), 2)).upper()

    return {"sha256": colons(hashlib.sha256(der).hexdigest()), "sha1": colons(hashlib.sha1(der).hexdigest())}


def lan_addresses() -> list[str]:
    """The Pi's addresses on the home network (the route a packet would take; nothing is sent)."""
    out: list[str] = []
    for probe in ("192.168.0.1", "10.0.0.1", "172.16.0.1"):
        with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((probe, 9))
            ip = str(s.getsockname()[0])
            if ip and not ip.startswith("127.") and ip not in out:
                out.append(ip)
    return out


def lan_urls(port: int) -> list[str]:
    """Where your other devices open the dashboard: by address, and by the Pi's name (mDNS)."""
    urls = [f"https://{ip}:{port}/" for ip in lan_addresses()]
    with contextlib.suppress(OSError):
        h = socket.gethostname().split(".", 1)[0].strip()
        if h:
            urls.append(f"https://{h}.local:{port}/")
    return urls


def cert_info(crt: Path) -> dict[str, Any] | None:
    """Names, addresses and expiry of the dashboard's certificate (for ``polymath doctor``), or None when there is
    none. Raises ``PermissionError`` when it may not be read (as you: ``/etc/polymath/tls`` is root's and the
    dashboard's), so the caller can ask the dashboard instead."""
    import ssl

    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)  # CPython's own decoder (no extra package)
    try:
        if decode is None or not Path(crt).is_file():
            return None
        raw = decode(str(crt))
        not_after = ssl.cert_time_to_seconds(str(raw["notAfter"]))
    except PermissionError:
        raise
    except (ssl.SSLError, OSError, KeyError, ValueError):
        return None
    sans = raw.get("subjectAltName") or ()
    return {"names": [v for k, v in sans if k == "DNS"], "ips": [v for k, v in sans if k == "IP Address"],
            "not_after": float(not_after)}  # fmt: skip


def cert_from_dashboard(config: Any) -> dict[str, Any] | None:
    """The certificate's facts as the dashboard reads them (loopback, no sign-in): ``{}`` when it has none, None when
    the dashboard does not answer."""
    import urllib.request

    url = f"http://127.0.0.1:{int(config.dashboard.port)}/api/access/me"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url), timeout=5) as resp:
            body = json.loads(resp.read() or b"{}")
    except (OSError, ValueError):
        return None
    cert = body.get("cert") if isinstance(body, dict) else None
    return cert if isinstance(cert, dict) else {}


def access_check(config: Any, *, now: float | None = None) -> tuple[str, str, str]:
    """``(status, detail, fix)`` for doctor: is remote access ready (certificate, listener, paired devices)?"""
    now = time.time() if now is None else now
    a = config.access
    fix_cert = "sudo /opt/polymath/src/scripts/access-cert.sh   (install.sh does it too)"
    if not a.remote:
        return "ok", "only on the Pi itself ([access] remote = false)", ""
    try:
        info = cert_info(Path(a.tls_dir) / "server.crt")
    except PermissionError:  # doctor runs as you: the dashboard (which may read it) says what it is
        info = cert_from_dashboard(config)
        if info is None:
            return ("warn", "the HTTPS certificate could not be checked (it is readable by root and the dashboard "
                    "only, and the dashboard is not answering)", "sudo polymath doctor")  # fmt: skip
        if not info:
            info = None
    if info is None:
        return "warn", "no HTTPS certificate yet: your PC cannot open the dashboard", fix_cert
    days = (info["not_after"] - now) / 86400
    if days < 0:
        return "bad", "the HTTPS certificate has expired: browsers refuse it", fix_cert
    missing = [ip for ip in lan_addresses() if ip not in info["ips"]]
    if missing:
        return (
            "warn",
            f"the certificate does not name this Pi's address {', '.join(missing)} (a new address?)",
            fix_cert,
        )
    try:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # only "does it answer TLS": the certificate was checked above
        with socket.create_connection(("127.0.0.1", int(a.port)), timeout=3) as raw, ctx.wrap_socket(raw):
            pass
    except OSError:
        return "warn", f"nothing answers HTTPS on port {a.port}", "sudo systemctl restart polymath-dashboard"
    soon = f"; the certificate is renewed by install.sh within {days:.0f} days" if days < 30 else ""
    return "ok", f"https://<this Pi>:{a.port}/ for paired devices on your home network{soon}", ""
