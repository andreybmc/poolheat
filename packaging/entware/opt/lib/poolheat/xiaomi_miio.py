#!/usr/bin/env python3
"""
Minimal Xiaomi miIO client (UDP 54321) for smart plugs / switches.

Uses only stdlib + pycryptodome (already on Entware for poolheat).
No python-miio / netifaces — those need a C compiler on Keenetic.

Token: 32 hex chars (16 bytes). Extract once (cloud / app tools), then pure LAN.
Protocol ref: community miIO (same as python-miio).
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from typing import Any, Optional

try:
    from Crypto.Cipher import AES  # type: ignore
except ImportError:  # pragma: no cover
    AES = None  # type: ignore


MAGIC = 0x2131
PORT = 54321
HELLO = bytes.fromhex(
    "21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)


def _require_aes() -> None:
    if AES is None:
        raise RuntimeError("pycryptodome required (pip install pycryptodome)")


def normalize_token(token: str) -> bytes:
    t = str(token or "").strip().replace(" ", "").lower()
    if t.startswith("0x"):
        t = t[2:]
    if len(t) != 32:
        raise ValueError("Xiaomi token must be 32 hex chars")
    try:
        raw = bytes.fromhex(t)
    except ValueError as e:
        raise ValueError("Xiaomi token is not valid hex") from e
    if len(raw) != 16:
        raise ValueError("Xiaomi token must decode to 16 bytes")
    return raw


def _md5(*parts: bytes) -> bytes:
    h = hashlib.md5()
    for p in parts:
        h.update(p)
    return h.digest()


def _pad16(data: bytes) -> bytes:
    # PKCS#7
    n = 16 - (len(data) % 16)
    return data + bytes([n] * n)


def _unpad16(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data.endswith(bytes([n]) * n):
        return data[:-n]
    return data


class XiaomiMiio:
    """LAN miIO device (plug / switch)."""

    def __init__(
        self,
        ip: str,
        token: str | bytes,
        *,
        timeout: float = 5.0,
        device_id: int | None = None,
    ):
        _require_aes()
        self.ip = str(ip or "").strip()
        if not self.ip:
            raise ValueError("Xiaomi IP empty")
        if isinstance(token, bytes):
            if len(token) != 16:
                raise ValueError("Xiaomi token bytes must be 16 long")
            self.token = token
        else:
            self.token = normalize_token(token)
        self.timeout = float(timeout)
        self.device_id = int(device_id) if device_id else 0
        self.stamp = 0
        self._seen = 0
        self._id = int(time.time()) % 10000
        self._key = _md5(self.token)
        self._iv = _md5(self._key + self.token)
        self.model: Optional[str] = None
        self.fw: Optional[str] = None

    def _encrypt(self, plaintext: bytes) -> bytes:
        cipher = AES.new(self._key, AES.MODE_CBC, iv=self._iv)
        return cipher.encrypt(_pad16(plaintext))

    def _decrypt(self, ciphertext: bytes) -> bytes:
        if not ciphertext:
            return b""
        cipher = AES.new(self._key, AES.MODE_CBC, iv=self._iv)
        return _unpad16(cipher.decrypt(ciphertext))

    def _handshake(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(HELLO, (self.ip, PORT))
            data, _ = sock.recvfrom(1024)
        finally:
            sock.close()
        if len(data) < 32:
            raise RuntimeError("Xiaomi handshake short reply")
        magic, length = struct.unpack(">HH", data[0:4])
        if magic != MAGIC:
            raise RuntimeError(f"Xiaomi bad magic {magic:#x}")
        did, stamp = struct.unpack(">II", data[8:16])
        self.device_id = did
        self.stamp = stamp
        self._seen = time.time()

    def _build(self, payload: dict) -> bytes:
        if not self.device_id or (time.time() - self._seen) > 120:
            self._handshake()
        self.stamp = (self.stamp + 1) & 0xFFFFFFFF
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        enc = self._encrypt(body)
        # header: magic, length, unknown(0), device_id, stamp, token-space/md5
        length = 32 + len(enc)
        header = bytearray(32)
        struct.pack_into(">HH", header, 0, MAGIC, length)
        struct.pack_into(">I", header, 4, 0)
        struct.pack_into(">I", header, 8, self.device_id & 0xFFFFFFFF)
        struct.pack_into(">I", header, 12, self.stamp)
        # md5 over header[0:16] + token + enc  → put in [16:32]
        header[16:32] = b"\x00" * 16
        checksum = _md5(bytes(header[0:16]), self.token, enc)
        header[16:32] = checksum
        return bytes(header) + enc

    def send(self, method: str, params: Any = None) -> Any:
        self._id = (self._id + 1) % 100000
        payload: dict = {"id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        raw = self._build(payload)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(raw, (self.ip, PORT))
            data, _ = sock.recvfrom(4096)
        except socket.timeout as e:
            raise RuntimeError(f"Xiaomi timeout {self.ip}:54321") from e
        finally:
            sock.close()
        if len(data) < 32:
            raise RuntimeError("Xiaomi short response")
        # update stamp/device from header
        did, stamp = struct.unpack(">II", data[8:16])
        if did:
            self.device_id = did
        self.stamp = stamp
        self._seen = time.time()
        enc = data[32:]
        if not enc:
            return None
        try:
            plain = self._decrypt(enc)
            msg = json.loads(plain.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"Xiaomi decrypt/parse failed (bad token?): {e}") from e
        if isinstance(msg, dict):
            if msg.get("error"):
                err = msg["error"]
                if isinstance(err, dict):
                    raise RuntimeError(
                        f"Xiaomi error {err.get('code')}: {err.get('message')}"
                    )
                raise RuntimeError(f"Xiaomi error: {err}")
            return msg.get("result", msg)
        return msg

    def info(self) -> dict:
        res = self.send("miIO.info")
        if isinstance(res, dict):
            self.model = str(res.get("model") or "") or self.model
            self.fw = str(res.get("fw_ver") or res.get("fw") or "") or self.fw
            return res
        return {}

    # ── power on/off strategies ─────────────────────────────────────────────

    def get_on(self) -> bool | None:
        # 1) legacy get_prop
        for props in (["power"], ["Pow"], ["on"]):
            try:
                r = self.send("get_prop", props)
                v = _first_val(r)
                b = _as_bool(v)
                if b is not None:
                    return b
            except Exception:
                pass
        # 2) MIoT get_properties common switch slots
        for siid, piid in ((2, 1), (3, 1), (2, 2), (5, 1), (4, 1)):
            try:
                r = self.send(
                    "get_properties",
                    [{"did": str(self.device_id or "0"), "siid": siid, "piid": piid}],
                )
                v = _miot_value(r)
                b = _as_bool(v)
                if b is not None:
                    return b
            except Exception:
                pass
        return None

    def set_on(self, on: bool) -> bool:
        on = bool(on)
        errors: list[str] = []
        # legacy
        for method, params in (
            ("set_power", ["on" if on else "off"]),
            ("set_power", [on]),
        ):
            try:
                self.send(method, params)
                got = self.get_on()
                if got is None or got is on:
                    return on if got is None else got
            except Exception as e:
                errors.append(f"{method}:{e}")
        # MIoT
        for siid, piid in ((2, 1), (3, 1), (2, 2), (5, 1), (4, 1)):
            try:
                self.send(
                    "set_properties",
                    [
                        {
                            "did": str(self.device_id or "0"),
                            "siid": siid,
                            "piid": piid,
                            "value": on,
                        }
                    ],
                )
                got = self.get_on()
                if got is None or got is on:
                    return on if got is None else got
            except Exception as e:
                errors.append(f"miot {siid}/{piid}:{e}")
        # last try toggle-style
        try:
            self.send("toggle", [])
            got = self.get_on()
            if got is not None:
                if got is not on:
                    # toggled wrong way — toggle again
                    self.send("toggle", [])
                    got = self.get_on()
                if got is not None:
                    return got
        except Exception as e:
            errors.append(f"toggle:{e}")
        raise RuntimeError(
            "Xiaomi set_on failed ("
            + ("; ".join(errors[:3]) if errors else "unsupported model")
            + ")"
        )

    def get_power_metrics(self) -> dict | None:
        """Return power_w / voltage_v / current_a when the plug reports them."""
        out: dict = {}
        # legacy props
        for batch in (
            ["power", "power_consume_rate", "load_power", "voltage", "current"],
            ["power_consume_rate"],
            ["load_power"],
            ["power"],
            ["elec_power"],
        ):
            try:
                r = self.send("get_prop", batch)
                if not isinstance(r, list):
                    continue
                for name, val in zip(batch, r):
                    if val is None or val == "" or val == "null":
                        continue
                    try:
                        f = float(val)
                    except (TypeError, ValueError):
                        continue
                    n = name.lower()
                    if n in ("power", "power_consume_rate", "load_power", "elec_power"):
                        # some report deciwatts
                        out["power_w"] = round(f / 10.0, 2) if f > 2000 else round(f, 2)
                    elif "volt" in n:
                        out["voltage_v"] = round(f / 10.0, 2) if f > 400 else round(f, 2)
                    elif "curr" in n:
                        out["current_a"] = (
                            round(f / 1000.0, 3) if f > 30 else round(f, 3)
                        )
                if out:
                    return out
            except Exception:
                continue
        # MIoT power sensors (common layouts)
        for siid, piid, kind in (
            (3, 1, "power"),
            (3, 2, "voltage"),
            (3, 3, "current"),
            (4, 1, "power"),
            (2, 2, "power"),
            (5, 1, "power"),
            (8, 1, "power"),
            (11, 1, "power"),
        ):
            try:
                r = self.send(
                    "get_properties",
                    [{"did": str(self.device_id or "0"), "siid": siid, "piid": piid}],
                )
                v = _miot_value(r)
                if v is None:
                    continue
                f = float(v)
                if kind == "power":
                    out["power_w"] = round(f / 10.0, 2) if f > 2000 else round(f, 2)
                elif kind == "voltage":
                    out["voltage_v"] = round(f / 10.0, 2) if f > 400 else round(f, 2)
                elif kind == "current":
                    out["current_a"] = round(f / 1000.0, 3) if f > 30 else round(f, 3)
            except Exception:
                continue
        return out or None


def _first_val(r: Any) -> Any:
    if isinstance(r, list) and r:
        return r[0]
    return r


def _miot_value(r: Any) -> Any:
    if isinstance(r, list) and r:
        item = r[0]
        if isinstance(item, dict):
            if item.get("code") not in (None, 0):
                return None
            return item.get("value")
        return item
    if isinstance(r, dict):
        return r.get("value", r)
    return r


def _as_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return bool(v)
        return None
    s = str(v).strip().lower()
    if s in ("on", "true", "1", "yes", "open"):
        return True
    if s in ("off", "false", "0", "no", "close", "closed"):
        return False
    return None


def control(
    ip: str,
    token: str,
    on: bool | None,
    *,
    timeout: float = 5.0,
) -> dict:
    """
    on=None → read state (+ power if any).
    on=bool → set then read.
    """
    dev = XiaomiMiio(ip, token, timeout=timeout)
    try:
        info = dev.info()
    except Exception:
        info = {}
    if on is None:
        state = dev.get_on()
    else:
        state = dev.set_on(bool(on))
    out: dict = {
        "on": state,
        "backend": "xiaomi",
        "ip": ip,
        "model": dev.model or (info.get("model") if isinstance(info, dict) else None),
        "device_id": dev.device_id or None,
    }
    try:
        pm = dev.get_power_metrics()
        if pm:
            out["power"] = pm
    except Exception:
        pass
    return out
