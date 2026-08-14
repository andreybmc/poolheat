#!/usr/bin/env python3
"""
eWeLink / Sonoff LAN control (CoolKit).

Modes:
  1) DIY  — unencrypted POST /zeroconf/*  (no key)
  2) LAN  — encrypted AES-128-CBC with per-device devicekey (from cloud)

Cloud (optional): email/password → access token → device list + devicekey.
Stdlib only (urllib) for Entware.

Refs: AlexxIT/SonoffLAN local encrypt + CoolKit API v2 login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# CoolKit / eWeLink app credentials (same as HA SonoffLAN community appid)
APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
APP_SECRET = "1ve5Qk9GXfUhKAn1svnKwpAlxXkMarru"

API_HOSTS = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}

# country code (+N) → region key
COUNTRY_REGION = {
    "+7": "eu",  # RU/KZ
    "+380": "eu",
    "+375": "eu",
    "+48": "eu",
    "+49": "eu",
    "+44": "eu",
    "+33": "eu",
    "+39": "eu",
    "+34": "eu",
    "+1": "us",
    "+86": "cn",
    "+81": "as",
    "+82": "as",
    "+61": "us",
    "+91": "as",
}


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    except Exception:
        pass
    return ctx


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 12.0,
) -> dict:
    hdrs = {"User-Agent": "poolheat-ewelink/1.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            j = json.loads(raw) if raw else {}
        except Exception:
            j = {}
        if isinstance(j, dict) and j.get("error") is not None:
            return j
        raise RuntimeError(f"HTTP {e.code}: {raw[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"bad JSON: {raw[:160]}") from e


def sign_body(data: bytes) -> str:
    dig = hmac.new(APP_SECRET.encode("utf-8"), data, hashlib.sha256).digest()
    return base64.b64encode(dig).decode("ascii")


def resolve_region(country_code: str = "+7", region: str | None = None) -> str:
    if region and str(region).lower() in API_HOSTS:
        return str(region).lower()
    cc = str(country_code or "+7").strip()
    if not cc.startswith("+"):
        cc = "+" + cc
    return COUNTRY_REGION.get(cc, "eu")


def cloud_login(
    email: str,
    password: str,
    *,
    country_code: str = "+7",
    region: str | None = None,
) -> dict[str, Any]:
    """
    Login to eWeLink cloud.
    Returns {at, region, user, apikey, raw}.
    """
    email = str(email or "").strip()
    password = str(password or "")
    if not email or not password:
        raise ValueError("email/password empty")
    reg = resolve_region(country_code, region)
    host = API_HOSTS[reg]
    cc = str(country_code or "+7").strip()
    if not cc.startswith("+"):
        cc = "+" + cc
    payload: dict[str, Any] = {"password": password, "countryCode": cc}
    if "@" in email:
        payload["email"] = email
    else:
        phone = email if email.startswith("+") else ("+" + email)
        payload["phoneNumber"] = phone
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": "Sign " + sign_body(data),
        "Content-Type": "application/json",
        "X-CK-Appid": APP_ID,
    }
    resp = _http_json("POST", host + "/v2/user/login", headers=headers, body=data)
    # wrong region → retry
    if resp.get("error") == 10004 and isinstance(resp.get("data"), dict):
        reg2 = str(resp["data"].get("region") or reg)
        if reg2 in API_HOSTS and reg2 != reg:
            reg = reg2
            host = API_HOSTS[reg]
            resp = _http_json(
                "POST", host + "/v2/user/login", headers=headers, body=data
            )
    if resp.get("error") not in (0, None, "0"):
        raise RuntimeError(
            f"eWeLink login error={resp.get('error')}: {resp.get('msg') or resp}"
        )
    auth = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    if not auth.get("at"):
        raise RuntimeError("eWeLink login: no access token")
    user = auth.get("user") if isinstance(auth.get("user"), dict) else {}
    return {
        "at": str(auth["at"]),
        "region": reg,
        "user": user,
        "apikey": user.get("apikey") or auth.get("apikey"),
        "raw": auth,
    }


def cloud_list_devices(at: str, region: str = "eu") -> list[dict[str, Any]]:
    """
    Fetch devices for account. Each item:
      deviceid, devicekey, name, online, productModel, brandName, uiid, params, ip?
    """
    reg = str(region or "eu").lower()
    if reg not in API_HOSTS:
        reg = "eu"
    host = API_HOSTS[reg]
    headers = {
        "Authorization": "Bearer " + str(at),
        "Content-Type": "application/json",
        "X-CK-Appid": APP_ID,
    }
    resp = _http_json(
        "GET",
        host + "/v2/device/thing?num=0",
        headers=headers,
        timeout=20.0,
    )
    if resp.get("error") not in (0, None, "0"):
        raise RuntimeError(
            f"eWeLink devices error={resp.get('error')}: {resp.get('msg') or resp}"
        )
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    things = data.get("thingList") if isinstance(data.get("thingList"), list) else []
    out: list[dict[str, Any]] = []
    for th in things:
        if not isinstance(th, dict):
            continue
        item = th.get("itemData") if isinstance(th.get("itemData"), dict) else th
        if not isinstance(item, dict):
            continue
        did = str(item.get("deviceid") or "").strip()
        if not did:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        # local IP sometimes in params / extra
        ip = (
            params.get("ip")
            or params.get("localAddress")
            or extra.get("ip")
            or item.get("ip")
            or ""
        )
        out.append(
            {
                "deviceid": did,
                "devicekey": str(item.get("devicekey") or item.get("deviceKey") or "").strip()
                or None,
                "apikey": str(item.get("apikey") or "").strip() or None,
                "name": str(item.get("name") or did),
                "online": bool(item.get("online")),
                "productModel": str(
                    item.get("productModel")
                    or extra.get("model")
                    or item.get("model")
                    or ""
                ),
                "brandName": str(item.get("brandName") or "eWeLink"),
                "uiid": extra.get("uiid") or item.get("uiid"),
                "params": params,
                "ip": str(ip).strip() or None,
            }
        )
    return out


# ── LAN encryption (AES-128-CBC, key=MD5(devicekey)) ─────────────────────────
# Fallback chain: cryptography → PyCryptodome → openssl CLI → pure Python AES.


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n] * n)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n] * n):
        return data  # best-effort
    return data[:-n]


# Minimal AES-128 (stdlib only) for Entware without cryptography/openssl.
_SBOX = bytes(
    [
        0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
        0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
        0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
        0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
        0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
        0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
        0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
        0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
        0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
        0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
        0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
        0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
        0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
        0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
        0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
        0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
    ]
)
_INV_SBOX = bytes([_SBOX.index(i) for i in range(256)])
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1) & 0xFF


def _mul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


def _aes128_expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    w = list(key)
    i = 1
    while len(w) < 176:
        t = w[-4:]
        if len(w) % 16 == 0:
            t = [t[1], t[2], t[3], t[0]]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i]
            i += 1
        for j in range(4):
            w.append(w[-16] ^ t[j])
    return [w[i : i + 16] for i in range(0, 176, 16)]


def _aes128_encrypt_block(rk: list[list[int]], block: bytes) -> bytes:
    s = [block[i] ^ rk[0][i] for i in range(16)]

    def sub():
        for i in range(16):
            s[i] = _SBOX[s[i]]

    def shift():
        s[1], s[5], s[9], s[13] = s[5], s[9], s[13], s[1]
        s[2], s[6], s[10], s[14] = s[10], s[14], s[2], s[6]
        s[3], s[7], s[11], s[15] = s[15], s[3], s[7], s[11]

    def mix():
        for c in range(4):
            i = c * 4
            a, b, d, e = s[i], s[i + 1], s[i + 2], s[i + 3]
            s[i] = _xtime(a) ^ _xtime(b) ^ b ^ d ^ e
            s[i + 1] = a ^ _xtime(b) ^ _xtime(d) ^ d ^ e
            s[i + 2] = a ^ b ^ _xtime(d) ^ _xtime(e) ^ e
            s[i + 3] = _xtime(a) ^ a ^ b ^ d ^ _xtime(e)

    for r in range(1, 10):
        sub()
        shift()
        mix()
        for i in range(16):
            s[i] ^= rk[r][i]
    sub()
    shift()
    for i in range(16):
        s[i] ^= rk[10][i]
    return bytes(s)


def _aes128_decrypt_block(rk: list[list[int]], block: bytes) -> bytes:
    s = [block[i] ^ rk[10][i] for i in range(16)]

    def inv_sub():
        for i in range(16):
            s[i] = _INV_SBOX[s[i]]

    def inv_shift():
        s[1], s[5], s[9], s[13] = s[13], s[1], s[5], s[9]
        s[2], s[6], s[10], s[14] = s[10], s[14], s[2], s[6]
        s[3], s[7], s[11], s[15] = s[7], s[11], s[15], s[3]

    def inv_mix():
        for c in range(4):
            i = c * 4
            a, b, d, e = s[i], s[i + 1], s[i + 2], s[i + 3]
            s[i] = _mul(a, 0x0E) ^ _mul(b, 0x0B) ^ _mul(d, 0x0D) ^ _mul(e, 0x09)
            s[i + 1] = _mul(a, 0x09) ^ _mul(b, 0x0E) ^ _mul(d, 0x0B) ^ _mul(e, 0x0D)
            s[i + 2] = _mul(a, 0x0D) ^ _mul(b, 0x09) ^ _mul(d, 0x0E) ^ _mul(e, 0x0B)
            s[i + 3] = _mul(a, 0x0B) ^ _mul(b, 0x0D) ^ _mul(d, 0x09) ^ _mul(e, 0x0E)

    for r in range(9, 0, -1):
        inv_shift()
        inv_sub()
        for i in range(16):
            s[i] ^= rk[r][i]
        inv_mix()
    inv_shift()
    inv_sub()
    for i in range(16):
        s[i] ^= rk[0][i]
    return bytes(s)


def _aes128_cbc_pure(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
    if len(data) % 16 != 0:
        raise ValueError("AES-CBC data length must be multiple of 16")
    rk = _aes128_expand_key(key)
    out = bytearray()
    prev = iv
    if encrypt:
        for i in range(0, len(data), 16):
            block = bytes(data[i + j] ^ prev[j] for j in range(16))
            enc = _aes128_encrypt_block(rk, block)
            out.extend(enc)
            prev = enc
    else:
        for i in range(0, len(data), 16):
            block = data[i : i + 16]
            dec = _aes128_decrypt_block(rk, block)
            out.extend(dec[j] ^ prev[j] for j in range(16))
            prev = block
    return bytes(out)


def _aes128_cbc_encrypt(key: bytes, iv: bytes, plain: bytes) -> bytes:
    """AES-128-CBC: cryptography → PyCryptodome → openssl → pure Python."""
    padded = _pkcs7_pad(plain, 16)
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return enc.update(padded) + enc.finalize()
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # type: ignore

        return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    except ImportError:
        pass
    # openssl enc (optional)
    try:
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(padded)
            inp = tf.name
        try:
            return subprocess.check_output(
                [
                    "openssl",
                    "enc",
                    "-aes-128-cbc",
                    "-nosalt",
                    "-nopad",
                    "-K",
                    key.hex(),
                    "-iv",
                    iv.hex(),
                    "-in",
                    inp,
                ],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        finally:
            try:
                os.unlink(inp)
            except Exception:
                pass
    except Exception:
        pass
    return _aes128_cbc_pure(key, iv, padded, encrypt=True)


def _aes128_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = dec.update(ct) + dec.finalize()
        return _pkcs7_unpad(padded)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # type: ignore

        return _pkcs7_unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct))
    except ImportError:
        pass
    try:
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(ct)
            inp = tf.name
        try:
            out = subprocess.check_output(
                [
                    "openssl",
                    "enc",
                    "-d",
                    "-aes-128-cbc",
                    "-nosalt",
                    "-nopad",
                    "-K",
                    key.hex(),
                    "-iv",
                    iv.hex(),
                    "-in",
                    inp,
                ],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return _pkcs7_unpad(out)
        finally:
            try:
                os.unlink(inp)
            except Exception:
                pass
    except Exception:
        pass
    return _pkcs7_unpad(_aes128_cbc_pure(key, iv, ct, encrypt=False))


def encrypt_data(data_obj: dict, devicekey: str) -> tuple[str, str]:
    """Return (b64_ciphertext, b64_iv)."""
    plaintext = json.dumps(data_obj, separators=(",", ":")).encode("utf-8")
    key = hashlib.md5(str(devicekey).encode("utf-8")).digest()
    iv = os.urandom(16)
    ct = _aes128_cbc_encrypt(key, iv, plaintext)
    return base64.b64encode(ct).decode("ascii"), base64.b64encode(iv).decode("ascii")


def decrypt_data(data_b64: str, iv_b64: str, devicekey: str) -> dict:
    key = hashlib.md5(str(devicekey).encode("utf-8")).digest()
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(data_b64)
    plain = _aes128_cbc_decrypt(key, iv, ct)
    plain = plain.rstrip(b"\x02")
    return json.loads(plain.decode("utf-8"))


def _pick_bind_ip(target_ip: str) -> str | None:
    try:
        import ipaddress
        import subprocess

        t = ipaddress.ip_address(target_ip)
        try:
            out = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode()
        except Exception:
            out = subprocess.check_output(
                ["busybox", "ip", "-o", "-4", "addr"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode()
        for line in out.splitlines():
            m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if not m:
                continue
            net = ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}", strict=False)
            if t in net:
                return m.group(1)
    except Exception:
        pass
    return None


def lan_http_post(
    ip: str,
    path: str,
    payload: dict,
    *,
    port: int = 8081,
    timeout: float = 5.0,
    retries: int = 4,
) -> dict:
    """
    POST JSON to device :8081/zeroconf/...
    Retries on ConnectionReset (single-threaded ESP web server).
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {ip}:{int(port)}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Accept: application/json\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii") + body
    bind = _pick_bind_ip(ip)
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if bind:
                try:
                    s.bind((bind, 0))
                except Exception:
                    pass
            s.settimeout(timeout)
            s.connect((ip, int(port)))
            s.sendall(req)
            chunks: list[bytes] = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    s.settimeout(max(0.15, deadline - time.time()))
                    c = s.recv(8192)
                    if not c:
                        break
                    chunks.append(c)
                except socket.timeout:
                    if chunks:
                        break
                    continue
            raw = b"".join(chunks)
            if not raw:
                last = TimeoutError("empty reply")
                time.sleep(0.15 + 0.1 * attempt)
                continue
            head, _, rest = raw.partition(b"\r\n\r\n")
            if not rest:
                head, _, rest = raw.partition(b"\n\n")
            text = rest.decode("utf-8", errors="replace").strip()
            if not text.startswith("{"):
                # text/html or garbage
                raise RuntimeError(f"non-JSON response: {text[:80]!r}")
            return json.loads(text)
        except ConnectionResetError as e:
            last = e
            time.sleep(0.12 + 0.08 * attempt)
            continue
        except Exception as e:
            last = e
            time.sleep(0.1)
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    raise RuntimeError(f"eWeLink LAN {ip}:{port}{path}: {last}")


def lan_send(
    ip: str,
    deviceid: str,
    *,
    devicekey: str | None = None,
    command: str = "info",
    data: dict | None = None,
    port: int = 8081,
    self_apikey: str = "123",
    timeout: float = 5.0,
) -> dict:
    """
    Send zeroconf command. Encrypts when devicekey provided.
    command: info | switch | switches | getState | uiActive | …
    self_apikey: user apikey from cloud (preferred) or "123" (DIY).
    """
    deviceid = str(deviceid or "").strip()
    if not deviceid:
        raise ValueError("deviceid empty")
    seq = str(int(time.time() * 1000))
    # CoolKit accepts either user apikey or DIY placeholder "123"
    apikey = str(self_apikey or "").strip() or "123"
    payload: dict[str, Any] = {
        "sequence": seq,
        "deviceid": deviceid,
        "selfApikey": apikey,
        "data": data if data is not None else {},
    }
    if devicekey:
        ct, iv = encrypt_data(payload["data"], devicekey)
        payload["encrypt"] = True
        payload["data"] = ct
        payload["iv"] = iv
    resp = lan_http_post(
        ip, f"/zeroconf/{command}", payload, port=port, timeout=timeout
    )
    # decrypt response data if present
    if (
        devicekey
        and isinstance(resp, dict)
        and resp.get("data")
        and resp.get("iv")
        and resp.get("encrypt")
    ):
        try:
            resp["data_decrypted"] = decrypt_data(
                str(resp["data"]), str(resp["iv"]), devicekey
            )
        except Exception:
            pass
    elif (
        devicekey
        and isinstance(resp, dict)
        and isinstance(resp.get("data"), str)
        and resp.get("iv")
    ):
        try:
            resp["data_decrypted"] = decrypt_data(
                str(resp["data"]), str(resp["iv"]), devicekey
            )
        except Exception:
            pass
    return resp


def parse_power_params(params: dict | None) -> dict | None:
    """Extract power_w / voltage_v / current_a from device params (LAN or cloud)."""
    if not isinstance(params, dict):
        return None
    # flatten one level of nested params/config
    flat = dict(params)
    for nest_k in ("params", "config", "data"):
        nested = params.get(nest_k)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    out: dict[str, float] = {}

    def _f(v) -> float | None:
        try:
            if v is None or v == "":
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    # voltage (SI or centi-V)
    for k in (
        "voltage",
        "voltage_00",
        "voltage_0",
        "voltage_01",
        "voltageV",
        "vol",
        "V",
    ):
        v = _f(flat.get(k))
        if v is None:
            continue
        out["voltage_v"] = round(v / 100.0, 2) if v > 400 else round(v, 2)
        break
    # current (SI or centi-A)
    for k in (
        "current",
        "current_00",
        "current_0",
        "current_01",
        "supplyCurrent",
        "currentA",
        "cur",
        "I",
    ):
        v = _f(flat.get(k))
        if v is None:
            continue
        if v > 20:
            out["current_a"] = round(v / 100.0, 3)
        else:
            out["current_a"] = round(v, 3)
        break
    # power (SI or centi-W); multi-channel sum actPow_00+actPow_01
    psum = 0.0
    pfound = False
    for k in (
        "power",
        "actPow_00",
        "actPow_0",
        "actPow_01",
        "actPow_1",
        "supplyPower",
        "powerW",
        "pwr",
        "P",
    ):
        v = _f(flat.get(k))
        if v is None:
            continue
        if k.startswith("actPow"):
            psum += v / 100.0 if v > 5000 else v
            pfound = True
            continue
        out["power_w"] = round(v / 100.0, 2) if v > 5000 else round(v, 2)
        pfound = True
        break
    if "power_w" not in out and pfound and psum > 0:
        out["power_w"] = round(psum, 2)
    # energy day kWh
    for k in ("dayKwh", "hundredDaysKwhData", "oneKwh", "energy", "energy_kwh"):
        v = _f(flat.get(k))
        if v is None:
            continue
        out["energy_kwh"] = round(v / 100.0 if v > 100 else v, 3)
        break
    return out or None


def parse_switch(params: dict | None) -> bool | None:
    if not isinstance(params, dict):
        return None
    if "switch" in params:
        return str(params.get("switch")).lower() in ("on", "1", "true")
    sw = params.get("switches")
    if isinstance(sw, list) and sw:
        # any outlet on → on
        for it in sw:
            if isinstance(it, dict) and str(it.get("switch")).lower() in (
                "on",
                "1",
                "true",
            ):
                return True
        return False
    return None


# Per-host detected transport for mode=auto: "diy" | "lan"
_EWELINK_PROTO_CACHE: dict[str, str] = {}


def _ewelink_ok(resp: dict | None) -> bool:
    if not isinstance(resp, dict):
        return False
    err = resp.get("error")
    return err in (0, "0", None)


def control(
    ip: str,
    deviceid: str,
    *,
    on: bool | None = None,
    devicekey: str | None = None,
    port: int = 8081,
    mode: str = "auto",  # auto | diy | lan
    outlet: int = 0,
    timeout: float = 5.0,
    self_apikey: str | None = None,
    email: str | None = None,
    password: str | None = None,
    country_code: str = "+7",
    region: str | None = None,
    cloud_fallback: bool = True,
) -> dict[str, Any]:
    """
    Read (on=None) or set switch.

    mode:
      diy  — unencrypted only
      lan  — AES encrypt only (needs devicekey)
      auto — detect: try LAN if devicekey present, else DIY; on failure try the
             other path. Caches working path per IP so subsequent calls are fast.

    Optional email/password + cloud_fallback: if LAN status has no switch/power,
    enrich from CoolKit cloud. Set cloud_fallback=False for a fast LAN-only probe
    (e.g. right after set).
    """
    ip = str(ip or "").strip()
    deviceid = str(deviceid or "").strip()
    port = int(port or 8081)
    mode = str(mode or "auto").lower().strip()
    if mode not in ("auto", "diy", "lan"):
        mode = "auto"
    devicekey = str(devicekey or "").strip() or None
    self_apikey = str(self_apikey or "").strip() or "123"
    email = str(email or "").strip() or None
    password = str(password or "") or None
    if not ip:
        raise ValueError("IP empty")
    if not deviceid:
        raise ValueError("deviceid empty")
    if mode == "lan" and not devicekey:
        raise ValueError("devicekey empty (LAN encrypt)")

    def _send(use_lan: bool, cmd: str, data: dict | None = None) -> dict:
        key = devicekey if use_lan else None
        if use_lan and not key:
            raise ValueError("devicekey empty (LAN encrypt)")
        return lan_send(
            ip,
            deviceid,
            devicekey=key,
            command=cmd,
            data=data if data is not None else {},
            port=port,
            timeout=timeout,
            self_apikey=self_apikey if key else "123",
        )

    def _paths() -> list[bool]:
        """Ordered list of use_lan flags to try."""
        if mode == "lan":
            return [True]
        if mode == "diy":
            return [False]
        # auto
        cached = _EWELINK_PROTO_CACHE.get(ip)
        order: list[bool] = []
        if cached == "lan" and devicekey:
            order = [True, False]
        elif cached == "diy":
            order = [False] + ([True] if devicekey else [])
        else:
            # prefer LAN when key known (cloud-paired plugs), else DIY
            if devicekey:
                order = [True, False]
            else:
                order = [False]
        # dedupe preserve order
        seen: set[bool] = set()
        out: list[bool] = []
        for u in order:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _remember(use_lan: bool) -> None:
        if mode == "auto":
            _EWELINK_PROTO_CACHE[ip] = "lan" if use_lan else "diy"

    # ── set ──────────────────────────────────────────────────────────────
    if on is not None:
        data_single = {"switch": "on" if on else "off"}
        data_multi = {
            "switches": [{"outlet": int(outlet), "switch": "on" if on else "off"}]
        }
        last_err: Exception | None = None
        for use_lan in _paths():
            for cmd, data in (("switch", data_single), ("switches", data_multi)):
                try:
                    resp = _send(use_lan, cmd, data)
                    if _ewelink_ok(resp):
                        _remember(use_lan)
                        return {
                            "on": bool(on),
                            "backend": "ewelink",
                            "mode": "lan" if use_lan else "diy",
                            "ip": ip,
                            "raw": resp,
                        }
                    last_err = RuntimeError(f"error={resp.get('error')}: {resp}")
                except Exception as e:
                    last_err = e
                    continue
        raise RuntimeError(f"eWeLink set failed: {last_err}")

    # ── status ───────────────────────────────────────────────────────────
    last_err = None
    best: dict[str, Any] | None = None  # prefer path that yields switch/power
    for use_lan in _paths():
        resp = None
        params: dict = {}
        # Order matters (SonoffLAN / local reverse-engineering):
        #  - statistics → POWR2/PSF-X67 (uiid 32) returns encrypted
        #    {voltage,current,power} over LAN (info/getState usually empty)
        #  - sledonline poke can refresh TH sensors
        #  - getState/info for switch
        for cmd, pdata in (
            ("statistics", {}),
            ("sledonline", {"sledOnline": "on"}),
            ("getState", {}),
            ("info", {}),
        ):
            try:
                resp = _send(use_lan, cmd, pdata)
            except Exception as e:
                last_err = e
                resp = None
                continue
            if not isinstance(resp, dict):
                continue
            data = resp.get("data_decrypted")
            if not isinstance(data, dict):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            # HTTP encrypt response may put ciphertext in data+iv without data_decrypted
            # if decrypt failed earlier — try again
            if (
                not isinstance(data, dict)
                and resp.get("data")
                and resp.get("iv")
                and use_lan
                and devicekey
            ):
                try:
                    data = decrypt_data(
                        str(resp["data"]), str(resp["iv"]), devicekey
                    )
                    resp["data_decrypted"] = data
                except Exception:
                    data = {}
            if isinstance(data, dict):
                params.update(data)
            # merge multi-outlet rows into flat actPow_xx if present
            sw_list = params.get("switches")
            if isinstance(sw_list, list):
                for it in sw_list:
                    if not isinstance(it, dict):
                        continue
                    oi = it.get("outlet")
                    for mk in ("current", "voltage", "actPow", "power"):
                        if mk in it and oi is not None:
                            params.setdefault(f"{mk}_{int(oi):02d}", it.get(mk))
            sw = parse_switch(params)
            power = parse_power_params(params)
            if sw is not None or power or _ewelink_ok(resp):
                _remember(use_lan)
                result = {
                    "on": sw,
                    "backend": "ewelink",
                    "mode": "lan" if use_lan else "diy",
                    "ip": ip,
                    "http": 200,
                    "params": params,
                    "power": power,
                    "fw": params.get("fwVersion") or params.get("fw_version"),
                    "raw_error": resp.get("error"),
                    "source": "lan-statistics"
                    if cmd == "statistics" and power
                    else ("lan" if use_lan else "diy"),
                    "raw": {
                        k: resp.get(k)
                        for k in ("error", "seq", "sequence")
                        if k in resp
                    },
                }
                # Prefer LAN power over continuing; still try getState for switch
                if power and sw is not None:
                    return result
                if power:
                    best = result
                    # keep going to pick up switch from getState/info
                    continue
                if sw is not None:
                    if best and best.get("power"):
                        best["on"] = sw
                        return best
                    return result
                if best is None:
                    best = result
        if best and best.get("power"):
            return best
        if not params and last_err is None and resp is None:
            continue
        if resp is not None and not _ewelink_ok(resp):
            last_err = RuntimeError(f"error={resp.get('error')}")

    # Cloud meter fallback only if LAN had no power (and caller allows it).
    # Prefer LAN POST /zeroconf/statistics for PSF-X67 / POWR2 (uiid 32).
    if cloud_fallback and email and password and not (
        best and isinstance(best.get("power"), dict) and best.get("power")
    ):
        try:
            cloud = cloud_device_live(
                email,
                password,
                deviceid,
                country_code=country_code or "+7",
                region=region,
            )
        except Exception as e:
            cloud = None
            last_err = e
        if isinstance(cloud, dict):
            power = cloud.get("power") if isinstance(cloud.get("power"), dict) else None
            sw = cloud.get("on")
            params = cloud.get("params") if isinstance(cloud.get("params"), dict) else {}
            # merge onto LAN best if any
            base = dict(best or {})
            if sw is not None:
                base["on"] = sw
            if power:
                base["power"] = power
            if params:
                base["params"] = {**(base.get("params") or {}), **params}
            base.setdefault("backend", "ewelink")
            base.setdefault("mode", (best or {}).get("mode") or "lan")
            base.setdefault("ip", ip)
            base["http"] = 200
            base["cloud"] = True
            if cloud.get("productModel"):
                base["productModel"] = cloud.get("productModel")
            if cloud.get("fw"):
                base["fw"] = cloud.get("fw")
            if cloud.get("rssi") is not None:
                base["rssi"] = cloud.get("rssi")
            if base.get("on") is not None or base.get("power"):
                return base
            if best is None:
                best = base

    if best is not None:
        return best
    raise RuntimeError(f"eWeLink status failed: {last_err}")


# Access-token cache for cloud meter fallback: email → {at, region, apikey, ts}
_CLOUD_AT_CACHE: dict[str, dict[str, Any]] = {}
_CLOUD_AT_TTL = 50 * 60  # seconds


def cloud_device_live(
    email: str,
    password: str,
    deviceid: str,
    *,
    country_code: str = "+7",
    region: str | None = None,
) -> dict[str, Any] | None:
    """
    CoolKit cloud snapshot for one device (switch + power/voltage/current).

    Used when LAN zeroconf returns empty data (common on some POW/PSF FW).
    Caches access token ~50 min per email.
    """
    email = str(email or "").strip()
    password = str(password or "")
    deviceid = str(deviceid or "").strip()
    if not email or not password or not deviceid:
        return None
    now = time.time()
    cached = _CLOUD_AT_CACHE.get(email.lower())
    auth = None
    if (
        isinstance(cached, dict)
        and cached.get("at")
        and (now - float(cached.get("ts") or 0)) < _CLOUD_AT_TTL
    ):
        auth = {
            "at": cached["at"],
            "region": cached.get("region") or "eu",
            "apikey": cached.get("apikey"),
        }
    else:
        try:
            auth = cloud_login(
                email, password, country_code=country_code, region=region
            )
            _CLOUD_AT_CACHE[email.lower()] = {
                "at": auth.get("at"),
                "region": auth.get("region"),
                "apikey": auth.get("apikey"),
                "ts": now,
            }
        except Exception as e:
            print(f"[ewelink] cloud_device_live login: {e}", flush=True)
            return None
    try:
        devices = cloud_list_devices(auth["at"], auth["region"])
    except Exception as e:
        # token expired → one retry
        _CLOUD_AT_CACHE.pop(email.lower(), None)
        print(f"[ewelink] cloud_device_live list: {e}", flush=True)
        return None
    for d in devices:
        if str(d.get("deviceid") or "") != deviceid:
            continue
        params = d.get("params") if isinstance(d.get("params"), dict) else {}
        return {
            "on": parse_switch(params),
            "power": parse_power_params(params),
            "params": params,
            "productModel": d.get("productModel"),
            "name": d.get("name"),
            "online": d.get("online"),
            "uiid": d.get("uiid"),
            "fw": params.get("fwVersion") or params.get("fw_version"),
            "rssi": params.get("rssi"),
            "source": "cloud",
        }
    return None


def login_and_find_device(
    email: str,
    password: str,
    *,
    deviceid: str | None = None,
    ip: str | None = None,
    country_code: str = "+7",
    region: str | None = None,
) -> dict[str, Any]:
    """
    Cloud login + auto-pick one device for LAN encrypt.

    Match order:
      1) exact deviceid
      2) exact LAN IP (when cloud reports it)
      3) single device in account
      4) single device that has a devicekey
    """
    auth = cloud_login(
        email, password, country_code=country_code, region=region
    )
    devices = cloud_list_devices(auth["at"], auth["region"])
    match = None
    did = str(deviceid or "").strip()
    tip = str(ip or "").strip()
    if did:
        for d in devices:
            if str(d.get("deviceid") or "") == did:
                match = d
                break
    if match is None and tip:
        for d in devices:
            if str(d.get("ip") or "").strip() == tip:
                match = d
                break
    if match is None and len(devices) == 1:
        match = devices[0]
    if match is None:
        with_key = [d for d in devices if d.get("devicekey")]
        if len(with_key) == 1:
            match = with_key[0]
    return {
        "ok": True,
        "region": auth["region"],
        "apikey": auth.get("apikey"),
        "devices": devices,
        "device_count": len(devices),
        "match": match,
        "at": auth["at"],  # caller may discard
    }
