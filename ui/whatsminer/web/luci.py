"""
Whatsminer LuCI (HTTP/HTTPS web) client — **stock** firmware admin UI.

On modern firmware (e.g. 20250915 M63), public write API is gated by apiswitch,
but factory web login admin/admin still allows control via LuCI — without
changing the password and without enabling Miner API Switch.

Pools, restart, reboot, web password live here.

**WMOC** lives in :mod:`whatsminer.web.wmoc` (detect, history, overclock, tools).
:class:`LuCIClient` keeps thin ``detect_wmoc`` / history helpers that delegate there.
"""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Optional


class LuCIError(RuntimeError):
    pass


def _normalize_base(
    host: str,
    *,
    scheme: Optional[str] = None,
    port: Optional[int] = None,
    base_url: Optional[str] = None,
) -> tuple[str, str, Optional[int], str]:
    """
    Build LuCI base URL.

    ``host`` may be ``192.168.1.10``, ``192.168.1.10:8788``, or a full URL.
    Returns ``(host_only, scheme, port, base)``.
    """
    if base_url:
        base = base_url.rstrip("/")
        parsed = urllib.parse.urlparse(base if "://" in base else f"https://{base}")
        host_only = parsed.hostname or host
        sch = parsed.scheme or "https"
        prt = parsed.port
        netloc = parsed.netloc or host_only
        return host_only, sch, prt, f"{sch}://{netloc}"

    raw = host.strip()
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        host_only = parsed.hostname or raw
        sch = scheme or parsed.scheme or "https"
        prt = port if port is not None else parsed.port
        if prt:
            return host_only, sch, prt, f"{sch}://{host_only}:{prt}"
        return host_only, sch, None, f"{sch}://{host_only}"

    # host:port without scheme
    if scheme is None and port is None and re.match(r"^[^/]+:\d+$", raw):
        host_only, _, p = raw.rpartition(":")
        # default non-443 ports often used for HTTP LuCI reverse-proxy
        sch = "http" if int(p) not in (443, 8443) else "https"
        return host_only, sch, int(p), f"{sch}://{host_only}:{p}"

    host_only = raw
    sch = scheme or "https"
    if port is not None:
        return host_only, sch, int(port), f"{sch}://{host_only}:{int(port)}"
    return host_only, sch, None, f"{sch}://{host_only}"


class LuCIClient:
    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "admin",
        *,
        timeout: float = 10.0,
        verify_tls: bool = False,
        scheme: Optional[str] = None,
        port: Optional[int] = None,
        base_url: Optional[str] = None,
    ):
        """
        Parameters
        ----------
        host:
            Hostname/IP, optional ``host:port``, or full URL.
        scheme / port / base_url:
            Override URL. Example WMOC lab: ``host="10.121.15.76", port=8788, scheme="http"``
            or ``host="http://10.121.15.76:8788"``.
        """
        host_only, sch, prt, base = _normalize_base(
            host, scheme=scheme, port=port, base_url=base_url
        )
        self.host = host_only
        self.scheme = sch
        self.port = prt
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base = base
        self._cj = CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self._cj)]
        if sch == "https":
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            handlers.insert(0, urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)
        self._token: Optional[str] = None
        self._logged_in = False
        self._wmoc_cache: Optional[dict[str, Any]] = None

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def _open(self, path: str, data: Optional[bytes] = None, method: Optional[str] = None) -> tuple[int, bytes, str]:
        headers = {"User-Agent": "whatsminer-lib/0.1"}
        req = urllib.request.Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
                return resp.status, body, resp.geturl()
        except urllib.error.HTTPError as e:
            body = e.read() if e.fp else b""
            return e.code, body, self._url(path)
        except urllib.error.URLError as e:
            raise LuCIError(f"connection failed {self.base}: {e}") from e

    def login(self) -> None:
        form = urllib.parse.urlencode(
            {"luci_username": self.username, "luci_password": self.password}
        ).encode()
        code, body, url = self._open("/cgi-bin/luci", data=form, method="POST")
        # success: 302 + sysauth cookie, or 200 home
        has_cookie = any(c.name == "sysauth" for c in self._cj)
        if not has_cookie and code not in (200, 302):
            raise LuCIError(f"login failed code={code} body={body[:200]!r}")
        # confirm by fetching status
        code, body, _ = self._open("/cgi-bin/luci/admin/status/btminerstatus")
        if code == 403 or b"Invalid username" in body or b"luci_username" in body and b"Miner Status" not in body:
            # some firmwares return login form again
            if b"Miner Status" not in body and b"btminerstatus" not in body:
                raise LuCIError(f"login rejected (code={code})")
        self._token = self._extract_token(body) or self._token
        self._logged_in = True

    def ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    @staticmethod
    def _extract_token(html: bytes | str) -> Optional[str]:
        if isinstance(html, bytes):
            html = html.decode("utf-8", errors="replace")
        m = re.search(r'name="token"\s+value="([0-9a-f]+)"', html)
        return m.group(1) if m else None

    def get_html(self, path: str) -> str:
        self.ensure_login()
        code, body, _ = self._open(path)
        if code >= 400:
            raise LuCIError(f"GET {path} -> {code}")
        text = body.decode("utf-8", errors="replace")
        tok = self._extract_token(text)
        if tok:
            self._token = tok
        return text

    # ── WMOC (delegates to whatsminer.web.wmoc — preferred import path) ──────────

    def detect_wmoc(self, *, login: bool = True, use_cache: bool = True) -> dict[str, Any]:
        """See :func:`whatsminer.web.wmoc.detect_on_client`."""
        from . import wmoc as _wmoc

        return _wmoc.detect_on_client(self, login=login, use_cache=use_cache)

    def has_wmoc(self, *, login: bool = True) -> bool:
        """See :func:`whatsminer.web.wmoc.has_wmoc`."""
        from . import wmoc as _wmoc

        return _wmoc.has_wmoc(self, login=login)

    def get_wmoc_history(
        self,
        *,
        max_records: Optional[int] = None,
    ) -> dict[str, Any]:
        """See :func:`whatsminer.web.wmoc.get_history`."""
        from . import wmoc as _wmoc

        return _wmoc.get_history(self, max_records=max_records)

    def analyze_wmoc_psu(
        self,
        *,
        history: Optional[dict[str, Any]] = None,
        sample: int = 50,
    ) -> dict[str, Any]:
        """See :func:`whatsminer.web.wmoc.analyze_psu`."""
        from . import wmoc as _wmoc

        return _wmoc.analyze_psu(history=history, client=self, sample=sample)

    def restart_miner(self) -> None:
        """Restart btminer process (LuCI button)."""
        self.ensure_login()
        code, _, _ = self._open("/cgi-bin/luci/admin/status/btminerstatus/restart")
        if code not in (200, 302):
            raise LuCIError(f"restart_miner failed code={code}")

    def reboot(self) -> None:
        """
        Trigger system reboot page action if available.
        Fallback: POST common LuCI reboot form endpoints.
        """
        self.ensure_login()
        # many Whatsminer LuCI builds use JS reboot hitting this:
        for path in (
            "/cgi-bin/luci/admin/system/reboot/call",
            "/cgi-bin/luci/admin/system/reboot?reboot=1",
        ):
            code, _, _ = self._open(path)
            if code in (200, 302):
                return
        # last resort: load page (user may need manual confirm)
        self.get_html("/cgi-bin/luci/admin/system/reboot")

    @staticmethod
    def _input_value(html: str, name: str) -> str:
        """Extract ``value`` for a named ``<input>`` (order of attrs varies)."""
        esc = re.escape(name)
        m = re.search(
            rf'<input[^>]*\bname="{esc}"[^>]*\bvalue="([^"]*)"',
            html,
            re.I,
        )
        if m:
            return m.group(1)
        m = re.search(
            rf'<input[^>]*\bvalue="([^"]*)"[^>]*\bname="{esc}"',
            html,
            re.I,
        )
        return m.group(1) if m else ""

    @staticmethod
    def _select_value(html: str, name: str) -> str:
        esc = re.escape(name)
        m = re.search(
            rf'<select[^>]*\bname="{esc}"[^>]*>(.*?)</select>',
            html,
            re.I | re.S,
        )
        if not m:
            return ""
        block = m.group(1)
        sel = re.search(
            r'<option[^>]*\bselected\b[^>]*\bvalue="([^"]*)"',
            block,
            re.I,
        )
        if sel:
            return sel.group(1)
        sel = re.search(
            r'<option[^>]*\bvalue="([^"]*)"[^>]*\bselected\b',
            block,
            re.I,
        )
        if sel:
            return sel.group(1)
        first = re.search(r'<option[^>]*\bvalue="([^"]*)"', block, re.I)
        return first.group(1) if first else ""

    def get_pools(self) -> dict[str, Any]:
        """
        Read pool configuration from LuCI ``/admin/network/btminer``.

        Unlike public API ``pools``, the web form includes **pool passwords**
        (``pool1pw`` … ``pool3pw``) in plaintext input values.

        Returns::

            {
              "coin_type": "BTC",
              "pools": [
                {"index": 0, "url": "...", "user": "...", "password": "..."},
                ...
              ],
              "source": "luci",
            }
        """
        html = self.get_html("/cgi-bin/luci/admin/network/btminer")
        coin = self._select_value(html, "cbid.pools.default.coin_type") or self._input_value(
            html, "cbid.pools.default.coin_type"
        )
        pools: list[dict[str, Any]] = []
        for i in range(1, 4):
            url = self._input_value(html, f"cbid.pools.default.pool{i}url")
            user = self._input_value(html, f"cbid.pools.default.pool{i}user")
            pw = self._input_value(html, f"cbid.pools.default.pool{i}pw")
            if not url and not user and not pw:
                continue
            pools.append(
                {
                    "index": i - 1,
                    "url": url,
                    "user": user,
                    "password": pw,
                    "url_full": url
                    if url.startswith("stratum")
                    else (f"stratum+tcp://{url}" if url else ""),
                }
            )
        return {"coin_type": coin, "pools": pools, "source": "luci"}

    def update_pools(
        self,
        pool1: str = "",
        worker1: str = "",
        passwd1: str = "x",
        pool2: str = "",
        worker2: str = "",
        passwd2: str = "",
        pool3: str = "",
        worker3: str = "",
        passwd3: str = "",
        coin_type: str = "BTC",
        *,
        pools: Optional[list[dict[str, Any]]] = None,
        restart_mining: bool = False,
    ) -> dict[str, Any]:
        """
        Write pool config via LuCI ``/admin/network/btminer`` (Save & Apply).

        **Deferred switch:** the UCI/config is updated, but the running
        ``btminer`` process often **keeps the old stratum session** until
        mining is restarted or the unit reboots. Pass
        ``restart_mining=True`` to hit LuCI *Restart* after save
        (``/admin/status/btminerstatus/restart``).

        Accepts either the classic pool1/worker1/… args or
        ``pools=[{url,user,password}, ...]`` (up to 3, same shape as
        :meth:`get_pools` / NetPacket ``set_pools``).
        """
        if pools is not None:
            slots = [{"url": "", "user": "", "password": ""} for _ in range(3)]
            for i, p in enumerate(pools[:3]):
                url = str(p.get("url") or p.get("pool") or p.get("url_full") or "")
                user = str(p.get("user") or p.get("worker") or "")
                pw = str(
                    p.get("password")
                    if p.get("password") is not None
                    else p.get("passwd")
                    if p.get("passwd") is not None
                    else p.get("pass")
                    if p.get("pass") is not None
                    else "x"
                )
                slots[i] = {"url": url, "user": user, "password": pw}
            pool1, worker1, passwd1 = slots[0]["url"], slots[0]["user"], slots[0]["password"]
            pool2, worker2, passwd2 = slots[1]["url"], slots[1]["user"], slots[1]["password"]
            pool3, worker3, passwd3 = slots[2]["url"], slots[2]["user"], slots[2]["password"]
        elif not pool1 or not worker1:
            raise ValueError("update_pools requires pool1+worker1 or pools=[...]")

        html = self.get_html("/cgi-bin/luci/admin/network/btminer")
        if not self._token:
            raise LuCIError("no CSRF token from pools page")
        # Keep coin_type from form if caller left default and page has a value
        page_coin = self._select_value(html, "cbid.pools.default.coin_type")
        if coin_type == "BTC" and page_coin:
            coin_type = page_coin
        form = {
            "token": self._token,
            "cbi.submit": "1",
            "cbi.apply": "Save & Apply",
            "cbid.pools.default.coin_type": coin_type,
            "cbid.pools.default.pool1url": pool1,
            "cbid.pools.default.pool1user": worker1,
            "cbid.pools.default.pool1pw": passwd1,
            "cbid.pools.default.pool2url": pool2,
            "cbid.pools.default.pool2user": worker2,
            "cbid.pools.default.pool2pw": passwd2,
            "cbid.pools.default.pool3url": pool3,
            "cbid.pools.default.pool3user": worker3,
            "cbid.pools.default.pool3pw": passwd3,
        }
        data = urllib.parse.urlencode(form).encode()
        code, body, _ = self._open("/cgi-bin/luci/admin/network/btminer", data=data, method="POST")
        if code >= 400:
            raise LuCIError(f"update_pools failed code={code} body={body[:200]!r}")

        out: dict[str, Any] = {
            "ok": True,
            "source": "luci",
            "coin_type": coin_type,
            "pools": [
                {"index": 0, "url": pool1, "user": worker1, "password": passwd1},
                {"index": 1, "url": pool2, "user": worker2, "password": passwd2},
                {"index": 2, "url": pool3, "user": worker3, "password": passwd3},
            ],
            "applied": "config",
            "active_switch": "deferred",
            "note": (
                "Pools written to LuCI/UCI config. Running miner may keep old "
                "stratum until restart_mining or reboot."
            ),
            "http_status": code,
        }
        # drop empty trailing slots from report
        out["pools"] = [
            p for p in out["pools"] if p["url"] or p["user"] or p["password"]
        ]

        if restart_mining:
            self.restart_miner()
            out["restart_mining"] = True
            out["active_switch"] = "restart_mining"
            out["note"] = (
                "Pools written and btminer restart requested via LuCI; "
                "new stratum should be picked up after process restart."
            )
        else:
            out["restart_mining"] = False
        return out

    def set_pools(
        self,
        pools: list[dict[str, Any]],
        *,
        coin_type: str = "BTC",
        restart_mining: bool = False,
    ) -> dict[str, Any]:
        """
        Alias of :meth:`update_pools` with ``pools=[...]``.

        LuCI path: config write only by default (deferred stratum switch).
        """
        return self.update_pools(
            pools=pools, coin_type=coin_type, restart_mining=restart_mining
        )

    def reinstall_pools(
        self,
        pools: Optional[list[dict[str, Any]]] = None,
        *,
        coin_type: str = "BTC",
        restart_mining: bool = False,
    ) -> dict[str, Any]:
        """
        Re-write pools via LuCI (переустановка конфига пулов).

        If ``pools`` is omitted, re-saves the current form values (useful to
        force UCI rewrite). Same deferred-activation semantics as
        :meth:`update_pools` unless ``restart_mining=True``.
        """
        if pools is None:
            cur = self.get_pools()
            pools = cur.get("pools") or []
            if coin_type == "BTC" and cur.get("coin_type"):
                coin_type = str(cur["coin_type"])
        if not pools:
            raise ValueError("reinstall_pools: no pools to write")
        return self.update_pools(
            pools=pools, coin_type=coin_type, restart_mining=restart_mining
        )

    def set_power_mode(self, mode: str | int) -> None:
        """
        Set power mode via LuCI /admin/network/btminer/power.
        mode: 'low'|'normal'|'high' or 0|1|2
        """
        mapping = {"low": "0", "normal": "1", "high": "2", 0: "0", 1: "1", 2: "2"}
        if isinstance(mode, str):
            key: str | int = mode.lower().strip()
        else:
            key = mode
        if key not in mapping:
            raise ValueError("mode must be low|normal|high or 0|1|2")
        html = self.get_html("/cgi-bin/luci/admin/network/btminer/power")
        if not self._token:
            raise LuCIError("no CSRF token from power page")
        form = {
            "token": self._token,
            "cbi.submit": "1",
            "cbi.apply": "Save & Apply",
            "cbid.btminer.default.miner_type": mapping[key],
        }
        data = urllib.parse.urlencode(form).encode()
        code, body, _ = self._open(
            "/cgi-bin/luci/admin/network/btminer/power", data=data, method="POST"
        )
        if code >= 400:
            raise LuCIError(f"set_power_mode failed code={code} body={body[:200]!r}")

    def change_web_password(self, new_password: str) -> None:
        html = self.get_html("/cgi-bin/luci/admin/system/admin")
        if not self._token:
            raise LuCIError("no CSRF token from admin page")
        form = {
            "token": self._token,
            "cbi.submit": "1",
            "cbi.apply": "Save & Apply",
            "cbid.system._pass.pw1": new_password,
            "cbid.system._pass.pw2": new_password,
        }
        data = urllib.parse.urlencode(form).encode()
        code, body, _ = self._open("/cgi-bin/luci/admin/system/admin", data=data, method="POST")
        if code >= 400:
            raise LuCIError(f"change_web_password failed code={code}")
        self.password = new_password
        self._logged_in = False  # force re-login

    def status_snapshot(self) -> dict[str, Any]:
        """Parse key hidden fields from Miner Status page."""
        html = self.get_html("/cgi-bin/luci/admin/status/btminerstatus")
        fields = dict(re.findall(r'id="cbid\.table\.1\.(\w+)" value="([^"]*)"', html))
        return fields
