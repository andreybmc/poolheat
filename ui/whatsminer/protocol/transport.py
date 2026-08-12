"""Low-level TCP helpers."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any


def recv_until_close(sock: socket.socket, bufsize: int = 4096, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = sock.recv(bufsize)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)


def tcp_json_oneshot(
    host: str,
    port: int,
    payload: dict[str, Any] | str | bytes,
    *,
    timeout: float = 10.0,
) -> bytes:
    """Connect, send JSON (or raw bytes), read until peer closes. Used by API V2."""
    if isinstance(payload, dict):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = payload

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(data)
        # V2 miners typically close after response
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        return recv_until_close(sock)


def tcp_len_prefixed(
    host: str,
    port: int,
    message: str | bytes,
    *,
    timeout: float = 10.0,
    max_response: int = 8192,
) -> bytes:
    """API V3: [u32 LE length][payload], persistent or one-shot."""
    if isinstance(message, str):
        body = message.encode("utf-8")
    else:
        body = message

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack("<I", len(body)) + body)

        hdr = _recv_exact(sock, 4)
        (length,) = struct.unpack("<I", hdr)
        if length > max_response:
            raise ValueError(f"V3 response length too large: {length}")
        return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"connection closed, need {n} bytes, got {len(buf)}")
        buf.extend(chunk)
    return bytes(buf)


def tcp_session(
    host: str,
    port: int,
    *,
    timeout: float = 30.0,
) -> socket.socket:
    """Open a TCP connection (caller must close). Used for multi-phase streams."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def recv_json_object(sock: socket.socket, *, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    """
    Read until a complete top-level JSON object is available.
    V2 miners may send one JSON object then keep the connection open for binary.
    """
    buf = bytearray()
    depth = 0
    in_str = False
    esc = False
    started = False
    while len(buf) < max_bytes:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf.extend(chunk)
        c = chunk[0]
        if not started:
            if c in (ord(" "), ord("\t"), ord("\r"), ord("\n")):
                continue
            if c != ord("{"):
                # unexpected — accumulate a bit more then fail parse
                more = sock.recv(4096)
                buf.extend(more)
                break
            started = True
            depth = 1
            continue
        ch = chr(c)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                text = bytes(buf).decode("utf-8", errors="replace").strip().rstrip("\0")
                return json.loads(text)
    text = bytes(buf).decode("utf-8", errors="replace").strip().rstrip("\0")
    if not text:
        raise ConnectionError("empty JSON response")
    return json.loads(text)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Public alias for exact-length binary reads (log/firmware streams)."""
    return _recv_exact(sock, n)


def send_u32le_blob(sock: socket.socket, data: bytes) -> None:
    """Send little-endian u32 length + payload (V2 firmware upload)."""
    sock.sendall(struct.pack("<I", len(data)) + data)


def tcp_len_prefixed_session(
    host: str,
    port: int,
    message: str | bytes,
    *,
    timeout: float = 30.0,
    max_response: int = 16 * 1024 * 1024,
) -> tuple[socket.socket, bytes]:
    """
    V3 one request, return (open sock after first JSON response body, body).
    Caller may continue reading binary from the same socket (e.g. log download).
    """
    if isinstance(message, str):
        body = message.encode("utf-8")
    else:
        body = message
    sock = tcp_session(host, port, timeout=timeout)
    try:
        sock.sendall(struct.pack("<I", len(body)) + body)
        hdr = _recv_exact(sock, 4)
        (length,) = struct.unpack("<I", hdr)
        if length > max_response:
            sock.close()
            raise ValueError(f"V3 response length too large: {length}")
        resp = _recv_exact(sock, length)
        return sock, resp
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
