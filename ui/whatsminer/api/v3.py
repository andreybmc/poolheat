"""
Whatsminer API V3 (TCP 4433) — official apidoc 3.0.x + Python demo.

Protocol:
  - Port 4433, length-prefixed JSON: [u32 LE length][utf-8 body]
  - Read:  {"cmd":"get.…","param":…}
  - Write: {"cmd":"set.…","ts":…,"token":…,"account":…,"param":…}
  - token = base64(sha256(cmd+password+salt+ts))[:8]
  - salt from get.device.info → msg.salt
  - Sensitive params (pools, password) AES-256-ECB encrypted with full sha256 key

Online docs: https://apidoc.whatsminer.com/
Firmware: ≈ 20240501+
Demo: docs/official_python_demo/
"""

from __future__ import annotations

import json
import logging
import struct
import time
from pathlib import Path
from typing import Any, BinaryIO

from ..protocol import crypto
from ..errors import CommandError, ProtocolError
from ..models import MinerStatus
from ..protocol.transport import (
    recv_exact,
    tcp_len_prefixed,
    tcp_len_prefixed_session,
)

log = logging.getLogger(__name__)

DEFAULT_PORT = 4433

# ── command catalog (apidoc 3.0.0–3.0.3) ─────────────────────────────────────

V3_GET_CMDS: frozenset[str] = frozenset(
    {
        "get.device.info",
        "get.device.custom_data",
        "get.fan.setting",
        "get.log.download",
        "get.miner.history",
        "get.miner.report",
        "get.miner.setting",
        "get.miner.status",
        "get.system.setting",
    }
)

V3_SET_CMDS: frozenset[str] = frozenset(
    {
        "set.device.custom_data",
        "set.fan.poweroff_cool",
        "set.fan.temp_offset",
        "set.fan.zero_speed",
        "set.log.upload",
        "set.miner.cointype",
        "set.miner.fast_hash",
        "set.miner.fastboot",
        "set.miner.heat_mode",
        "set.miner.pools",
        "set.miner.power",
        "set.miner.power_limit",
        "set.miner.power_mode",
        "set.miner.power_percent",
        "set.miner.report",
        "set.miner.restore_setting",
        "set.miner.service",
        "set.miner.target_freq",
        "set.miner.upfreq_speed",
        "set.system.factory_reset",
        "set.system.hostname",
        "set.system.led",
        "set.system.net_config",
        "set.system.ntp_server",
        "set.system.reboot",
        "set.system.time_randomized",
        "set.system.timezone",
        "set.system.update_firmware",
        "set.system.webpools",
        "set.user.change_passwd",
        "set.user.permission",
    }
)

# params that must be AES-encrypted (official demo / apidoc)
V3_ENCRYPTED_PARAMS: frozenset[str] = frozenset(
    {
        "set.miner.pools",
        "set.user.change_passwd",
    }
)


class WhatsminerV3:
    """Client for API V3 used on newer firmwares (port 4433)."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        account: str = "super",
        password: str = "super",
        *,
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.account = account
        self.password = password
        self.timeout = timeout
        self.salt: str | None = None

    # ── transport ────────────────────────────────────────────────────────────

    def _send(self, payload: dict[str, Any], *, max_response: int = 2 * 1024 * 1024) -> dict[str, Any]:
        message = json.dumps(payload, separators=(",", ":"))
        raw = tcp_len_prefixed(
            self.host,
            self.port,
            message,
            timeout=self.timeout,
            max_response=max_response,
        )
        try:
            resp = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ProtocolError(f"{self.host}: invalid V3 JSON: {raw[:200]!r}") from e
        code = resp.get("code")
        if code not in (0, None) and code != "0":
            raise CommandError(
                str(resp.get("msg", resp)),
                code=int(code) if str(code).isdigit() else None,
                raw=resp,
            )
        return resp

    def _token(self, command: str, ts: int) -> str:
        if not self.salt:
            raise ProtocolError("salt not set; call ensure_salt() first")
        src = f"{command}{self.password}{self.salt}{ts}"
        return crypto.b64(crypto.sha256_digest(src))[:8]

    def _encrypt_param(self, param: Any, command: str, ts: int) -> str:
        if isinstance(param, str):
            param_str = param
        else:
            param_str = json.dumps(param, separators=(",", ":"))
        src = f"{command}{self.password}{self.salt}{ts}"
        key = crypto.sha256_digest(src)
        ct = crypto.aes256_ecb_encrypt(crypto.pad_pkcs7(param_str), key)
        return crypto.b64(ct)

    def ensure_salt(self) -> str:
        if self.salt:
            return self.salt
        resp = self.get("get.device.info")
        msg = resp.get("msg") or {}
        salt = msg.get("salt") if isinstance(msg, dict) else None
        if not salt:
            raise ProtocolError(f"{self.host}: no salt in get.device.info: {resp}")
        self.salt = str(salt)
        return self.salt

    def get(self, cmd: str, param: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"cmd": cmd, "param": param}
        return self._send(payload)

    def set(
        self,
        cmd: str,
        param: Any = None,
        *,
        encrypt_param: bool | None = None,
    ) -> dict[str, Any]:
        self.ensure_salt()
        ts = int(time.time())
        if encrypt_param is None:
            encrypt_param = cmd in V3_ENCRYPTED_PARAMS
        payload: dict[str, Any] = {
            "cmd": cmd,
            "ts": ts,
            "token": self._token(cmd, ts),
            "account": self.account,
        }
        if encrypt_param and param is not None:
            payload["param"] = self._encrypt_param(param, cmd, ts)
        else:
            payload["param"] = param
        return self._send(payload)

    # ── Device ───────────────────────────────────────────────────────────────

    def device_info(self, param: str | None = None) -> dict[str, Any]:
        """
        get.device.info — salt, network, miner, system, power, errors.
        Optional param filter: miner|system|power|network|salt|error …
        """
        return self.get("get.device.info", param)

    def get_device_custom_data(self) -> dict[str, Any]:
        return self.get("get.device.custom_data")

    def set_device_custom_data(self, key: str, value: str) -> dict[str, Any]:
        """key: CustomerSn | msg0..msg9."""
        return self.set(
            "set.device.custom_data",
            {"key": key, "value": value},
        )

    # ── Fan ──────────────────────────────────────────────────────────────────

    def fan_setting(self) -> dict[str, Any]:
        return self.get("get.fan.setting")

    def set_fan_poweroff_cool(self, enable: bool | int) -> dict[str, Any]:
        return self.set("set.fan.poweroff_cool", 1 if enable else 0)

    def set_fan_temp_offset(self, offset: int) -> dict[str, Any]:
        """Negative integer or 0 (°C), air-cooled only."""
        return self.set("set.fan.temp_offset", int(offset))

    def set_fan_zero_speed(self, enable: bool | int) -> dict[str, Any]:
        return self.set("set.fan.zero_speed", 1 if enable else 0)

    # ── Log ──────────────────────────────────────────────────────────────────

    def download_logs(
        self,
        dest: str | Path | None = None,
        *,
        timeout: float | None = 120.0,
    ) -> bytes:
        """
        get.log.download — packages logs as .tgz and streams binary after JSON.
        """
        old = self.timeout
        if timeout is not None:
            self.timeout = timeout
        try:
            payload = {"cmd": "get.log.download", "param": None}
            message = json.dumps(payload, separators=(",", ":"))
            sock, raw = tcp_len_prefixed_session(
                self.host,
                self.port,
                message,
                timeout=self.timeout,
            )
            try:
                resp = json.loads(raw.decode("utf-8"))
                code = resp.get("code")
                if code not in (0, None, "0"):
                    raise CommandError(
                        str(resp.get("msg", resp)),
                        code=int(code) if str(code).isdigit() else None,
                        raw=resp,
                    )
                # remaining stream = archive (length may be in msg)
                msg = resp.get("msg") if isinstance(resp.get("msg"), dict) else {}
                size = 0
                for k in ("size", "length", "logfilelen", "file_size"):
                    if k in (msg or {}):
                        try:
                            size = int(msg[k])
                            break
                        except (TypeError, ValueError):
                            pass
                if size > 0:
                    blob = recv_exact(sock, size)
                else:
                    # read until peer closes
                    chunks: list[bytes] = []
                    while True:
                        try:
                            c = sock.recv(65536)
                        except OSError:
                            break
                        if not c:
                            break
                        chunks.append(c)
                    blob = b"".join(chunks)
                if dest is not None:
                    Path(dest).write_bytes(blob)
                return blob
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        finally:
            self.timeout = old

    def set_log_upload(
        self,
        server_ip: str,
        server_port: str | int,
        proto: str = "udp",
    ) -> dict[str, Any]:
        return self.set(
            "set.log.upload",
            {"ip": server_ip, "port": str(server_port), "proto": proto},
        )

    # ── Miner (read) ─────────────────────────────────────────────────────────

    def miner_status(self, param: str | None = None) -> dict[str, Any]:
        """
        get.miner.status — filter: pools|summary|edevs or combinations with '+'.
        Hashrate in TH/s.
        """
        return self.get("get.miner.status", param)

    def miner_setting(self) -> dict[str, Any]:
        return self.get("get.miner.setting")

    def miner_history(self, begin: int | str, end: int | str) -> dict[str, Any]:
        return self.get(
            "get.miner.history",
            {"begin": str(begin), "end": str(end)},
        )

    def miner_report(self) -> dict[str, Any]:
        """Some firmwares expose get.miner.report (auto-report config/status)."""
        return self.get("get.miner.report")

    # ── Miner (write) ────────────────────────────────────────────────────────

    def set_pools(
        self,
        pools: list[dict[str, str]] | None = None,
        *,
        pool1: str = "",
        worker1: str = "",
        passwd1: str = "x",
        pool2: str = "",
        worker2: str = "",
        passwd2: str = "x",
        pool3: str = "",
        worker3: str = "",
        passwd3: str = "x",
    ) -> dict[str, Any]:
        """
        set.miner.pools — up to 3 pools; param is AES-encrypted.
        Pass either `pools=[{pool,worker,passwd},…]` or pool1/worker1/…
        """
        if pools is None:
            pools = [
                {"pool": pool1, "worker": worker1, "passwd": passwd1},
                {"pool": pool2, "worker": worker2, "passwd": passwd2},
                {"pool": pool3, "worker": worker3, "passwd": passwd3},
            ]
        return self.set("set.miner.pools", pools, encrypt_param=True)

    def set_service(self, action: str) -> dict[str, Any]:
        """restart | start | stop | enable | disable."""
        return self.set("set.miner.service", action)

    def set_cointype(self, cointype: str) -> dict[str, Any]:
        """BTC | BCH | BSV | DCR | HC | DGB | SHA256 …"""
        return self.set("set.miner.cointype", {"cointype": cointype})

    def set_fastboot(self, enable: bool) -> dict[str, Any]:
        return self.set("set.miner.fastboot", "enable" if enable else "disable")

    def set_fast_hash(self, enable: bool | int = True) -> dict[str, Any]:
        """apidoc 3.0.2+ — reduce hash-rate loss during startup."""
        return self.set("set.miner.fast_hash", 1 if enable else 0)

    def set_heat_mode(self, mode: str) -> dict[str, Any]:
        """heating | normal | anti-freezing (liquid-cooled)."""
        return self.set("set.miner.heat_mode", mode)

    def set_power(self, watts: int | str) -> dict[str, Any]:
        return self.set("set.miner.power", int(watts) if str(watts).isdigit() else watts)

    def set_power_limit(self, watts: int | str) -> dict[str, Any]:
        return self.set(
            "set.miner.power_limit",
            int(watts) if str(watts).isdigit() else watts,
        )

    def set_power_mode(self, mode: str) -> dict[str, Any]:
        """low | normal | high."""
        return self.set("set.miner.power_mode", mode)

    def set_power_percent(
        self,
        percent: int | str,
        mode: str = "normal",
    ) -> dict[str, Any]:
        """mode: fast | normal."""
        return self.set(
            "set.miner.power_percent",
            {"percent": int(percent) if str(percent).isdigit() else percent, "mode": mode},
        )

    def set_report(self, gap_sec: int) -> dict[str, Any]:
        """Auto-report interval seconds; 0 disables (max ~285)."""
        return self.set("set.miner.report", {"gap": int(gap_sec)})

    def restore_setting(self) -> dict[str, Any]:
        return self.set("set.miner.restore_setting")

    def set_target_freq(self, percent: int | str) -> dict[str, Any]:
        return self.set(
            "set.miner.target_freq",
            int(percent) if str(percent).isdigit() else percent,
        )

    def set_upfreq_speed(self, speed: int | str) -> dict[str, Any]:
        """0 = normal … 10 = fastest."""
        return self.set(
            "set.miner.upfreq_speed",
            int(speed) if str(speed).isdigit() else speed,
        )

    # ── System ───────────────────────────────────────────────────────────────

    def system_setting(self) -> dict[str, Any]:
        return self.get("get.system.setting")

    def set_hostname(self, hostname: str) -> dict[str, Any]:
        return self.set("set.system.hostname", {"hostname": hostname})

    def set_led_auto(self) -> dict[str, Any]:
        return self.set("set.system.led", "auto")

    def set_led_manual(
        self,
        patterns: list[dict[str, Any]] | None = None,
        *,
        color: str = "red",
        period: int = 1000,
        duration: int = 500,
        start: int = 0,
    ) -> dict[str, Any]:
        if patterns is None:
            patterns = [
                {
                    "color": color,
                    "period": period,
                    "duration": duration,
                    "start": start,
                }
            ]
        return self.set("set.system.led", patterns)

    def net_config_dhcp(self) -> dict[str, Any]:
        return self.set("set.system.net_config", "dhcp")

    def net_config_static(
        self,
        ip: str,
        netmask: str,
        gateway: str,
        dns: str,
        **extra: Any,
    ) -> dict[str, Any]:
        param: dict[str, Any] = {
            "ip": ip,
            "netmask": netmask,
            "gateway": gateway,
            "dns": dns,
        }
        param.update(extra)
        return self.set("set.system.net_config", param)

    def set_ntp_server(self, servers: str | list[str]) -> dict[str, Any]:
        """Comma-separated string or list of NTP hosts."""
        if isinstance(servers, list):
            servers = ",".join(servers)
        return self.set("set.system.ntp_server", servers)

    def factory_reset(self) -> dict[str, Any]:
        return self.set("set.system.factory_reset")

    def reboot(self) -> dict[str, Any]:
        return self.set("set.system.reboot")

    def set_time_randomized(self, start: int = 10, stop: int = 10) -> dict[str, Any]:
        """Random delay (seconds) before network start / mining stop."""
        return self.set(
            "set.system.time_randomized",
            {"start": int(start), "stop": int(stop)},
        )

    def set_timezone(self, timezone: str, zonename: str) -> dict[str, Any]:
        return self.set(
            "set.system.timezone",
            {"timezone": timezone, "zonename": zonename},
        )

    def set_webpools(self, enable: bool) -> dict[str, Any]:
        return self.set("set.system.webpools", "enable" if enable else "disable")

    def update_firmware(
        self,
        firmware: str | Path | bytes | BinaryIO,
        *,
        timeout: float | None = 300.0,
    ) -> dict[str, Any]:
        """
        set.system.update_firmware — multi-phase like V2:
        JSON ready → binary transfer (implementation follows apidoc flow).
        """
        if isinstance(firmware, (str, Path)):
            blob = Path(firmware).read_bytes()
        elif isinstance(firmware, bytes):
            blob = firmware
        else:
            data = firmware.read()
            blob = data.encode("utf-8") if isinstance(data, str) else data

        self.ensure_salt()
        old = self.timeout
        if timeout is not None:
            self.timeout = timeout
        try:
            cmd = "set.system.update_firmware"
            ts = int(time.time())
            payload = {
                "cmd": cmd,
                "ts": ts,
                "token": self._token(cmd, ts),
                "account": self.account,
                "param": None,
            }
            message = json.dumps(payload, separators=(",", ":"))
            sock, raw = tcp_len_prefixed_session(
                self.host,
                self.port,
                message,
                timeout=self.timeout,
            )
            try:
                resp = json.loads(raw.decode("utf-8"))
                code = resp.get("code")
                if code not in (0, None, "0"):
                    raise CommandError(
                        str(resp.get("msg", resp)),
                        code=int(code) if str(code).isdigit() else None,
                        raw=resp,
                    )
                # send size + data (same LE framing as V2 for many firmwares)
                sock.sendall(struct.pack("<I", len(blob)) + blob)
                try:
                    sock.settimeout(min(60.0, timeout or 60.0))
                    # optional final length-prefixed ack
                    hdr = sock.recv(4)
                    if len(hdr) == 4:
                        (ln,) = struct.unpack("<I", hdr)
                        if 0 < ln < 1_000_000:
                            tail = recv_exact(sock, ln)
                            try:
                                return json.loads(tail.decode("utf-8"))
                            except Exception:
                                return {
                                    "ok": True,
                                    "bytes": len(blob),
                                    "ack": resp,
                                    "tail": tail[:200],
                                }
                except OSError:
                    pass
                return {"ok": True, "bytes": len(blob), "ack": resp}
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        finally:
            self.timeout = old

    # ── User ─────────────────────────────────────────────────────────────────

    def change_password(self, account: str, old: str, new: str) -> dict[str, Any]:
        param = {"account": account, "old": old, "new": new}
        resp = self.set("set.user.change_passwd", param, encrypt_param=True)
        if account == self.account:
            self.password = new
            self.salt = None
        return resp

    def set_user_permission(self, user: str, permission: str | list[str]) -> dict[str, Any]:
        """
        super-only. permission = comma-separated cmds or list.
        e.g. "set.system.led,set.miner.pools"
        """
        if isinstance(permission, list):
            permission = ",".join(permission)
        return self.set(
            "set.user.permission",
            {"user": user, "permission": permission},
        )

    # ── snapshot / catalog ───────────────────────────────────────────────────

    def snapshot(self) -> MinerStatus:
        info = self.device_info()
        msg = info.get("msg") if isinstance(info.get("msg"), dict) else {}
        status_raw: dict[str, Any] = {}
        try:
            st = self.miner_status()
            status_raw = st.get("msg") if isinstance(st.get("msg"), dict) else st
        except Exception as e:
            log.debug("get.miner.status failed on %s: %s", self.host, e)

        return MinerStatus(
            ip=self.host,
            api="v3",
            miner_type=str(msg.get("miner_type") or msg.get("type") or ""),
            firmware=str(msg.get("fw_ver") or msg.get("firmware") or ""),
            platform=str(msg.get("platform") or ""),
            mac=str(msg.get("mac") or ""),
            hostname=str(msg.get("hostname") or ""),
            raw={"device_info": info, "status": status_raw},
        )

    @classmethod
    def list_commands(cls) -> dict[str, list[str]]:
        return {
            "get": sorted(V3_GET_CMDS),
            "set": sorted(V3_SET_CMDS),
            "encrypted_param": sorted(V3_ENCRYPTED_PARAMS),
        }
