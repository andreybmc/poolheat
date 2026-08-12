"""
Whatsminer API V2 (TCP 4028) — official User's Manual V2.2.2.

Protocol:
  - Port 4028, one-shot TCP JSON (plaintext reads; AES-256-ECB + token writes)
  - Write requires Miner API Switch enabled + non-default admin password
  - Token via get_token (md5_crypt salt/newsalt), ~30 min TTL

Reference:
  https://apidoc.whatsminer.com/  (V3 online; V2 covered by PDF manual)
  docs/API_Manual_V2.2.2.txt
  docs/whatsminer-api-manual.pdf
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from ..protocol import crypto
from ..errors import AuthError, CommandError, ProtocolError
from ..models import BoardInfo, ErrorCode, MinerStatus, PoolInfo
from ..protocol.transport import (
    recv_exact,
    recv_json_object,
    send_u32le_blob,
    tcp_json_oneshot,
    tcp_session,
)

log = logging.getLogger(__name__)

DEFAULT_PORT = 4028
TOKEN_TTL_S = 25 * 60  # refresh before typical 30 min expiry

# ── command catalog (manual V2.2.2) ──────────────────────────────────────────

V2_READ_CMDS: frozenset[str] = frozenset(
    {
        "summary",
        "pools",
        "edevs",
        "devs",
        "devdetails",
        "get_psu",
        "get_version",
        "get_token",
        "status",
        "get_miner_info",
        "get_error_code",
        "get_customer_msg",
    }
)

V2_WRITE_CMDS: frozenset[str] = frozenset(
    {
        "update_pools",
        "restart_btminer",
        "power_off",
        "power_on",
        "set_led",
        "set_low_power",
        "set_normal_power",
        "set_high_power",
        "update_firmware",
        "reboot",
        "factory_reset",
        "update_pwd",
        "net_config",
        "download_logs",
        "set_target_freq",
        "enable_btminer_fast_boot",
        "disable_btminer_fast_boot",
        "enable_web_pools",
        "disable_web_pools",
        "set_hostname",
        "set_zone",
        "load_log",
        "set_power_pct",
        "set_power_pct_v2",
        "set_power",
        "restore_power_pct",
        "set_temp_offset",
        "adjust_power_limit",
        "adjust_upfreq_speed",
        "set_poweroff_cool",
        "set_fan_zero_speed",
        "set_heat_mode",
        "disable_btminer_init",
        "enable_btminer_init",
        "set_customer_msg",
        "set_fast_mining",
        "set_fast_hash",
    }
)


class WhatsminerV2:
    """Client for classic Whatsminer JSON API on port 4028 (API V2.x)."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str = "admin",
        *,
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sign: str | None = None
        self._aeskey: bytes | None = None
        self._token_at: datetime | None = None

    # ── transport ────────────────────────────────────────────────────────────

    def _send_raw(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        raw = tcp_json_oneshot(self.host, self.port, payload, timeout=self.timeout)
        if not raw:
            raise ProtocolError(f"{self.host}: empty response")
        text = raw.decode("utf-8", errors="replace").strip().rstrip("\0")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            end = text.rfind("}")
            if end >= 0:
                return json.loads(text[: end + 1])
            raise ProtocolError(f"{self.host}: invalid JSON: {text[:200]!r}") from None

    def read(self, cmd: str, **params: Any) -> dict[str, Any]:
        """Plaintext read command (summary, pools, edevs, …)."""
        body: dict[str, Any] = {"cmd": cmd}
        body.update(params)
        return self._send_raw(body)

    def multi_read(self, *cmds: str) -> dict[str, Any]:
        """Join read cmds with '+' (e.g. summary+pools)."""
        return self.read("+".join(cmds))

    # ── token / write ────────────────────────────────────────────────────────

    def refresh_token(self) -> None:
        resp = self.read("get_token")
        msg = resp.get("Msg")
        if msg == "over max connect" or resp.get("Code") == 136:
            raise AuthError(f"{self.host}: token over max connections")
        if not isinstance(msg, dict) or "salt" not in msg:
            raise AuthError(f"{self.host}: bad get_token response: {resp}")

        salt = msg["salt"]
        newsalt = msg["newsalt"]
        time_s = str(msg["time"])
        time_tail = time_s[-4:]

        key = crypto.md5_crypt_hash(self.password, salt)
        sign = crypto.md5_crypt_hash(key + time_tail, newsalt)
        self._sign = sign
        self._aeskey = crypto.sha256_hex_key(key)
        self._token_at = datetime.now(timezone.utc)
        log.debug("%s token refreshed sign=%s…", self.host, sign[:4])

    def _ensure_token(self) -> None:
        if self._sign is None or self._aeskey is None or self._token_at is None:
            self.refresh_token()
            return
        age = (datetime.now(timezone.utc) - self._token_at).total_seconds()
        if age > TOKEN_TTL_S:
            self.refresh_token()

    def _build_enc_packet(self, cmd: str, **params: Any) -> dict[str, Any]:
        self._ensure_token()
        assert self._sign is not None and self._aeskey is not None
        plain: dict[str, Any] = {"cmd": cmd, "token": self._sign}
        plain.update(params)
        api_cmd = json.dumps(plain, separators=(",", ":"))
        enc = crypto.aes256_ecb_encrypt(crypto.pad16_null(api_cmd), self._aeskey)
        return {"enc": 1, "data": crypto.b64(enc)}

    def _decrypt_response(self, resp: dict[str, Any]) -> dict[str, Any]:
        assert self._aeskey is not None
        if isinstance(resp, dict) and "enc" in resp and "STATUS" not in resp:
            try:
                enc_field = resp["enc"]
                if isinstance(enc_field, str) and enc_field not in ("1", "0"):
                    ct = crypto.b64d(enc_field)
                elif "data" in resp:
                    ct = crypto.b64d(resp["data"])
                else:
                    return resp
                pt = crypto.unpad_null(crypto.aes256_ecb_decrypt(ct, self._aeskey)).decode(
                    "utf-8"
                )
                return json.loads(pt)
            except Exception as e:
                raise ProtocolError(f"{self.host}: decrypt failed: {e}; raw={resp}") from e
        return resp

    def write(self, cmd: str, **params: Any) -> dict[str, Any]:
        """Encrypted write command. Requires API write enabled + non-default password."""
        packet = self._build_enc_packet(cmd, **params)
        resp = self._send_raw(packet)
        resp = self._decrypt_response(resp)
        if resp.get("STATUS") == "E" or resp.get("Code") in (45, 132, 135, 136, 137):
            raise CommandError(
                str(resp.get("Msg", resp)),
                code=resp.get("Code"),
                raw=resp,
            )
        return resp

    def _write_stream_json(self, cmd: str, **params: Any) -> tuple[Any, dict[str, Any]]:
        """
        Send encrypted write and keep the TCP socket open for follow-up binary.
        Returns (socket, first JSON response). Caller must close the socket.
        """
        packet = self._build_enc_packet(cmd, **params)
        data = json.dumps(packet, separators=(",", ":")).encode("utf-8")
        sock = tcp_session(self.host, self.port, timeout=max(self.timeout, 60.0))
        try:
            sock.sendall(data)
            try:
                sock.shutdown(__import__("socket").SHUT_WR)
            except OSError:
                pass
            resp = recv_json_object(sock)
            resp = self._decrypt_response(resp)
            if resp.get("STATUS") == "E" or resp.get("Code") in (45, 132, 135, 136, 137):
                sock.close()
                raise CommandError(
                    str(resp.get("Msg", resp)),
                    code=resp.get("Code"),
                    raw=resp,
                )
            return sock, resp
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    # ── Readable API (manual §4) ─────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        return self.read("summary")

    def pools(self) -> dict[str, Any]:
        return self.read("pools")

    def edevs(self) -> dict[str, Any]:
        return self.read("edevs")

    def devs(self) -> dict[str, Any]:
        """Alias of edevs on some firmwares."""
        return self.read("devs")

    def devdetails(self) -> dict[str, Any]:
        """Hashboard model details (not recommended for continuous polling)."""
        return self.read("devdetails")

    def get_psu(self) -> dict[str, Any]:
        return self.read("get_psu")

    def get_version(self) -> dict[str, Any]:
        return self.read("get_version")

    def get_token(self) -> dict[str, Any]:
        """Raw get_token (also used internally by refresh_token)."""
        return self.read("get_token")

    def status(self) -> dict[str, Any]:
        return self.read("status")

    def get_miner_info(
        self,
        info: str = "ip,proto,netmask,gateway,dns,hostname,mac,ledstat,minersn,powersn",
    ) -> dict[str, Any]:
        return self.read("get_miner_info", info=info)

    def get_error_code(self) -> dict[str, Any]:
        return self.read("get_error_code")

    def get_customer_msg(self) -> dict[str, Any]:
        """Read customer-defined SN / msg0..msg9 (write-token required on some FW)."""
        # Manual lists under writable section but command is a read with token.
        try:
            return self.write("get_customer_msg")
        except Exception:
            return self.read("get_customer_msg")

    def snapshot(self) -> MinerStatus:
        """Aggregate summary + boards + pools into MinerStatus."""
        summary = self.summary()
        msg = summary.get("Msg") or {}
        if not isinstance(msg, dict):
            msg = {}

        boards: list[BoardInfo] = []
        try:
            ed = self.edevs()
            for d in ed.get("DEVS") or []:
                boards.append(
                    BoardInfo(
                        slot=int(d.get("Slot", d.get("ASC", 0))),
                        temperature=_f(d.get("Temperature")),
                        chip_frequency=_f(d.get("Chip Frequency")),
                        hashrate_mhs=_f(d.get("MHS av") or d.get("HS RT")),
                        factory_ghs=_f(d.get("Factory GHS")),
                        pcb_sn=str(d.get("PCB SN") or ""),
                        effective_chips=_i(d.get("Effective Chips")),
                        raw=d,
                    )
                )
        except Exception as e:
            log.debug("edevs failed on %s: %s", self.host, e)

        pools: list[PoolInfo] = []
        try:
            pr = self.pools()
            for p in pr.get("POOLS") or []:
                pools.append(
                    PoolInfo(
                        index=int(p.get("POOL", 0)),
                        url=str(p.get("URL") or p.get("Stratum URL") or ""),
                        user=str(p.get("User") or ""),
                        status=str(p.get("Status") or ""),
                        accepted=int(p.get("Accepted") or 0),
                        rejected=int(p.get("Rejected") or 0),
                        raw=p,
                    )
                )
        except Exception as e:
            log.debug("pools failed on %s: %s", self.host, e)

        errors: list[ErrorCode] = []
        try:
            from ..support.error_codes import describe_error

            er = self.get_error_code()
            em = er.get("Msg") or {}
            ec = em.get("error_code") if isinstance(em, dict) else None
            if isinstance(ec, dict):
                for code, ts in ec.items():
                    msg = describe_error(code)
                    errors.append(
                        ErrorCode(
                            code=str(code),
                            timestamp=str(ts),
                            description=msg,
                            cause=msg,
                            lang="en",
                        )
                    )
            elif isinstance(ec, list):
                for item in ec:
                    if isinstance(item, dict):
                        for code, ts in item.items():
                            msg = describe_error(code)
                            errors.append(
                                ErrorCode(
                                    code=str(code),
                                    timestamp=str(ts),
                                    description=msg,
                                    cause=msg,
                                    lang="en",
                                )
                            )
        except Exception as e:
            log.debug("get_error_code failed on %s: %s", self.host, e)

        miner_type = ""
        firmware = str(msg.get("Firmware Version") or "")
        platform = ""
        try:
            ver = self.get_version()
            vm = ver.get("Msg") or {}
            if isinstance(vm, dict):
                miner_type = str(vm.get("miner_type") or "")
                firmware = firmware or str(vm.get("fw_ver") or "")
                platform = str(vm.get("platform") or "")
        except Exception:
            pass

        mhs = _f(msg.get("MHS av") or msg.get("HS RT"))
        mhs_rt = _f(msg.get("HS RT") or msg.get("MHS 1m"))

        return MinerStatus(
            ip=self.host,
            api="v2",
            miner_type=miner_type,
            firmware=firmware,
            platform=platform,
            power_mode=str(msg.get("Power Mode") or ""),
            power_w=_f(msg.get("Power")),
            power_limit=_f(msg.get("Power Limit")),
            hashrate_ths=MinerStatus.mhs_to_ths(mhs),
            hashrate_rt_ths=MinerStatus.mhs_to_ths(mhs_rt),
            temp_chip_avg=_f(msg.get("Chip Temp Avg")),
            temp_chip_max=_f(msg.get("Chip Temp Max")),
            fan_in=_i(msg.get("Fan Speed In")),
            fan_out=_i(msg.get("Fan Speed Out")),
            uptime_s=_i(msg.get("Uptime")),
            elapsed_s=_f(msg.get("Elapsed")),
            pools=pools,
            boards=boards,
            errors=errors,
            raw={"summary": summary},
        )

    # ── Writable API (manual §3) ─────────────────────────────────────────────

    def update_pools(
        self,
        pool1: str,
        worker1: str,
        passwd1: str = "x",
        pool2: str = "",
        worker2: str = "",
        passwd2: str = "x",
        pool3: str = "",
        worker3: str = "",
        passwd3: str = "x",
    ) -> dict[str, Any]:
        return self.write(
            "update_pools",
            pool1=pool1,
            worker1=worker1,
            passwd1=passwd1,
            pool2=pool2,
            worker2=worker2,
            passwd2=passwd2,
            pool3=pool3,
            worker3=worker3,
            passwd3=passwd3,
        )

    def restart_btminer(self) -> dict[str, Any]:
        return self.write("restart_btminer")

    def power_off(self, respbefore: str | None = "true") -> dict[str, Any]:
        params: dict[str, Any] = {}
        if respbefore is not None:
            params["respbefore"] = respbefore
        return self.write("power_off", **params)

    def power_on(self) -> dict[str, Any]:
        return self.write("power_on")

    def set_led_auto(self) -> dict[str, Any]:
        return self.write("set_led", param="auto")

    def set_led_manual(
        self,
        color: str = "red",
        period: int = 1000,
        duration: int = 500,
        start: int = 0,
    ) -> dict[str, Any]:
        return self.write(
            "set_led",
            color=color,
            period=period,
            duration=duration,
            start=start,
        )

    def set_power_mode(self, mode: str) -> dict[str, Any]:
        mode = mode.lower().strip()
        mapping = {
            "low": "set_low_power",
            "normal": "set_normal_power",
            "high": "set_high_power",
        }
        if mode not in mapping:
            raise ValueError("mode must be low|normal|high")
        return self.write(mapping[mode])

    def update_firmware(
        self,
        firmware: str | Path | bytes | BinaryIO,
        *,
        timeout: float | None = 300.0,
    ) -> dict[str, Any]:
        """
        Firmware upgrade stream (manual §3.7).
        1) enc JSON update_firmware → ready
        2) u32le size + binary blob on same connection
        """
        if isinstance(firmware, (str, Path)):
            blob = Path(firmware).read_bytes()
        elif isinstance(firmware, bytes):
            blob = firmware
        else:
            blob = firmware.read()
            if isinstance(blob, str):
                blob = blob.encode("utf-8")

        old_timeout = self.timeout
        if timeout is not None:
            self.timeout = timeout
        try:
            sock, resp = self._write_stream_json("update_firmware")
            try:
                msg = resp.get("Msg")
                if str(msg).lower() not in ("ready",) and resp.get("Code") not in (
                    131,
                    None,
                ):
                    # still try if STATUS=S
                    if resp.get("STATUS") not in ("S", "s", None):
                        raise CommandError(
                            f"firmware not ready: {resp}",
                            code=resp.get("Code"),
                            raw=resp,
                        )
                send_u32le_blob(sock, blob)
                # some firmwares close; some send a final ack
                try:
                    sock.settimeout(min(30.0, timeout or 30.0))
                    tail = sock.recv(4096)
                    if tail:
                        try:
                            text = tail.decode("utf-8", errors="replace").strip().rstrip("\0")
                            if text.startswith("{"):
                                return json.loads(text)
                        except Exception:
                            pass
                except OSError:
                    pass
                return {"ok": True, "phase": "uploaded", "bytes": len(blob), "ack": resp}
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        finally:
            self.timeout = old_timeout

    def reboot(self) -> dict[str, Any]:
        return self.write("reboot")

    def factory_reset(self) -> dict[str, Any]:
        return self.write("factory_reset")

    def update_password(self, old: str, new: str) -> dict[str, Any]:
        resp = self.write("update_pwd", old=old, new=new)
        self.password = new
        self._sign = None
        self._aeskey = None
        return resp

    # alias matching manual cmd name
    def update_pwd(self, old: str, new: str) -> dict[str, Any]:
        return self.update_password(old, new)

    def net_config_dhcp(self) -> dict[str, Any]:
        return self.write("net_config", param="dhcp")

    def net_config_static(
        self,
        ip: str,
        mask: str,
        gate: str,
        dns: str,
        host: str = "",
    ) -> dict[str, Any]:
        return self.write("net_config", ip=ip, mask=mask, gate=gate, dns=dns, host=host)

    def download_logs(
        self,
        dest: str | Path | None = None,
        *,
        timeout: float | None = 120.0,
    ) -> bytes:
        """
        Download miner logs archive (manual §3.12).
        Returns raw bytes; optionally writes to dest path.
        """
        old_timeout = self.timeout
        if timeout is not None:
            self.timeout = timeout
        try:
            sock, resp = self._write_stream_json("download_logs")
            try:
                msg = resp.get("Msg") or {}
                size = 0
                if isinstance(msg, dict):
                    try:
                        size = int(msg.get("logfilelen") or 0)
                    except (TypeError, ValueError):
                        size = 0
                time.sleep(0.02)  # manual: 10 ms delay before binary
                if size > 0:
                    blob = recv_exact(sock, size)
                else:
                    # unknown length — read until close
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
            self.timeout = old_timeout

    def set_target_freq(self, percent: int | str) -> dict[str, Any]:
        return self.write("set_target_freq", percent=str(percent))

    def set_fast_boot(self, enable: bool) -> dict[str, Any]:
        return self.write(
            "enable_btminer_fast_boot" if enable else "disable_btminer_fast_boot"
        )

    def enable_btminer_fast_boot(self) -> dict[str, Any]:
        return self.write("enable_btminer_fast_boot")

    def disable_btminer_fast_boot(self) -> dict[str, Any]:
        return self.write("disable_btminer_fast_boot")

    def enable_web_pools(self) -> dict[str, Any]:
        return self.write("enable_web_pools")

    def disable_web_pools(self) -> dict[str, Any]:
        return self.write("disable_web_pools")

    def set_hostname(self, hostname: str) -> dict[str, Any]:
        return self.write("set_hostname", hostname=hostname)

    def set_zone(self, timezone: str, zonename: str) -> dict[str, Any]:
        """e.g. timezone='CST-8', zonename='Asia/Shanghai'."""
        return self.write("set_zone", timezone=timezone, zonename=zonename)

    def set_timezone(self, timezone: str, zonename: str) -> dict[str, Any]:
        """Alias of set_zone."""
        return self.set_zone(timezone, zonename)

    def load_log(self, ip: str, port: str | int, proto: str = "udp") -> dict[str, Any]:
        """Configure rsyslog remote log server."""
        return self.write("load_log", ip=str(ip), port=str(port), proto=str(proto))

    def set_power_pct(self, percent: int | str, *, fast: bool = False) -> dict[str, Any]:
        """Fast mode = set_power_pct; normal = set_power_pct_v2."""
        cmd = "set_power_pct" if fast else "set_power_pct_v2"
        return self.write(cmd, percent=str(percent))

    def set_power_pct_v2(self, percent: int | str) -> dict[str, Any]:
        return self.write("set_power_pct_v2", percent=str(percent))

    def set_power(self, watts: int | str) -> dict[str, Any]:
        return self.write("set_power", power=str(watts))

    def restore_power_pct(self) -> dict[str, Any]:
        return self.write("restore_power_pct")

    def set_temp_offset(self, offset: int | str) -> dict[str, Any]:
        """Target temp offset °C, range typically -30..0 (air-cooled)."""
        return self.write("set_temp_offset", temp_offset=str(offset))

    def set_power_limit(self, watts: int | str) -> dict[str, Any]:
        return self.write("adjust_power_limit", power_limit=str(watts))

    def adjust_power_limit(self, watts: int | str) -> dict[str, Any]:
        return self.set_power_limit(watts)

    def adjust_upfreq_speed(self, speed: int | str) -> dict[str, Any]:
        """0 = normal … 10 = fastest."""
        return self.write("adjust_upfreq_speed", upfreq_speed=str(speed))

    def set_poweroff_cool(self, enable: bool | int | str) -> dict[str, Any]:
        val = "1" if str(enable).lower() in ("1", "true", "yes", "on") or enable is True else "0"
        return self.write("set_poweroff_cool", poweroff_cool=val)

    def set_fan_zero_speed(self, enable: bool | int | str) -> dict[str, Any]:
        val = "1" if str(enable).lower() in ("1", "true", "yes", "on") or enable is True else "0"
        return self.write("set_fan_zero_speed", fan_zero_speed=val)

    def set_heat_mode(self, mode: str) -> dict[str, Any]:
        """anti-icing | heating (hydro). Manual also mentions power-keeping modes."""
        return self.write("set_heat_mode", mode=mode)

    def disable_btminer_init(self) -> dict[str, Any]:
        """Prevent mining auto-start on boot."""
        return self.write("disable_btminer_init")

    def enable_btminer_init(self) -> dict[str, Any]:
        return self.write("enable_btminer_init")

    def set_btminer_init(self, enable: bool) -> dict[str, Any]:
        return self.enable_btminer_init() if enable else self.disable_btminer_init()

    def set_customer_msg(self, key: str, val: str) -> dict[str, Any]:
        """key: CustomerSn | msg0..msg9."""
        return self.write("set_customer_msg", key=key, val=val)

    def set_fast_mining(self, enable: bool | int | str = True) -> dict[str, Any]:
        val = "1" if str(enable).lower() in ("1", "true", "yes", "on") or enable is True else "0"
        return self.write("set_fast_mining", fast_mining=val)

    def set_fast_hash(self, enable: bool | int | str = True) -> dict[str, Any]:
        """Same function as set_fast_mining on supported models (VK/VL/VM)."""
        val = "1" if str(enable).lower() in ("1", "true", "yes", "on") or enable is True else "0"
        return self.write("set_fast_hash", fast_hash=val)

    # ── introspection ────────────────────────────────────────────────────────

    @classmethod
    def list_commands(cls) -> dict[str, list[str]]:
        return {
            "read": sorted(V2_READ_CMDS),
            "write": sorted(V2_WRITE_CMDS),
        }


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
