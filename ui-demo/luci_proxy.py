#!/usr/bin/env python3
"""
LuCI reverse proxy for poolheat.

Listens on a separate port (default :8788) and forwards HTTP(S) to the
Whatsminer web UI so operators can open LuCI via the router:

  http://<router-lan-ip>:8788/  →  https://<miner-host>/

This is an HTTP reverse proxy (not L3 transparent NAT). Browser talks
plain HTTP to poolheat; poolheat connects to the miner (HTTPS by default,
TLS verify off for self-signed certs).

Control lifecycle from serve.py:
  configure(...) / apply() / start() / stop() / status() / get_config()
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

# ── defaults ────────────────────────────────────────────────────────────────

DEFAULT_LISTEN_PORT = 8788
DEFAULT_BIND = "0.0.0.0"
DEFAULT_SCHEME = "https"
DEFAULT_TARGET_PORT_HTTPS = 443
DEFAULT_TARGET_PORT_HTTP = 80

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
        "content-length",  # recompute from body / use chunked carefully
        "host",
    }
)

# ── runtime state ───────────────────────────────────────────────────────────

_lock = threading.RLock()
_cfg: dict[str, Any] = {
    "enabled": False,
    "bind": DEFAULT_BIND,
    "listen_port": DEFAULT_LISTEN_PORT,
    "target_scheme": DEFAULT_SCHEME,
    "target_port": DEFAULT_TARGET_PORT_HTTPS,
    "verify_tls": False,
    "target_host": "",  # filled by serve from miner settings
}
_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_last_error: str | None = None
_started_at: float | None = None
# optional callback: () -> str host (live miner IP)
_host_resolver: Callable[[], str] | None = None


def set_host_resolver(fn: Callable[[], str] | None) -> None:
    """serve.py sets this so proxy always uses current miner host."""
    global _host_resolver
    with _lock:
        _host_resolver = fn


def get_config() -> dict[str, Any]:
    with _lock:
        return dict(_cfg)


def configure(
    *,
    enabled: bool | None = None,
    bind: str | None = None,
    listen_port: int | None = None,
    target_scheme: str | None = None,
    target_port: int | None = None,
    verify_tls: bool | None = None,
    target_host: str | None = None,
) -> dict[str, Any]:
    """Update config fields (does not start/stop — call apply())."""
    with _lock:
        if enabled is not None:
            _cfg["enabled"] = bool(enabled)
        if bind is not None and str(bind).strip():
            _cfg["bind"] = str(bind).strip()
        if listen_port is not None:
            try:
                p = int(listen_port)
            except (TypeError, ValueError):
                p = DEFAULT_LISTEN_PORT
            _cfg["listen_port"] = max(1, min(65535, p))
        if target_scheme is not None:
            sch = str(target_scheme).strip().lower()
            _cfg["target_scheme"] = "http" if sch == "http" else "https"
            # auto port if still default for other scheme
            if target_port is None:
                if _cfg["target_scheme"] == "http" and _cfg.get("target_port") in (
                    443,
                    DEFAULT_TARGET_PORT_HTTPS,
                ):
                    _cfg["target_port"] = DEFAULT_TARGET_PORT_HTTP
                elif _cfg["target_scheme"] == "https" and _cfg.get("target_port") in (
                    80,
                    DEFAULT_TARGET_PORT_HTTP,
                ):
                    _cfg["target_port"] = DEFAULT_TARGET_PORT_HTTPS
        if target_port is not None:
            try:
                tp = int(target_port)
            except (TypeError, ValueError):
                tp = (
                    DEFAULT_TARGET_PORT_HTTP
                    if _cfg["target_scheme"] == "http"
                    else DEFAULT_TARGET_PORT_HTTPS
                )
            _cfg["target_port"] = max(1, min(65535, tp))
        if verify_tls is not None:
            _cfg["verify_tls"] = bool(verify_tls)
        if target_host is not None and str(target_host).strip():
            _cfg["target_host"] = str(target_host).strip()
        return dict(_cfg)


def _resolve_target_host() -> str:
    with _lock:
        fn = _host_resolver
        fallback = str(_cfg.get("target_host") or "").strip()
    if fn:
        try:
            h = str(fn() or "").strip()
            if h:
                return h
        except Exception:
            pass
    return fallback


def _upstream_base() -> tuple[str, str, int]:
    """Return (scheme, host, port)."""
    with _lock:
        scheme = str(_cfg.get("target_scheme") or "https").lower()
        if scheme != "http":
            scheme = "https"
        port = int(_cfg.get("target_port") or (80 if scheme == "http" else 443))
    host = _resolve_target_host()
    return scheme, host, port


def _ssl_context(verify: bool) -> ssl.SSLContext | None:
    if verify:
        return ssl.create_default_context()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def status() -> dict[str, Any]:
    with _lock:
        running = bool(
            _server is not None
            and _thread is not None
            and _thread.is_alive()
        )
        cfg = dict(_cfg)
        err = _last_error
        started = _started_at
    scheme, host, port = _upstream_base()
    return {
        "running": running,
        "enabled": bool(cfg.get("enabled")),
        "listen": f"{cfg.get('bind')}:{cfg.get('listen_port')}",
        "bind": cfg.get("bind"),
        "listen_port": cfg.get("listen_port"),
        "target": f"{scheme}://{host}:{port}/" if host else None,
        "target_host": host or None,
        "target_scheme": scheme,
        "target_port": port,
        "verify_tls": bool(cfg.get("verify_tls")),
        "error": err,
        "started_at": (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started))
            if started
            else None
        ),
    }


class _LuciProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, fmt: str, *args) -> None:
        # quiet by default; uncomment for debug
        # print(f"[luci-proxy] {self.address_string()} {fmt % args}")
        return

    def log_error(self, fmt: str, *args) -> None:
        print(f"[luci-proxy] ERROR {fmt % args}")

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def _read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return b""
        # cap extreme bodies (100 MiB) — firmware flashes can be large
        if n > 100 * 1024 * 1024:
            raise ValueError(f"body too large: {n}")
        return self.rfile.read(n)

    def _upstream_host_header(self, host: str, port: int) -> str:
        if port in (80, 443):
            return host
        return f"{host}:{port}"

    def _rewrite_client_url_to_upstream(
        self, val: str, scheme: str, host: str, port: int
    ) -> str:
        """
        Rewrite Referer/Origin from http://router:8788/... → https://miner/...
        so LuCI CSRF / form checks see the real upstream origin.
        """
        if not val:
            return val
        try:
            parts = urlsplit(val)
        except Exception:
            return val
        # only rewrite if this looks like it hit our proxy (any host, our listen path)
        # or relative Origin without host — leave alone
        if not parts.scheme and not parts.netloc:
            return val
        path = parts.path or "/"
        q = parts.query
        frag = parts.fragment
        up_netloc = self._upstream_host_header(host, port)
        return urlunsplit((scheme, up_netloc, path, q, frag))

    def _build_upstream_headers(self, host: str, port: int, scheme: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, val in self.headers.items():
            lk = key.lower()
            if lk in _HOP_BY_HOP:
                continue
            if lk in ("referer", "origin"):
                out[key] = self._rewrite_client_url_to_upstream(val, scheme, host, port)
                continue
            out[key] = val
        # Host as miner expects
        out["Host"] = self._upstream_host_header(host, port)
        # Prefer identity encoding for simpler proxying / body rewrite
        out["Accept-Encoding"] = "identity"
        # Tell upstream the external client used HTTP to the proxy.
        # Do NOT claim https — that makes some firmwares emit Secure-only cookies
        # that the browser then refuses to store on http://router:8788.
        client = self.client_address[0] if self.client_address else ""
        if client:
            prior = self.headers.get("X-Forwarded-For")
            out["X-Forwarded-For"] = f"{prior}, {client}" if prior else client
        out["X-Forwarded-Proto"] = "http"
        out["X-Forwarded-Host"] = self.headers.get("Host") or f"localhost:{DEFAULT_LISTEN_PORT}"
        out["X-Real-IP"] = client or ""
        return out

    def _rewrite_location(self, loc: str, scheme: str, host: str, port: int) -> str:
        """Rewrite absolute Location (any scheme) pointing at miner → path on proxy."""
        if not loc:
            return loc
        try:
            parts = urlsplit(loc)
        except Exception:
            return loc
        # relative already
        if not parts.scheme and not parts.netloc:
            return loc
        loc_host = (parts.hostname or "").lower()
        miner = host.lower()
        up = self._upstream_host_header(host, port).lower()
        net = (parts.netloc or "").lower()
        if loc_host and loc_host != miner and net != up and net.split(":")[0] != miner:
            return loc
        # strip scheme/host → keep path?query#frag (browser stays on :8788)
        return urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))

    def _rewrite_set_cookie(self, cookie: str) -> str:
        """
        Make session cookies work on plain HTTP proxy:
        - drop Domain= (bind to router host :8788)
        - drop Secure (browser would ignore cookie on http://)
        - soften SameSite=None (requires Secure) → Lax
        """
        if not cookie:
            return cookie
        parts = []
        for seg in cookie.split(";"):
            s = seg.strip()
            if not s:
                continue
            low = s.lower()
            if low.startswith("domain="):
                continue
            if low == "secure":
                continue
            if low.startswith("samesite="):
                # SameSite=None without Secure is rejected by modern browsers
                if "none" in low:
                    parts.append("SameSite=Lax")
                else:
                    parts.append(s)
                continue
            parts.append(s)
        return "; ".join(parts)

    def _rewrite_html_body(self, payload: bytes, scheme: str, host: str, port: int) -> bytes:
        """
        Soft-rewrite absolute miner URLs in HTML so forms/links stay on the proxy.
        Keeps login POSTs from jumping to https://miner/ directly.
        """
        if not payload or len(payload) > 4 * 1024 * 1024:
            return payload
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = payload.decode("latin-1")
            except Exception:
                return payload
        # only touch likely HTML
        head = text[:200].lower()
        if "<html" not in head and "<!doctype" not in head and "<form" not in text[:2000].lower():
            return payload

        up_host = re.escape(host)
        up_netloc = re.escape(self._upstream_host_header(host, port))
        # https://miner/  https://miner:443/  http://miner/
        patterns = [
            rf"https?://{up_netloc}",
            rf"https?://{up_host}(?::(?:80|443))?",
            rf"https?://{up_host}",
        ]
        out = text
        for pat in patterns:
            out = re.sub(pat, "", out, flags=re.IGNORECASE)
        if out is text:
            return payload
        try:
            return out.encode("utf-8")
        except Exception:
            return payload

    def _forward_response_headers(
        self,
        headers_list: list[tuple[str, str]],
        scheme: str,
        host: str,
        port: int,
    ) -> None:
        """Copy upstream headers; multi-value Set-Cookie preserved."""
        cookies: list[str] = []
        for key, val in headers_list:
            lk = key.lower()
            if lk in _HOP_BY_HOP or lk == "content-length":
                continue
            if lk == "set-cookie":
                cookies.append(val)
                continue
            if lk == "location":
                val = self._rewrite_location(val, scheme, host, port)
            # drop HSTS — would force browser onto https://router:8788 (broken)
            if lk == "strict-transport-security":
                continue
            # avoid forcing upgrade
            if lk == "content-security-policy" and "upgrade-insecure-requests" in (val or "").lower():
                continue
            self.send_header(key, val)
        for c in cookies:
            self.send_header("Set-Cookie", self._rewrite_set_cookie(c))

    def _proxy(self) -> None:
        """
        Reverse-proxy one request to miner LuCI.

        Critical: do NOT auto-follow redirects. Login returns 302 + Set-Cookie;
        if the proxy follows it, the browser never receives the session cookie.
        """
        global _last_error
        scheme, host, port = _upstream_base()
        if not host:
            self.send_error(502, "LuCI proxy: miner host not configured")
            return
        try:
            body = self._read_body()
        except Exception as e:
            self.send_error(400, f"bad body: {e}")
            return

        path = self.path  # includes query
        if not path.startswith("/"):
            path = "/" + path
        method = self.command.upper()
        headers = self._build_upstream_headers(host, port, scheme)

        with _lock:
            verify = bool(_cfg.get("verify_tls"))

        conn: http.client.HTTPConnection | None = None
        try:
            timeout = 90.0
            if scheme == "https":
                ctx = _ssl_context(verify)
                conn = http.client.HTTPSConnection(
                    host, port, timeout=timeout, context=ctx
                )
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)

            # http.client wants a plain dict / sequence of headers
            conn.request(
                method,
                path,
                body=body if body else None,
                headers=headers,
            )
            resp = conn.getresponse()
            status = int(resp.status)
            reason = str(resp.reason or "OK")
            # Preserve multi Set-Cookie via getheaders()
            raw_headers = list(resp.getheaders() or [])
            payload = resp.read() if method != "HEAD" else b""

            # rewrite HTML absolute links (login form action=https://miner/...)
            ctype = ""
            for k, v in raw_headers:
                if k.lower() == "content-type":
                    ctype = (v or "").lower()
                    break
            if payload and ("text/html" in ctype or "application/xhtml" in ctype):
                payload = self._rewrite_html_body(payload, scheme, host, port)

            self.send_response(status, reason)
            self._forward_response_headers(raw_headers, scheme, host, port)
            if method != "HEAD":
                self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD" and payload:
                self.wfile.write(payload)
            with _lock:
                _last_error = None
        except Exception as e:
            msg = str(e) or type(e).__name__
            with _lock:
                _last_error = msg
            try:
                body_err = (
                    f"<html><body><h1>LuCI proxy error</h1>"
                    f"<p>{_html_escape(msg)}</p>"
                    f"<p>upstream: {scheme}://{host}:{port}{path}</p>"
                    f"<p>proxy is plain HTTP on :8788; cookies Secure/HSTS stripped.</p>"
                    f"</body></html>"
                ).encode("utf-8")
                self.send_response(502, "Bad Gateway")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_err)))
                self.send_header("Connection", "close")
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(body_err)
            except Exception:
                pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self) -> None:
        # SO_REUSEADDR already via allow_reuse_address
        super().server_bind()


def _cleanup_server_locked() -> None:
    """Close leftover server socket (caller holds _lock). Thread must be dead/stopped."""
    global _server, _thread, _started_at
    srv = _server
    _server = None
    _thread = None
    _started_at = None
    if srv is None:
        return
    try:
        srv.server_close()
    except Exception:
        pass


def start() -> dict[str, Any]:
    """Start proxy listener if not already running."""
    global _server, _thread, _last_error, _started_at
    with _lock:
        if _server is not None and _thread is not None and _thread.is_alive():
            return {**status(), "ok": True, "note": "already running"}
        # Stale server (thread died / previous failed stop) still holds the port
        if _server is not None:
            print("[luci-proxy] cleaning stale server before start")
            _cleanup_server_locked()

        bind = str(_cfg.get("bind") or DEFAULT_BIND).strip() or DEFAULT_BIND
        port = int(_cfg.get("listen_port") or DEFAULT_LISTEN_PORT)
        # Prefer IPv4 dual-stack style bind; fall back if host unusable
        bind_candidates = [bind]
        if bind in ("0.0.0.0", "::"):
            bind_candidates = [bind, ""]
        elif bind not in ("", "0.0.0.0"):
            bind_candidates = [bind, "0.0.0.0"]

        srv = None
        last_err: Exception | None = None
        used_bind = bind
        for b in bind_candidates:
            try:
                srv = _ReusableThreadingHTTPServer((b, port), _LuciProxyHandler)
                used_bind = b if b != "" else "0.0.0.0"
                break
            except OSError as e:
                last_err = e
                srv = None
                continue
        if srv is None:
            _last_error = f"bind {bind}:{port}: {last_err}"
            print(f"[luci-proxy] start failed: {_last_error}")
            return {**status(), "ok": False, "error": _last_error}

        def _run(server: ThreadingHTTPServer = srv) -> None:
            global _last_error
            try:
                server.serve_forever(poll_interval=0.5)
            except Exception as e:
                print(f"[luci-proxy] serve_forever: {e}")
                with _lock:
                    _last_error = f"serve: {e}"

        th = threading.Thread(target=_run, name="luci-proxy", daemon=True)
        _server = srv
        _thread = th
        _last_error = None
        _started_at = time.time()
        # keep configured bind in sync with what actually worked
        _cfg["bind"] = used_bind if used_bind else bind
        th.start()
        # brief yield so is_alive() is reliable
        time.sleep(0.05)
        if not th.is_alive():
            _last_error = "thread exited immediately after start"
            print(f"[luci-proxy] start failed: {_last_error}")
            _cleanup_server_locked()
            return {**status(), "ok": False, "error": _last_error}

        scheme, host, tport = _upstream_base()
        print(
            f"[luci-proxy] listening http://{used_bind}:{port}/ → "
            f"{scheme}://{host or '?'}:{tport}/"
        )
        return {**status(), "ok": True}


def stop() -> dict[str, Any]:
    """Stop proxy listener."""
    global _server, _thread, _started_at, _last_error
    with _lock:
        srv = _server
        th = _thread
        _server = None
        _thread = None
        _started_at = None
    if srv is not None:
        try:
            srv.shutdown()
        except Exception as e:
            print(f"[luci-proxy] shutdown: {e}")
        try:
            srv.server_close()
        except Exception:
            pass
        print("[luci-proxy] stopped")
    if th is not None and th.is_alive():
        th.join(timeout=3.0)
    return {**status(), "ok": True}


def apply() -> dict[str, Any]:
    """
    Start if enabled, stop if disabled.
    If already running with different port/bind — restart.
    """
    with _lock:
        want = bool(_cfg.get("enabled"))
        bind = str(_cfg.get("bind") or DEFAULT_BIND)
        port = int(_cfg.get("listen_port") or DEFAULT_LISTEN_PORT)
        running = (
            _server is not None
            and _thread is not None
            and _thread.is_alive()
        )
        cur_bind = None
        cur_port = None
        if _server is not None:
            try:
                cur_bind, cur_port = _server.server_address[:2]
            except Exception:
                pass

    if not want:
        return stop()

    if running and (str(cur_bind) != bind or int(cur_port or 0) != port):
        stop()
        return start()
    if running:
        return {**status(), "ok": True, "note": "already running"}
    # not running (or dead thread) — start cleanly
    if not running and (_server is not None or _thread is not None):
        stop()
    return start()


def is_running() -> bool:
    with _lock:
        return bool(
            _server is not None
            and _thread is not None
            and _thread.is_alive()
        )


# socket helper for status/self-test
def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
