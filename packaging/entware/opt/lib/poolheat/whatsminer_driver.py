#!/usr/bin/env python3
"""
Whatsminer control driver for poolheat_WM.

Combines proven paths:

1) **HTTPS/HTTP LuCI** (same strategy as btccom/libbtctools WhatsMinerHttpsLuci)
   - login: POST /cgi-bin/luci  luci_username + luci_password
   - CSRF token from forms
   - pools:   POST /cgi-bin/luci/admin/network/{btminer|cgminer}
   - power:   POST .../network/{btminer|cgminer}/power  (miner_type 0/1/2)
   - restart: GET  .../status/{btminer|cgminer}status/restart
   - reboot:  POST .../system/reboot/call  (or form on /system/reboot)
   Works **without** Miner API Switch / public write API.

2) **TCP API v2 :4028** — privileged AES write (get_token + enc) when unlocked.

3) **TCP API v3 :4433** — length-prefixed JSON when apiswitch=1.

4) **:8889 Remote** (WhatsMinerTools) — `Remote8889Client`
   - AES-256-ECB KEY0/KEY1, magic 5A5A7F7F, CRC body
   - auth → session; status 0x16; power_limit 14=W; suspend 8=0/1; pools 0x02
   - live-verified on M63 (write ack ffff = OK)

Write strategy:
  probe apiswitch → if ON: TCP first; if OFF: LuCI / :8889 alternatives.

Reference:
  https://github.com/btccom/libbtctools
  src/lua/scripts/{configurator,rebooter,scanner}/WhatsMinerHttpsLuci.lua
  tools/whatsminer-proxy/CAPTURE-NOTES.md
"""

from __future__ import annotations

import binascii
import http.cookiejar
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from Crypto.Cipher import AES as _AES
except ImportError:  # pragma: no cover
    try:
        from Cryptodome.Cipher import AES as _AES  # type: ignore
    except ImportError:  # pragma: no cover
        _AES = None  # type: ignore

# pyasic cloud helper for :8889 «open write API» (UpstreamData)
# See: UpstreamData/pyasic pyasic/rpc/btminer.py open_api()
# NOTE: auto-call is currently commented out in upstream pyasic itself.
PYASIC_WMT_STAGE1 = "https://wmt.pyasic.org/v1/stage1"
PYASIC_WMT_STAGE2 = "https://wmt.pyasic.org/v1/stage2"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _ok_resp(msg: str, *, transport: str, **extra) -> dict:
    out = {
        "STATUS": "S",
        "Code": 131,
        "Msg": msg,
        "transport": transport,
    }
    out.update(extra)
    return out


def _f(x) -> float | None:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


# ─── LuCI client (libbtctools-compatible) ────────────────────────────────────


class LuciClient:
    """
    Sessioned LuCI over HTTPS (preferred) or HTTP.
    Mirrors btccom makeLuciSessionReq / makeSessionedHttpReq / form POSTs.
    """

    def __init__(
        self,
        host: str,
        *,
        username: str = "admin",
        password: str = "admin",
        prefer_https: bool = True,
        timeout: float = 15.0,
    ):
        self.host = str(host).strip()
        self.username = username or "admin"
        self.password = password if password is not None else "admin"
        self.prefer_https = prefer_https
        self.timeout = timeout
        self._scheme = "https" if prefer_https else "http"
        self._opener: Any = None
        self._program: str | None = None  # btminer | cgminer
        self._lock = threading.RLock()

    def _ssl_ctx(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _base(self) -> str:
        return f"{self._scheme}://{self.host}"

    def clear(self) -> None:
        with self._lock:
            self._opener = None
            self._program = None

    def login(self, *, allow_empty_password: bool = True) -> None:
        """POST /cgi-bin/luci — expect 302 + Set-Cookie (libbtctools)."""
        with self._lock:
            schemes = (
                ("https", "http") if self.prefer_https else ("http", "https")
            )
            last_err: Exception | None = None
            for scheme in schemes:
                self._scheme = scheme
                for no_pwd in (False, True) if allow_empty_password else (False,):
                    try:
                        jar = http.cookiejar.CookieJar()
                        handlers: list = [urllib.request.HTTPCookieProcessor(jar)]
                        if scheme == "https":
                            handlers.insert(
                                0, urllib.request.HTTPSHandler(context=self._ssl_ctx())
                            )
                        opener = urllib.request.build_opener(*handlers)
                        pw = "" if no_pwd else (self.password or "")
                        body = urllib.parse.urlencode(
                            {
                                "luci_username": self.username,
                                "luci_password": pw,
                            }
                        ).encode("utf-8")
                        req = urllib.request.Request(
                            self._base() + "/cgi-bin/luci",
                            data=body,
                            method="POST",
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded",
                                "User-Agent": "poolheat-whatsminer-driver/1.0",
                            },
                        )
                        # Don't follow redirects automatically for status check —
                        # urllib follows 302 by default which is fine if cookie set.
                        try:
                            with opener.open(req, timeout=self.timeout) as resp:
                                _ = resp.read(256)
                        except urllib.error.HTTPError as e:
                            # some FW return 403 without cookie
                            if e.code not in (302, 200, 403):
                                raise
                        # cookie present?
                        cookies = list(jar)
                        if not cookies:
                            # try empty password next / next scheme
                            last_err = RuntimeError("LuCI login: no session cookie")
                            continue
                        self._opener = opener
                        return
                    except Exception as e:
                        last_err = e
                        continue
            raise RuntimeError(f"LuCI login failed: {last_err}")

    def _ensure(self) -> Any:
        if self._opener is None:
            self.login()
        return self._opener

    def request(
        self,
        path: str,
        *,
        data: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        opener = self._ensure()
        url = self._base() + path
        body = None
        headers = {"User-Agent": "poolheat-whatsminer-driver/1.0"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        to = self.timeout if timeout is None else timeout
        try:
            with opener.open(req, timeout=to) as resp:
                return int(getattr(resp, "status", 200) or 200), resp.read().decode(
                    "utf-8", "replace"
                )
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            if int(e.code) in (403, 401):
                self.clear()
                opener = self._ensure()
                try:
                    with opener.open(req, timeout=to) as resp:
                        return int(getattr(resp, "status", 200) or 200), resp.read().decode(
                            "utf-8", "replace"
                        )
                except urllib.error.HTTPError as e2:
                    raw2 = e2.read() if e2.fp else b""
                    return int(e2.code), raw2.decode("utf-8", "replace")
            return int(e.code), raw.decode("utf-8", "replace")

    @staticmethod
    def extract_token(html: str) -> str:
        # form field (pools/power) or JS token:'…' (reboot page — libbtctools)
        m = re.search(r'name=["\']token["\']\s+value=["\']([^"\']+)["\']', html)
        if not m:
            m = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']token["\']', html)
        if not m:
            m = re.search(r"token:\s*'([^']+)'", html)
        if not m:
            m = re.search(r'token:\s*"([^"]+)"', html)
        if not m:
            raise RuntimeError("LuCI CSRF token not found")
        return m.group(1)

    def detect_program(self) -> str:
        """btminer first (modern), then cgminer — same order as MiningProgram:WhatsMinerDefault."""
        if self._program:
            return self._program
        for prog in ("btminer", "cgminer"):
            st, html = self.request(f"/cgi-bin/luci/admin/network/{prog}/power")
            if st == 200 and ("token" in html or "miner_type" in html or "cbi" in html):
                self._program = prog
                return prog
            st2, html2 = self.request(f"/cgi-bin/luci/admin/network/{prog}")
            if st2 == 200 and ("pool1url" in html2 or "token" in html2):
                self._program = prog
                return prog
        self._program = "btminer"
        return self._program

    def set_power_mode(self, mode: str) -> dict:
        """
        libbtctools: POST .../network/{prog}/power
        cbid.{prog}.default.miner_type = 0|1|2  (Low|Normal|High)
        """
        m = str(mode or "").strip().lower()
        type_map = {"low": "0", "normal": "1", "high": "2", "0": "0", "1": "1", "2": "2"}
        if m not in type_map:
            raise ValueError("mode must be low|normal|high")
        prog = self.detect_program()
        path = f"/cgi-bin/luci/admin/network/{prog}/power"
        st, html = self.request(path)
        if st == 404:
            self._program = None
            prog = self.detect_program()
            path = f"/cgi-bin/luci/admin/network/{prog}/power"
            st, html = self.request(path)
        if st != 200:
            raise RuntimeError(f"LuCI power page HTTP {st}")
        token = self.extract_token(html)
        field = f"cbid.{prog}.default.miner_type"
        st2, _ = self.request(
            path,
            data={
                "token": token,
                "cbi.submit": "1",
                "cbi.apply": "1",
                field: type_map[m],
            },
            timeout=25.0,
        )
        if st2 not in (200, 302):
            raise RuntimeError(f"LuCI set power mode HTTP {st2}")
        return _ok_resp(
            f"LuCI power mode → {m}",
            transport="luci",
            mode=m if m in ("low", "normal", "high") else {"0": "low", "1": "normal", "2": "high"}.get(m, m),
            program=prog,
        )

    def set_pools(
        self,
        pools: list[dict],
        *,
        coin_type: str | None = None,
    ) -> dict:
        """
        libbtctools setMinerConf:
        POST /cgi-bin/luci/admin/network/{prog}
        cbid.pools.default.pool{N}url|user|pw
        """
        prog = self.detect_program()
        path = f"/cgi-bin/luci/admin/network/{prog}"
        st, html = self.request(path)
        if st == 404:
            self._program = None
            prog = self.detect_program()
            path = f"/cgi-bin/luci/admin/network/{prog}"
            st, html = self.request(path)
        if st != 200:
            raise RuntimeError(f"LuCI pools page HTTP {st}")
        token = self.extract_token(html)
        if coin_type is None:
            m = re.search(
                r'id="cbid\.pools\.default\.coin_type[^"]*"\s*value="([^"]*)"[^>]*selected',
                html,
            )
            if not m:
                m = re.search(
                    r'value="([^"]*)"[^>]*selected[^>]*id="cbid\.pools\.default\.coin_type',
                    html,
                )
            coin_type = m.group(1) if m else ""
        data: dict[str, str] = {
            "token": token,
            "cbi.submit": "1",
            "cbi.apply": "1",
            "cbid.pools.default.coin_type": coin_type or "",
        }
        for i in range(1, 4):
            p = pools[i - 1] if i - 1 < len(pools) else {}
            data[f"cbid.pools.default.pool{i}url"] = str(p.get("url") or p.get("URL") or "")
            data[f"cbid.pools.default.pool{i}user"] = str(
                p.get("user") or p.get("User") or p.get("worker") or ""
            )
            data[f"cbid.pools.default.pool{i}pw"] = str(
                p.get("pass") or p.get("password") or p.get("Pass") or "x"
            )
        st2, _ = self.request(path, data=data, timeout=25.0)
        if st2 not in (200, 302):
            raise RuntimeError(f"LuCI set pools HTTP {st2}")
        # restart mining process (libbtctools restartCGMiner)
        try:
            self.restart_btminer()
        except Exception:
            pass
        return _ok_resp("LuCI pools updated", transport="luci", program=prog, pools=pools[:3])

    def restart_btminer(self) -> dict:
        """GET /cgi-bin/luci/admin/status/{prog}status/restart — expect 302."""
        prog = self.detect_program()
        for p in (prog, "btminer", "cgminer"):
            path = f"/cgi-bin/luci/admin/status/{p}status/restart"
            st, _ = self.request(path)
            if st in (200, 302):
                return _ok_resp(
                    f"LuCI restart {p}",
                    transport="luci",
                    program=p,
                    http=st,
                )
            if st != 404:
                raise RuntimeError(f"LuCI restart HTTP {st}")
        raise RuntimeError("LuCI restart path not found")

    def reboot(self) -> dict:
        """
        libbtctools: token from /admin/system/reboot then
        POST /admin/system/reboot/call  body token=…
        """
        st, html = self.request("/cgi-bin/luci/admin/system/reboot")
        token = None
        try:
            if st == 200:
                token = self.extract_token(html)
        except Exception:
            token = None
        # primary: call endpoint
        if token:
            try:
                st2, _ = self.request(
                    "/cgi-bin/luci/admin/system/reboot/call",
                    data={"token": token},
                    timeout=8.0,
                )
                if st2 in (200, 302, 500):
                    return _ok_resp("LuCI reboot/call", transport="luci", http=st2)
            except (TimeoutError, socket.timeout, OSError, urllib.error.URLError):
                return _ok_resp("LuCI reboot/call sent (link dropped)", transport="luci")
        # fallback form posts
        for data in (
            {"token": token, "cbi.submit": "1", "reboot": "1"} if token else None,
            {"reboot": "1"},
        ):
            if not data:
                continue
            try:
                st3, _ = self.request(
                    "/cgi-bin/luci/admin/system/reboot", data=data, timeout=8.0
                )
                if st3 in (200, 302, 500):
                    return _ok_resp("LuCI reboot form", transport="luci", http=st3)
            except Exception:
                return _ok_resp("LuCI reboot sent (link dropped)", transport="luci")
        return _ok_resp("LuCI reboot attempted", transport="luci")

    def enable_api_switch(self, enable: bool = True) -> dict:
        """
        Best-effort UCI open_by_api / apiswitch via Power page POST.
        (Not in libbtctools; poolheat-specific unlock for TCP write.)

        WhatsMinerTools does the same over proprietary :8889; on M63 LuCI we
        post hidden CBI fields accepted by the backend even when the UI hides them.
        Double-apply: some firmwares only stick options on the second UCI commit.
        """
        prog = self.detect_program()
        path = f"/cgi-bin/luci/admin/network/{prog}/power"
        st, html = self.request(path)
        if st != 200:
            raise RuntimeError(f"LuCI power page HTTP {st}")
        token = self.extract_token(html)
        val = "1" if enable else "0"
        data: dict[str, str] = {
            "token": token,
            "cbi.submit": "1",
            "cbi.apply": "1",
            f"cbid.{prog}.default.miner_type": "1",
            f"cbid.{prog}.default.open_by_api": val,
            f"cbid.{prog}.default.apiswitch": val,
            f"cbid.{prog}.default.api_switch": val,
            "cbid.miner_setting.default.open_by_api": val,
            "cbid.miner_setting.@miner_setting[0].open_by_api": val,
            "cbid.btminer.default.open_by_api": val,
            "cbid.btminer.default.apiswitch": val,
            "cbid.btminer.default.api_switch": val,
            "cbid.system.default.open_by_api": val,
        }
        m = re.search(
            rf'name="cbid\.{re.escape(prog)}\.default\.miner_type"[^>]*value="([012])"[^>]*checked',
            html,
            re.I,
        )
        if not m:
            m = re.search(
                rf'value="([012])"[^>]*checked[^>]*name="cbid\.{re.escape(prog)}\.default\.miner_type"',
                html,
                re.I,
            )
        if m:
            data[f"cbid.{prog}.default.miner_type"] = m.group(1)
        st2, _ = self.request(path, data=data, timeout=25.0)
        # second apply sometimes needed after UCI create
        try:
            st3, html3 = self.request(path)
            if st3 == 200:
                data2 = dict(data)
                data2["token"] = self.extract_token(html3)
                self.request(path, data=data2, timeout=25.0)
        except Exception:
            pass
        time.sleep(1.0)
        return _ok_resp(
            f"LuCI open_by_api={'1' if enable else '0'}",
            transport="luci",
            enable=bool(enable),
            http=st2,
        )


# ─── WhatsMinerTools Remote :8889 ─────────────────────────────────────────────
# Protocol (reversed from WMT pcap + live M63, firmware keys KEY0/1/2):
#
#   Client frame (AES-256-ECB encrypt whole PT):
#     magic 5A5A7F7F | cmd u32LE | f2 u32LE | crc u32LE | body + zero-pad
#     f2 = (payload_len << 16) | cred_len
#     body = f"{miner_ip}|{unix_ts}|{account}|{password}|{session}{payload}"
#     crc  = (~zlib.crc32(body)) & 0xffffffff   # no trailing NUL
#     KEY0 for cmd 0 (auth); KEY1 for status/writes (0x16, 0x0d, 0x02, …)
#
#   Auth (cmd=0, KEY0) → 24 B plaintext: status u32 + session 4 B
#   Status (cmd=0x16, KEY1) → large plaintext [MinerInfo]…
#   Write ack often 16 B with tail ffff — live-confirmed SUCCESS for power_limit
#     payload "14=<watts>" (cmd 0x0d), "8=1"/"8=0" suspend/resume
#     pools cmd 0x02: "0,url,user,FAILOVER,,pass|"
#
# See tools/whatsminer-proxy/CAPTURE-NOTES.md


class Remote8889Error(RuntimeError):
    """Raised for :8889 protocol / capability errors."""


class Remote8889Client:
    """
    WhatsMinerTools proprietary Remote Ctrl (TCP :8889).

    Working control path when apiswitch=0 (LuCI still preferred for mode/pools
    where it is reliable; 8889 covers suspend / power_limit / telemetry).
    """

    MAGIC = bytes.fromhex("5a5a7f7f")
    PORT = 8889

    # Firmware constants (remote-daemon AES-256 keys) — user-supplied / VA table
    KEY0 = bytes.fromhex(
        "f0d379ee4188bc6216cfa09adcd49100ee7f971217aaba26bc86c0b6ae1da90f"
    )  # auth / ordinary
    KEY1 = bytes.fromhex(
        "66476cc48201182b9c27c302e48e120724a0e460fb970474a7539a48e787c296"
    )  # status + writes
    KEY2 = bytes.fromhex(
        "9be70afc109f7756383155083c120910ddfff76720a34786fa272611ead19bf1"
    )  # reserved / unused in WMT capture

    CMD_AUTH = 0x00
    CMD_POOLS = 0x02
    CMD_WRITE = 0x0D  # power limit / suspend fields
    CMD_HASHRATE = 0x0F
    CMD_SUMMARY = 0x13
    CMD_STATUS = 0x16  # [MinerInfo] + [PowerInfo]
    CMD_POWER_RT = 0x1A

    # Bad framing / decrypt (not write-ack): mid field 0x02 + ffff
    ERR_BAD_FRAME_MID = 0x02

    def __init__(
        self,
        host: str,
        *,
        password: str = "super",
        account: str = "super",
        port: int = 8889,
        timeout: float = 6.0,
        miner_ip: str | None = None,
        # legacy unused (old half-key hypothesis)
        key_mid: bytes | None = None,
        key_tail: bytes | None = None,
        mac: str | None = None,
        salt: str | None = None,
    ):
        self.host = str(host).strip()
        # Tools uses account=super password=super in body; non-empty password
        # is enough for read; writes work with "super" on live M63.
        self.password = password if password is not None else "super"
        self.account = (account or "super").strip() or "super"
        self.port = int(port or self.PORT)
        self.timeout = float(timeout)
        # Body IP MUST be the miner address (not client) — live requirement
        self.miner_ip = (miner_ip or self.host).strip()
        self.key_mid = key_mid
        self.key_tail = key_tail
        self.mac = mac
        self.salt = salt
        self._session: str | None = None  # 8 hex chars from auth ACK
        self._writes_ready = _AES is not None

    # ── crypto / frame build ────────────────────────────────────────────────

    @staticmethod
    def _require_aes() -> None:
        if _AES is None:
            raise Remote8889Error(
                "PyCryptodome required for :8889 (pip install pycryptodome)"
            )

    @classmethod
    def key_for_cmd(cls, cmd: int) -> bytes:
        """KEY0 for auth (0); KEY1 for everything observed in WMT capture."""
        if cmd == cls.CMD_AUTH:
            return cls.KEY0
        return cls.KEY1

    @classmethod
    def build_frame(
        cls,
        cmd: int,
        body: bytes,
        *,
        payload_len: int = 0,
        key: bytes | None = None,
    ) -> bytes:
        """Encrypt one client frame (AES-256-ECB)."""
        cls._require_aes()
        if payload_len < 0 or payload_len > len(body):
            raise ValueError("payload_len out of range")
        cred_len = len(body) - payload_len
        f2 = ((payload_len & 0xFFFF) << 16) | (cred_len & 0xFFFF)
        crc = (~zlib.crc32(body)) & 0xFFFFFFFF
        hdr = cls.MAGIC + struct.pack("<III", cmd & 0xFFFFFFFF, f2, crc)
        pad = (16 - (len(body) % 16)) % 16
        pt = hdr + body + (b"\x00" * pad)
        k = key if key is not None else cls.key_for_cmd(cmd)
        if len(k) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        return _AES.new(k, _AES.MODE_ECB).encrypt(pt)

    def _make_body(self, session: str, payload: bytes = b"") -> tuple[bytes, int]:
        ts = str(int(time.time()))
        cred = (
            f"{self.miner_ip}|{ts}|{self.account}|{self.password}|{session}"
        ).encode("utf-8")
        return cred + payload, len(payload)

    # ── framing parse ───────────────────────────────────────────────────────

    @classmethod
    def is_reject(cls, data: bytes) -> bool:
        """True for bad-frame rejects (mid=0x02). Write-ack ffff is NOT reject."""
        if not data or len(data) < 16 or data[:4] != cls.MAGIC:
            return False
        if len(data) != 16:
            return False
        mid = struct.unpack_from("<I", data, 8)[0]
        return mid == cls.ERR_BAD_FRAME_MID

    @classmethod
    def is_write_ack(cls, data: bytes) -> bool:
        """
        Short 16 B reply with tail 0x0000ffff — live-confirmed success for
        power_limit (PowerLimitSet changed). mid may be 0.
        """
        if not data or len(data) != 16 or data[:4] != cls.MAGIC:
            return False
        mid = struct.unpack_from("<I", data, 8)[0]
        tail = struct.unpack_from("<I", data, 12)[0]
        if mid == cls.ERR_BAD_FRAME_MID:
            return False
        return (tail & 0xFFFF) == 0xFFFF or tail == 0xFFFF

    @classmethod
    def parse_frame(cls, data: bytes) -> dict[str, Any]:
        """Parse one server chunk → {magic, type, body, text, reject, ini, …}."""
        out: dict[str, Any] = {
            "len": len(data) if data else 0,
            "magic": data[:4].hex() if data and len(data) >= 4 else "",
            "reject": cls.is_reject(data) if data else True,
            "write_ack": cls.is_write_ack(data) if data else False,
            "type": None,
            "body": b"",
            "text": "",
            "ini": {},
            "session": None,
            "ack_status": None,
        }
        if not data or len(data) < 4 or data[:4] != cls.MAGIC:
            out["text"] = data.decode("utf-8", "replace") if data else ""
            return out
        if len(data) >= 8:
            out["type"] = int.from_bytes(data[4:8], "little")
        body = data[16:] if len(data) > 16 else b""
        out["body"] = body
        # Auth ACK: 24 B → status u32 + session 4 B
        if len(data) >= 24 and out["type"] == 0 and len(body) >= 8:
            out["ack_status"] = struct.unpack_from("<I", body, 0)[0]
            out["session"] = body[4:8].hex()
        text = body.decode("utf-8", "replace") if body else ""
        out["text"] = text
        if (
            "[MinerInfo]" in text
            or "MinerType" in text
            or "[PowerInfo]" in text
            or "[PowerRealTimeInfo]" in text
        ):
            out["ini"] = cls.parse_ini_sections(text)
        return out

    @staticmethod
    def parse_ini_sections(text: str) -> dict[str, dict[str, str]]:
        """Parse WMT-style [Section] key = value blocks from 8889 telemetry."""
        sections: dict[str, dict[str, str]] = {}
        cur: str | None = None
        for line in (text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1].strip()
                sections.setdefault(cur, {})
                continue
            # also accept key=value glued after #MAC# without section refresh
            if "=" in s:
                # strip leading #MAC# junk
                if s.startswith("#") and "#[" in s:
                    # e.g. #CE:..#[PowerInfo]
                    br = s.find("[")
                    if br >= 0 and "]" in s[br:]:
                        cur = s[br + 1 : s.find("]", br)].strip()
                        sections.setdefault(cur, {})
                        rest = s[s.find("]", br) + 1 :].strip()
                        if rest.startswith("=") or "=" not in rest:
                            continue
                        s = rest
                if cur is None:
                    # free-form lines still collected under _root
                    cur = "_root"
                    sections.setdefault(cur, {})
                k, v = s.split("=", 1)
                sections[cur][k.strip()] = v.strip()
        return sections

    # ── socket I/O ──────────────────────────────────────────────────────────

    def probe(self) -> dict:
        """TCP connect only — port open? (does not prove auth)."""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return _ok_resp(
                    f":{self.port} open",
                    transport="remote_8889",
                    port=self.port,
                )
        except OSError as e:
            raise Remote8889Error(f":8889 probe failed: {e}") from e

    def raw_exchange(self, payload: bytes, *, recv_max: int = 65536) -> bytes:
        """Send one blob, read response chunk(s). One TCP session per call."""
        if not payload:
            raise ValueError("empty 8889 payload")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.settimeout(self.timeout)
            s.sendall(payload)
            chunks: list[bytes] = []
            total = 0
            try:
                while total < recv_max:
                    part = s.recv(min(8192, recv_max - total))
                    if not part:
                        break
                    chunks.append(part)
                    total += len(part)
                    joined = b"".join(chunks)
                    if len(joined) >= 16 and joined[:4] == self.MAGIC:
                        if len(joined) == 16:
                            break
                        # auth ACK 24 B
                        if len(joined) >= 24 and joined[4:8] == b"\x00\x00\x00\x00":
                            if len(joined) >= 24:
                                break
                        if b"[MinerInfo]" in joined and (
                            b"[PowerInfo]" in joined or total > 1500
                        ):
                            s.settimeout(0.35)
                            continue
                        if total >= 400 and (
                            b"[PowerRealTimeInfo]" in joined
                            or b"WhatsMiner-" in joined
                        ):
                            s.settimeout(0.35)
            except (socket.timeout, TimeoutError):
                pass
            return b"".join(chunks)

    def _exchange_cmd(
        self,
        cmd: int,
        *,
        session: str | None = None,
        payload: bytes = b"",
        key: bytes | None = None,
    ) -> dict[str, Any]:
        sess = session if session is not None else (self._session or "00000000")
        body, pay_len = self._make_body(sess, payload)
        pkt = self.build_frame(cmd, body, payload_len=pay_len, key=key)
        resp = self.raw_exchange(pkt)
        return self.parse_frame(resp)

    # ── session / status ────────────────────────────────────────────────────

    def auth(self, *, force: bool = False) -> str:
        """
        cmd=0 KEY0 → server assigns 4-byte session (hex). Cached on client.
        Body password is not strongly checked for this handshake on live M63.
        """
        if self._session and not force:
            return self._session
        fr = self._exchange_cmd(self.CMD_AUTH, session="00000000", key=self.KEY0)
        sess = fr.get("session")
        if not sess:
            raise Remote8889Error(
                f"8889 auth failed len={fr.get('len')} type={fr.get('type')}"
            )
        self._session = sess
        return sess

    def ensure_session(self) -> str:
        return self.auth(force=False)

    def poll_status(self, auth_packet: bytes | None = None) -> dict:
        """
        Full [MinerInfo]/[PowerInfo] telemetry (cmd 0x16).
        auth_packet ignored (legacy); uses KEY1 + server session.
        """
        if auth_packet is not None:
            # legacy path: raw 64 B already encrypted
            resp = self.raw_exchange(auth_packet)
            fr = self.parse_frame(resp)
        else:
            self.ensure_session()
            fr = self._exchange_cmd(self.CMD_STATUS)
        if fr.get("reject") or not fr.get("len"):
            # session expired — one retry with fresh auth
            self.auth(force=True)
            fr = self._exchange_cmd(self.CMD_STATUS)
        if fr.get("reject") or (
            not fr.get("ini") and "[MinerInfo]" not in (fr.get("text") or "")
        ):
            raise Remote8889Error(
                f"8889 status failed len={fr.get('len')} type={fr.get('type')}"
            )
        return _ok_resp(
            "8889 status",
            transport="remote_8889",
            ini=fr.get("ini") or {},
            type=fr.get("type"),
            raw_len=fr.get("len"),
            text=fr.get("text") or "",
            session=self._session,
        )

    def get_summary_line(self) -> dict:
        """cmd 0x13 — compact WhatsMiner-… CSV/summary blob."""
        self.ensure_session()
        fr = self._exchange_cmd(self.CMD_SUMMARY)
        if fr.get("reject") or not fr.get("text"):
            raise Remote8889Error("8889 summary failed")
        return _ok_resp(
            "8889 summary",
            transport="remote_8889",
            text=fr.get("text") or "",
            raw_len=fr.get("len"),
        )

    def get_power_rt(self) -> dict:
        """cmd 0x1a — [PowerRealTimeInfo] + [PowerInfo]."""
        self.ensure_session()
        fr = self._exchange_cmd(self.CMD_POWER_RT)
        if fr.get("reject"):
            raise Remote8889Error("8889 power_rt failed")
        return _ok_resp(
            "8889 power_rt",
            transport="remote_8889",
            ini=fr.get("ini") or {},
            text=fr.get("text") or "",
            raw_len=fr.get("len"),
        )

    # ── writes ──────────────────────────────────────────────────────────────

    def writes_implemented(self) -> bool:
        return bool(self._writes_ready)

    def _write_field(self, payload: str, *, cmd: int | None = None) -> dict:
        """
        Authenticated field write. payload e.g. '14=2500', '8=1'.
        Response 16 B with ffff is treated as OK (verified live).
        """
        if not self.writes_implemented():
            raise Remote8889Error("AES not available for 8889 writes")
        self.ensure_session()
        c = self.CMD_WRITE if cmd is None else int(cmd)
        fr = self._exchange_cmd(c, payload=payload.encode("utf-8"))
        if fr.get("reject"):
            # refresh session once
            self.auth(force=True)
            fr = self._exchange_cmd(c, payload=payload.encode("utf-8"))
        if fr.get("reject"):
            raise Remote8889Error(
                f"8889 write rejected payload={payload!r} head={fr.get('magic')}"
            )
        # write_ack or longer success body
        if not (fr.get("write_ack") or (fr.get("len") or 0) > 16):
            # mid=0x06 observed with wrong account — treat as soft fail
            mid = 0
            raw_len = fr.get("len") or 0
            if raw_len >= 12:
                # re-parse mid from last exchange not stored — use write_ack only
                pass
            if raw_len == 16 and not fr.get("write_ack"):
                raise Remote8889Error(
                    f"8889 write unexpected reply len=16 payload={payload!r}"
                )
        return _ok_resp(
            f"8889 write {payload}",
            transport="remote_8889",
            payload=payload,
            cmd=c,
            raw_len=fr.get("len"),
            write_ack=fr.get("write_ack"),
            session=self._session,
        )

    def set_power_mode(self, mode: str) -> dict:
        """
        Power mode via field write if known. Prefer LuCI for mode when possible.
        Mapping (best-effort from WMT field ids; may vary by FW):
          not fully confirmed — raise unless we know opcode.
        """
        m = str(mode or "").strip().lower()
        # Field id for mode not confirmed in capture (only 14=limit, 8=suspend).
        # Keep explicit until capture of mode change lands.
        raise Remote8889Error(
            f"8889 power_mode={m} opcode not confirmed in capture — use LuCI "
            f"set_low/normal/high_power or enable API"
        )

    def set_power_limit(self, watts: int) -> dict:
        """Set power limit watts (field 14). May restart mining like adjust_power_limit."""
        w = int(watts)
        if w < 0 or w > 20000:
            raise ValueError("power_limit out of range")
        return self._write_field(f"14={w}")

    def set_mining(self, suspend: bool) -> dict:
        """Suspend (8=1) or resume (8=0) mining — same as WMT capture."""
        return self._write_field("8=1" if suspend else "8=0")

    def set_pools(self, pools: list[dict]) -> dict:
        """
        Update pools (cmd 0x02). Capture format:
          0,url,user,FAILOVER,,pass|
        Only first pool fully confirmed; extra pools may need more captures.
        """
        if not pools:
            raise ValueError("pools list required")
        p0 = pools[0] or {}
        url = str(p0.get("url") or p0.get("pool") or "").strip()
        user = str(p0.get("user") or p0.get("worker") or "").strip()
        passwd = str(p0.get("pass") or p0.get("password") or "x").strip() or "x"
        if not url or not user:
            raise ValueError("pool url and user required")
        payload = f"0,{url},{user},FAILOVER,,{passwd}|"
        self.ensure_session()
        fr = self._exchange_cmd(self.CMD_POOLS, payload=payload.encode("utf-8"))
        if fr.get("reject"):
            self.auth(force=True)
            fr = self._exchange_cmd(self.CMD_POOLS, payload=payload.encode("utf-8"))
        if fr.get("reject"):
            raise Remote8889Error("8889 set_pools rejected")
        return _ok_resp(
            "8889 pools",
            transport="remote_8889",
            payload=payload,
            raw_len=fr.get("len"),
            write_ack=fr.get("write_ack"),
        )

    def reboot(self) -> dict:
        raise Remote8889Error(
            "8889 reboot opcode not confirmed — use LuCI system/reboot"
        )

    def factory_reset(self) -> dict:
        raise Remote8889Error(
            "8889 factory_reset opcode not confirmed — use TCP API when unlocked"
        )

    def capabilities(self) -> dict:
        return {
            "port": self.port,
            "account": self.account,
            "password_set": bool(self.password),
            "miner_ip": self.miner_ip,
            "session": self._session,
            "probe": True,
            "framing": True,
            "telemetry_parse": True,
            "auth": True,
            "status": True,
            "writes": self.writes_implemented(),
            "write_power_limit": True,
            "write_suspend": True,
            "write_pools": True,
            "write_mode": False,
            "write_reboot": False,
            "keys": "KEY0 auth / KEY1 status+writes (firmware AES-256-ECB)",
            "note": (
                "Live-verified: auth→session, status 0x16, power_limit 14=W "
                "(ack ffff). Prefer LuCI for mode/reboot when available."
            ),
        }


def os_urandom(n: int) -> bytes:
    return os.urandom(n)


# ─── Unified driver ──────────────────────────────────────────────────────────


# Commands LuCI can do without Miner API Switch (btccom path)
_LUCI_CMDS = frozenset(
    {
        "set_low_power",
        "set_normal_power",
        "set_high_power",
        "reboot",
        "restart_btminer",
        "restart_cgminer",
        "update_pools",
        "set_pools",
    }
)
# Commands that need TCP privileged write or 8889 (no LuCI in libbtctools)
_TCP_OR_8889_CMDS = frozenset(
    {
        "power_off",
        "power_on",
        "adjust_power_limit",
        "set_power_pct",
        "factory_reset",
        "set_led",
        "set_target_freq",
    }
)


@dataclass
class WhatsminerDriver:
    """
    High-level control surface for poolheat.

    Write strategy (as operator expects):

      1) Probe Miner API Switch (apiswitch) when possible
      2) If API **on** → TCP v2 / v3 privileged write first
      3) If API **off** (or TCP failed):
           · LuCI for mode / pools / reboot / restart  (libbtctools)
           · :8889 Remote for everything WMT does when API off
             (writes wired but crypto not ready — clear error)
      4) Optional: LuCI open_by_api then one TCP retry

    Never spam get_token when LuCI can do the job.
    """

    host: str
    api_password: str = "admin"
    luci_username: str = "admin"
    luci_password: str | None = None  # default: same as api_password
    port_v2: int = 4028
    port_v3: int = 4433
    port_8889: int = 8889
    # injectables for integration with serve.py (locks, token, etc.)
    tcp_write: Callable[[dict, str], dict] | None = None
    v3_write: Callable[[dict, str], dict] | None = None
    is_online: Callable[[], bool] | None = None
    # Optional: () -> bool | None  True=apiswitch on, False=off, None=unknown
    api_enabled_probe: Callable[[], bool | None] | None = None
    _luci: LuciClient | None = field(default=None, repr=False)
    _remote: Remote8889Client | None = field(default=None, repr=False)
    _api_enabled_cache: bool | None = field(default=None, repr=False)
    _api_enabled_ts: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if self.luci_password is None:
            self.luci_password = self.api_password or "admin"
        self._luci = LuciClient(
            self.host,
            username=self.luci_username,
            password=self.luci_password or "admin",
        )
        # Tools body uses account=super password=super (not LuCI admin).
        # api_password is for :4028 AES; 8889 field-password is protocol "super".
        self._remote = Remote8889Client(
            self.host,
            password="super",
            account="super",
            port=self.port_8889,
            miner_ip=self.host,
        )

    @property
    def luci(self) -> LuciClient:
        assert self._luci is not None
        return self._luci

    @property
    def remote(self) -> Remote8889Client:
        """WhatsMinerTools :8889 client (AES KEY0/1, status + power_limit/suspend)."""
        assert self._remote is not None
        return self._remote

    def _online(self) -> bool:
        if self.is_online:
            try:
                return bool(self.is_online())
            except Exception:
                pass
        try:
            with socket.create_connection((self.host, self.port_v2), timeout=3.0):
                return True
        except OSError:
            return False

    def probe_api_enabled(self, *, force: bool = False, ttl: float = 30.0) -> bool | None:
        """
        True  → apiswitch on (TCP write should work)
        False → apiswitch off
        None  → unknown (probe failed)
        """
        now = time.time()
        if (
            not force
            and self._api_enabled_cache is not None
            and (now - self._api_enabled_ts) < ttl
        ):
            return self._api_enabled_cache
        val: bool | None = None
        if self.api_enabled_probe:
            try:
                val = self.api_enabled_probe()
            except Exception:
                val = None
        if val is None:
            # lightweight v3 get.device.info (no token)
            try:
                val = self._probe_apiswitch_v3()
            except Exception:
                val = None
        self._api_enabled_cache = val
        self._api_enabled_ts = now
        return val

    def _probe_apiswitch_v3(self) -> bool | None:
        """Unauthenticated-ish v3 get.device.info → system.apiswitch."""
        # 4-byte LE length + JSON
        body = json.dumps(
            {"cmd": "get.device.info", "account": "super", "id": "1"},
            separators=(",", ":"),
        ).encode("utf-8")
        pkt = struct.pack("<I", len(body)) + body
        with socket.create_connection((self.host, self.port_v3), timeout=4.0) as s:
            s.settimeout(4.0)
            s.sendall(pkt)
            hdr = s.recv(4)
            if len(hdr) < 4:
                return None
            (n,) = struct.unpack("<I", hdr)
            if n <= 0 or n > 2_000_000:
                return None
            raw = b""
            while len(raw) < n:
                chunk = s.recv(min(65536, n - len(raw)))
                if not chunk:
                    break
                raw += chunk
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None
        # shapes: {code, msg: {system: {apiswitch}}} or nested
        sys_ = None
        if isinstance(msg, dict):
            m = msg.get("msg") or msg.get("Msg") or msg
            if isinstance(m, dict):
                sys_ = m.get("system") or m.get("System")
            if sys_ is None and "system" in msg:
                sys_ = msg.get("system")
        if not isinstance(sys_, dict):
            return None
        sw = str(sys_.get("apiswitch") or sys_.get("MinerApiSwitch") or "0").strip()
        return sw in ("1", "true", "on", "ON")

    def _try_tcp(self, cmd: dict) -> dict:
        errors: list[str] = []
        if self.tcp_write:
            try:
                r = self.tcp_write(cmd, self.api_password)
                if isinstance(r, dict):
                    ok = str(r.get("STATUS") or "").upper() in ("S", "OK", "")
                    code = r.get("Code")
                    msg = str(r.get("Msg") or "").lower()
                    if code == 45 or "can't access write" in msg:
                        errors.append(f"v2: {r.get('Msg')}")
                    elif ok or code in (0, 131, None):
                        r.setdefault("transport", "api_v2")
                        return r
                    else:
                        errors.append(f"v2: {r.get('Msg') or r}")
                else:
                    return {"STATUS": "S", "Msg": str(r), "transport": "api_v2"}
            except Exception as e:
                errors.append(f"v2: {e}")
        if self.v3_write:
            try:
                r = self.v3_write(cmd, self.api_password)
                if isinstance(r, dict):
                    r.setdefault("transport", "api_v3")
                return r  # type: ignore[return-value]
            except Exception as e:
                errors.append(f"v3: {e}")
        raise RuntimeError(" · ".join(errors) if errors else "TCP write failed")

    def _try_8889_cmd(self, cname: str, cmd: dict) -> dict:
        """Map classic cmd → Remote8889Client method (when crypto ready)."""
        r = self.remote
        if cname == "set_low_power":
            return r.set_power_mode("low")
        if cname == "set_normal_power":
            return r.set_power_mode("normal")
        if cname == "set_high_power":
            return r.set_power_mode("high")
        if cname == "power_off":
            return r.set_mining(True)
        if cname == "power_on":
            return r.set_mining(False)
        if cname == "adjust_power_limit":
            return r.set_power_limit(int(cmd.get("power_limit") or 0))
        if cname == "reboot":
            return r.reboot()
        if cname == "factory_reset":
            return r.factory_reset()
        if cname in ("update_pools", "set_pools"):
            pools = cmd.get("pools")
            if not isinstance(pools, list):
                raise ValueError("pools list required")
            return r.set_pools(pools)
        raise Remote8889Error(f"8889 has no mapping for cmd={cname}")

    # ── public write API ────────────────────────────────────────────────────

    def set_power_mode(self, mode: str) -> dict:
        """Power Mode low|normal|high — LuCI when API off; TCP when on."""
        m = str(mode or "").strip().lower()
        cmd = {
            "low": "set_low_power",
            "normal": "set_normal_power",
            "high": "set_high_power",
        }.get(m)
        if not cmd:
            raise ValueError("mode must be low|normal|high")
        return self.write_cmd({"cmd": cmd})

    def set_power_limit(self, watts: int) -> dict:
        return self.write_cmd(
            {"cmd": "adjust_power_limit", "power_limit": str(int(watts))}
        )

    def set_mining(self, suspend: bool) -> dict:
        return self.write_cmd({"cmd": "power_off" if suspend else "power_on"})

    def set_pools(self, pools: list[dict]) -> dict:
        return self.write_cmd({"cmd": "update_pools", "pools": pools})

    def reboot(self) -> dict:
        return self.write_cmd({"cmd": "reboot"})

    def restart_btminer(self) -> dict:
        return self.write_cmd({"cmd": "restart_btminer"})

    def factory_reset(self) -> dict:
        return self.write_cmd({"cmd": "factory_reset"})

    def enable_api_switch(self, enable: bool = True) -> dict:
        out = self.luci.enable_api_switch(enable)
        # invalidate cache so next write re-probes
        self._api_enabled_cache = None
        self._api_enabled_ts = 0.0
        return out

    def open_api_pyasic_cloud(
        self,
        *,
        timeout: float = 25.0,
        port_8889: int | None = None,
    ) -> dict:
        """
        Unlock TCP write API using pyasic's cloud :8889 handshake.

        Flow (same as UpstreamData/pyasic BTMinerRPCAPI.open_api):
          1) POST wmt.pyasic.org/v1/stage1  {"ip": miner}
          2) send hex blob → miner :8889, collect reply hex
          3) POST wmt.pyasic.org/v1/stage2  {ip, stage1_result}
          4) send each stage2 command hex → miner :8889

        Requires outbound HTTPS to wmt.pyasic.org (Cloudflare).
        Miner stays on LAN — cloud never needs to reach the ASIC.

        Returns status dict. Raises RuntimeError on hard failure.
        """
        port = int(port_8889 or self.port_8889 or 8889)
        ip = str(self.host).strip()
        steps: list[dict] = []

        def _http_json(url: str, payload: dict) -> Any:
            raw = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=raw,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "poolheat-whatsminer-driver/open_api",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                    code = int(getattr(resp, "status", 200) or 200)
            except urllib.error.HTTPError as e:
                err_body = (e.read() or b"")[:300].decode("utf-8", "replace")
                raise RuntimeError(
                    f"HTTP {e.code} from {url}: {err_body or e.reason}"
                ) from e
            except Exception as e:
                raise RuntimeError(f"HTTP to {url} failed: {e}") from e
            if not body:
                raise RuntimeError(f"empty body from {url} (HTTP {code})")
            try:
                return json.loads(body.decode("utf-8"))
            except Exception:
                # some deployments return raw hex string JSON-encoded
                text = body.decode("utf-8", "replace").strip()
                if text.startswith('"') and text.endswith('"'):
                    return json.loads(text)
                return text

        def _send_8889(blob: bytes, *, to: float = 5.0) -> bytes:
            with socket.create_connection((ip, port), timeout=to) as s:
                s.settimeout(to)
                s.sendall(blob)
                chunks: list[bytes] = []
                try:
                    while True:
                        part = s.recv(8192)
                        if not part:
                            break
                        chunks.append(part)
                        if sum(len(c) for c in chunks) >= 4096:
                            break
                except (socket.timeout, TimeoutError):
                    pass
                return b"".join(chunks)

        # Stage 1
        try:
            stage1 = _http_json(PYASIC_WMT_STAGE1, {"ip": ip})
            steps.append({"step": "stage1_http", "ok": True, "type": type(stage1).__name__})
        except Exception as e:
            steps.append({"step": "stage1_http", "ok": False, "error": str(e)})
            raise RuntimeError(
                f"pyasic cloud stage1 failed: {e}. "
                "wmt.pyasic.org may be down (Cloudflare 522) or blocked."
            ) from e

        # stage1 is hex string or {"payload": "hex"} / list
        if isinstance(stage1, dict):
            stage1_hex = (
                stage1.get("payload")
                or stage1.get("data")
                or stage1.get("command")
                or stage1.get("hex")
            )
            if stage1_hex is None and len(stage1) == 1:
                stage1_hex = next(iter(stage1.values()))
        else:
            stage1_hex = stage1
        if not isinstance(stage1_hex, str) or len(stage1_hex) < 8:
            raise RuntimeError(f"stage1 unexpected payload: {stage1!r}"[:200])

        try:
            stage1_blob = binascii.unhexlify(stage1_hex.strip())
        except Exception as e:
            raise RuntimeError(f"stage1 not hex: {e}") from e

        try:
            stage1_res = _send_8889(stage1_blob, to=8.0)
            steps.append(
                {
                    "step": "stage1_8889",
                    "ok": True,
                    "sent": len(stage1_blob),
                    "recv": len(stage1_res),
                    "recv_head": stage1_res[:16].hex() if stage1_res else "",
                }
            )
        except Exception as e:
            steps.append({"step": "stage1_8889", "ok": False, "error": str(e)})
            raise RuntimeError(f"stage1 send to :8889 failed: {e}") from e

        # Stage 2
        try:
            stage2 = _http_json(
                PYASIC_WMT_STAGE2,
                {
                    "ip": ip,
                    "stage1_result": binascii.hexlify(stage1_res).decode("ascii"),
                },
            )
            steps.append(
                {
                    "step": "stage2_http",
                    "ok": True,
                    "type": type(stage2).__name__,
                    "n": len(stage2) if isinstance(stage2, list) else 1,
                }
            )
        except Exception as e:
            steps.append({"step": "stage2_http", "ok": False, "error": str(e)})
            raise RuntimeError(f"pyasic cloud stage2 failed: {e}") from e

        commands: list[str] = []
        if isinstance(stage2, list):
            commands = [str(c) for c in stage2]
        elif isinstance(stage2, dict):
            for k in ("commands", "payload", "data"):
                if isinstance(stage2.get(k), list):
                    commands = [str(c) for c in stage2[k]]
                    break
            if not commands and isinstance(stage2.get("command"), str):
                commands = [stage2["command"]]
        elif isinstance(stage2, str):
            commands = [stage2]
        if not commands:
            raise RuntimeError(f"stage2 empty/unexpected: {stage2!r}"[:200])

        sent_ok = 0
        for i, cmd_hex in enumerate(commands):
            try:
                blob = binascii.unhexlify(str(cmd_hex).strip())
                _ = _send_8889(blob, to=3.0)
                sent_ok += 1
                steps.append(
                    {"step": f"stage2_8889[{i}]", "ok": True, "sent": len(blob)}
                )
            except Exception as e:
                steps.append(
                    {"step": f"stage2_8889[{i}]", "ok": False, "error": str(e)}
                )

        self._api_enabled_cache = None
        self._api_enabled_ts = 0.0
        return {
            "STATUS": "S" if sent_ok else "E",
            "Code": 131 if sent_ok else 45,
            "Msg": f"pyasic open_api: {sent_ok}/{len(commands)} cmds sent via :8889",
            "transport": "pyasic_cloud_8889",
            "steps": steps,
            "commands": len(commands),
            "sent_ok": sent_ok,
        }

    def write_cmd(self, cmd: dict) -> dict:
        """
        Map classic privileged cmd → best transport.

        Strategy:
          · API on  → TCP first, then LuCI (if applicable), then 8889
          · API off / unknown → LuCI first (safe, no tokens), then 8889, then
            TCP (only if still needed — may burn get_token)
        """
        cname = str(cmd.get("cmd") or "").strip()
        if not cname:
            raise ValueError("cmd required")
        # normalize set_power_mode helper path
        if cname not in _LUCI_CMDS and cname not in _TCP_OR_8889_CMDS:
            # allow unknown cmds through TCP path only
            pass

        api_on = self.probe_api_enabled()
        errors: list[str] = []

        def _luci_path() -> dict | None:
            if cname == "set_low_power":
                return self.luci.set_power_mode("low")
            if cname == "set_normal_power":
                return self.luci.set_power_mode("normal")
            if cname == "set_high_power":
                return self.luci.set_power_mode("high")
            if cname == "reboot":
                return self.luci.reboot()
            if cname in ("restart_btminer", "restart_cgminer"):
                return self.luci.restart_btminer()
            if cname in ("update_pools", "set_pools"):
                pools = cmd.get("pools")
                if not isinstance(pools, list):
                    raise ValueError("update_pools requires pools=[{url,user,pass},…]")
                return self.luci.set_pools(pools)
            return None

        def _tcp_path() -> dict | None:
            if not (self.tcp_write or self.v3_write):
                return None
            return self._try_tcp(cmd)

        def _8889_path() -> dict | None:
            if cname in (
                "set_low_power",
                "set_normal_power",
                "set_high_power",
                "power_off",
                "power_on",
                "adjust_power_limit",
                "reboot",
                "factory_reset",
                "update_pools",
                "set_pools",
            ):
                return self._try_8889_cmd(cname, cmd)
            return None

        # ── order by API state ──────────────────────────────────────────────
        r8889_ready = self.remote.writes_implemented()
        if api_on is True:
            order = ("tcp", "luci", "8889")
        else:
            # API off / unknown: never burn get_token first for LuCI-capable ops
            if cname in _LUCI_CMDS:
                order = ("luci", "8889", "tcp")
            elif r8889_ready:
                # Tools-parity path when crypto lands
                order = ("8889", "tcp", "luci")
            else:
                # suspend/limit/factory without 8889 crypto:
                # one open_by_api attempt then TCP — no blind get_token spam
                order = ("open_api_tcp", "8889")

        for step in order:
            try:
                if step == "luci":
                    r = _luci_path()
                    if r is not None:
                        return r
                elif step == "tcp":
                    # skip TCP entirely when we *know* API is off (saves tokens)
                    if api_on is False and cname not in _LUCI_CMDS:
                        errors.append("tcp: skipped (apiswitch=0)")
                        continue
                    r = _tcp_path()
                    if r is not None:
                        return r
                elif step == "8889":
                    if not r8889_ready:
                        errors.append(
                            "8889: AES unavailable "
                            f"(install pycryptodome; cmd={cname})"
                        )
                        continue
                    r = _8889_path()
                    if r is not None:
                        return r
                elif step == "open_api_tcp":
                    # Best-effort unlock then TCP (WMT «Enable API» analogue)
                    try:
                        self.enable_api_switch(True)
                        self._api_enabled_cache = None
                    except Exception as e_en:
                        errors.append(f"open_by_api: {e_en}")
                    r = _tcp_path()
                    if r is not None:
                        if isinstance(r, dict):
                            r["auto_enabled_write"] = True
                        return r
            except Exception as e:
                errors.append(f"{step}: {e}")
                if step in ("tcp", "open_api_tcp") and (
                    "access write" in str(e).lower() or "code 45" in str(e).lower()
                ):
                    try:
                        self.enable_api_switch(True)
                        r2 = _tcp_path()
                        if isinstance(r2, dict):
                            r2["auto_enabled_write"] = True
                            r2.setdefault("transport", "api_v2")
                        if r2 is not None:
                            return r2
                    except Exception as e2:
                        errors.append(f"auto_enable+tcp: {e2}")

        raise RuntimeError(
            " · ".join(errors)
            if errors
            else f"write failed: {cname} (api_on={api_on})"
        )


# ─── CLI smoke ───────────────────────────────────────────────────────────────


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Whatsminer LuCI/TCP/8889 driver smoke")
    ap.add_argument("--host", default="192.168.1.10")
    ap.add_argument("--password", default="admin")
    ap.add_argument(
        "action",
        choices=[
            "login",
            "mode",
            "reboot-dry",
            "pools-read",
            "api",
            "8889-probe",
            "8889-caps",
            "8889-status",
            "8889-limit",
            "strategy",
            "open-api-cloud",
        ],
    )
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--watts", type=int, default=0, help="for 8889-limit")
    args = ap.parse_args()
    d = WhatsminerDriver(args.host, api_password=args.password, luci_password=args.password)
    if args.action == "login":
        d.luci.login()
        print("login ok", d.luci._scheme, "program", d.luci.detect_program())
    elif args.action == "mode":
        print(d.set_power_mode(args.mode))
    elif args.action == "reboot-dry":
        d.luci.login()
        st, html = d.luci.request("/cgi-bin/luci/admin/system/reboot")
        print("reboot page", st, "token", "token" in html or "Token" in html)
    elif args.action == "pools-read":
        d.luci.login()
        prog = d.luci.detect_program()
        st, html = d.luci.request(f"/cgi-bin/luci/admin/network/{prog}")
        print("pools page", st, "len", len(html), "pool1url" in html)
    elif args.action == "api":
        print("apiswitch_on", d.probe_api_enabled(force=True))
    elif args.action == "8889-probe":
        print(d.remote.probe())
    elif args.action == "8889-caps":
        print(json.dumps(d.remote.capabilities(), indent=2))
    elif args.action == "8889-status":
        st = d.remote.poll_status()
        mi = (st.get("ini") or {}).get("MinerInfo") or {}
        print("session", d.remote._session)
        print("MinerType", mi.get("MinerType"), "PowerLimitSet", mi.get("PowerLimitSet"))
        print("PowerMode", mi.get("PowerMode"), "MinerApiSwitch", mi.get("MinerApiSwitch"))
        print("raw_len", st.get("raw_len"))
    elif args.action == "8889-limit":
        if not args.watts:
            raise SystemExit("--watts required")
        print(d.remote.set_power_limit(args.watts))
        st = d.remote.poll_status()
        mi = (st.get("ini") or {}).get("MinerInfo") or {}
        print("PowerLimitSet now", mi.get("PowerLimitSet"))
    elif args.action == "strategy":
        api = d.probe_api_enabled(force=True)
        rready = d.remote.writes_implemented()
        print("api_on", api, "8889_writes", rready)
        print("8889", d.remote.capabilities())
        for c in (
            "set_normal_power",
            "power_off",
            "adjust_power_limit",
            "reboot",
            "factory_reset",
        ):
            if api is True:
                order = ("tcp", "luci", "8889")
            elif c in _LUCI_CMDS:
                order = ("luci", "8889", "tcp")
            elif rready:
                order = ("8889", "tcp", "luci")
            else:
                order = ("open_api_tcp", "8889")
            print(f"  {c:22} → {' → '.join(order)}")
    elif args.action == "open-api-cloud":
        try:
            print(json.dumps(d.open_api_pyasic_cloud(), indent=2, default=str))
        except Exception as e:
            print("FAIL", e)
            raise SystemExit(1)


if __name__ == "__main__":
    _cli()
