"""Minecraft Server List Ping (the protocol every Java-edition client uses for the server list).

Implemented directly over a TCP socket: handshake (next state = status), status
request, read the JSON status response. Only ``players.online`` / ``players.max``
are used — the agent yields the CPU to the PaperMC server whenever someone plays.
No query plugin, RCON or server-side change is needed.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass

PROTOCOL_ANY = -1 & 0xFFFFFFFF  # "-1": status-only ping, valid for every server version
MAX_RESPONSE = 1 << 20


@dataclass
class ServerStatus:
    online: int
    max: int
    version: str


def _varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed the connection")
        buf += chunk
    return bytes(buf)


def _read_varint(sock: socket.socket) -> int:
    result = 0
    for shift in range(0, 35, 7):
        byte = _read_exact(sock, 1)[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result
    raise ValueError("VarInt too long")


def _packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = _varint(packet_id) + payload
    return _varint(len(body)) + body


def handshake(host: str, port: int) -> bytes:
    addr = host.encode("utf-8")
    payload = _varint(PROTOCOL_ANY) + _varint(len(addr)) + addr + struct.pack(">H", port) + _varint(1)
    return _packet(0x00, payload) + _packet(0x00)  # handshake, then status request


def ping(host: str, port: int, *, timeout: float = 2.0) -> ServerStatus | None:
    """Status of the server, or None when it is down / not a Minecraft server."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(handshake(host, port))
            length = _read_varint(sock)
            if not 0 < length <= MAX_RESPONSE:
                return None
            data = _read_exact(sock, length)
    except (OSError, ValueError, ConnectionError):
        return None
    try:
        pos = 0
        packet_id, pos = _decode_varint(data, pos)
        if packet_id != 0x00:
            return None
        n, pos = _decode_varint(data, pos)
        status = json.loads(data[pos : pos + n].decode("utf-8"))
        players = status.get("players") or {}
        version = status.get("version") or {}
        return ServerStatus(int(players.get("online", 0)), int(players.get("max", 0)), str(version.get("name", "")))
    except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
        return None


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 35, 7):
        if pos >= len(data):
            raise ValueError("truncated VarInt")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
    raise ValueError("VarInt too long")
