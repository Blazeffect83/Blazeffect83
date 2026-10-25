#!/usr/bin/env bash
# Polymath's HTTPS certificate for the dashboard on your home network (interface/access.py).
#
#   sudo ./scripts/access-cert.sh          make or renew what is missing (install.sh runs it every time)
#   sudo ./scripts/access-cert.sh --force  a new server certificate even if the current one is fine
#   ./scripts/access-cert.sh --status      what there is, and whether it covers this Pi's addresses (no sudo)
#
# It makes, in /etc/polymath/tls:
#   ca.crt / ca.key        Polymath's own small certificate authority (10 years, P-256). The key never leaves the Pi
#                          and only root can read it. Trust ca.crt once in Windows, and Chrome trusts the dashboard.
#   server.crt / .key      the dashboard's certificate (397 days, P-256), signed by that authority, for the Pi's
#                          host name, <name>.local, localhost and every current address. Readable by the polymath
#                          group (the dashboard), not by anyone else.
# The server certificate is renewed when it is missing, expires within 30 days, or no longer names one of this
# Pi's addresses (a new address from the router). The authority stays the same, so a browser that trusts it keeps
# trusting the renewed certificate.
set -euo pipefail

TLS="${POLYMATH_TLS_DIR:-/etc/polymath/tls}"
GROUP="${POLYMATH_GROUP:-polymath}"
HOST="${POLYMATH_HOSTNAME:-$(hostname -s 2>/dev/null || hostname)}"
ADDRS="${POLYMATH_ADDRS:-$(hostname -I 2>/dev/null || true)}"
MODE="make"
case "${1:-}" in
    --force) MODE="force" ;;
    --status) MODE="status" ;;
    "") ;;
    *) echo "usage: $0 [--force|--status]" >&2; exit 2 ;;
esac

if ! command -v openssl >/dev/null 2>&1; then
    echo "access-cert: openssl is not installed: the dashboard stays reachable only on the Pi itself" >&2
    echo "access-cert: install it with: sudo apt install openssl   (then run this again)" >&2
    exit 3
fi

wanted_ips() {
    local a
    echo "127.0.0.1"
    for a in $ADDRS; do
        case "$a" in
            fe80:*|FE80:*) ;;  # link-local IPv6 needs a zone: not usable in a URL bar
            *) echo "$a" ;;
        esac
    done
}

san_line() {
    local line="DNS:${HOST},DNS:${HOST}.local,DNS:localhost" ip
    for ip in $(wanted_ips | sort -u); do line="${line},IP:${ip}"; done
    printf '%s' "$line"
}

needs_renewal() {
    local crt="$TLS/server.crt" ip sans
    [ -f "$crt" ] && [ -f "$TLS/server.key" ] || { echo "no server certificate yet"; return 0; }
    openssl x509 -checkend $((30 * 86400)) -noout -in "$crt" >/dev/null 2>&1 || { echo "it expires within 30 days"; return 0; }
    sans="$(openssl x509 -noout -ext subjectAltName -in "$crt" 2>/dev/null | tail -n +2 | tr -d ' \n')"
    for ip in $(wanted_ips); do
        case ",$sans," in
            *",IPAddress:${ip},"*) ;;
            *) echo "it does not name ${ip}"; return 0 ;;
        esac
    done
    case ",$sans," in
        *",DNS:${HOST},"*) ;;
        *) echo "it does not name ${HOST}"; return 0 ;;
    esac
    return 1
}

if [ "$MODE" = status ]; then
    if [ ! -f "$TLS/ca.crt" ]; then echo "no certificate authority yet: sudo $0"; exit 1; fi
    echo "authority: $(openssl x509 -noout -subject -in "$TLS/ca.crt" | sed 's/^subject=//')"
    echo "SHA-1 thumbprint: $(openssl x509 -noout -fingerprint -sha1 -in "$TLS/ca.crt" | cut -d= -f2)"
    if why="$(needs_renewal)"; then echo "server certificate: needs renewal ($why): sudo $0"; exit 1; fi
    echo "server certificate: fine until $(openssl x509 -noout -enddate -in "$TLS/server.crt" | cut -d= -f2)"
    echo "names: $(openssl x509 -noout -ext subjectAltName -in "$TLS/server.crt" | tail -n +2 | tr -d ' ')"
    exit 0
fi

if [ "$(id -u)" != 0 ] && [ -z "${POLYMATH_TLS_DIR:-}" ]; then
    echo "access-cert: run it with sudo" >&2
    exit 1
fi
umask 077
mkdir -p "$TLS"
chmod 0750 "$TLS"
getent group "$GROUP" >/dev/null 2>&1 && chgrp "$GROUP" "$TLS" || true

if [ ! -f "$TLS/ca.key" ] || [ ! -f "$TLS/ca.crt" ]; then
    echo "access-cert: making Polymath's certificate authority for ${HOST}"
    openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -sha256 -days 3650 \
        -subj "/CN=Polymath on ${HOST} (local authority)/O=Polymath" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -keyout "$TLS/ca.key" -out "$TLS/ca.crt" 2>/dev/null
    chmod 0600 "$TLS/ca.key"
    chmod 0644 "$TLS/ca.crt"
    MODE="force"  # a new authority: the server certificate must be signed by it
fi

why=""
if [ "$MODE" = force ]; then
    why="asked for"
elif ! why="$(needs_renewal)"; then
    echo "access-cert: the certificate is fine (names $(san_line | tr ',' ' '))"
    exit 0
fi

echo "access-cert: making the dashboard's certificate ($why)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cat >"$work/ext.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$(san_line)
EOF
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -sha256 \
    -subj "/CN=${HOST}/O=Polymath" -keyout "$work/server.key" -out "$work/server.csr" 2>/dev/null
openssl x509 -req -in "$work/server.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" -CAcreateserial \
    -CAserial "$TLS/ca.srl" -days 397 -sha256 -extfile "$work/ext.cnf" -out "$work/server.crt" 2>/dev/null
install -m 0640 "$work/server.key" "$TLS/server.key"
install -m 0644 "$work/server.crt" "$TLS/server.crt"
getent group "$GROUP" >/dev/null 2>&1 && chgrp "$GROUP" "$TLS/server.key" || true
echo "access-cert: done (names $(san_line | tr ',' ' '))"
echo "access-cert: SHA-1 thumbprint of the authority: $(openssl x509 -noout -fingerprint -sha1 -in "$TLS/ca.crt" | cut -d= -f2)"
