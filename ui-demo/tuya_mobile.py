#!/usr/bin/env python3
"""
Tuya / Smart Life mobile API — login with email/password → local_key list.

Based on APK RE (com.tuya.smart 7.9.3) + community keys (Smart Life).
Same flow as projects/smartlife/tuya_local_socket/get_local_key.py
Uses only stdlib (urllib) so it runs on Entware without requests.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional

# Smart Life app (community keys)
SMART_LIFE = {
    "name": "Smart Life",
    "key": "ekmnwp9f5pnh3trdtpgy",
    "secret": "r3me7ghmxjevrvnpemwmhw3fxtacphyg",
    "secret2": "jfg5rs5kkmrj5mxahugvucrsvw43t48x",
    "cert": "0F:C3:61:99:9C:C0:C3:5B:A8:AC:A5:7D:AA:55:93:A2:0C:F5:57:27:70:2E:A8:5A:D7:B3:22:89:49:F8:88:FE",
    "ttid": "smart_life",
}

# Tuya Smart 7.9.3 (from APK)
TUYA_SMART_793 = {
    "name": "Tuya Smart 7.9.3",
    "key": "3cxxt3au9x33ytvq3h9j",
    "secret": "5gdtanjtf38vyxkqh87cjwfcqjhvjjqa",
    "secret2": "f3hd7pet4p83kemjdf5wqsa5tavrv579",
    "cert": "93:21:9F:C2:73:E2:20:0F:4A:DE:E5:F7:19:1D:C6:56:BA:2A:2D:7B:2F:F5:D2:4C:D5:5C:4B:61:55:00:1E:40",
    "ttid": "tuyaSmart",
}

# Older Tuya Smart
TUYA_SMART_CLASSIC = {
    "name": "Tuya Smart classic",
    "key": "3fjrekuxank9eaej3gcx",
    "secret": "aq7xvqcyqcnegvew793pqjmhv77rneqc",
    "secret2": "vay9g59g9g99qf3rtqptmc3emhkanwkx",
    "cert": "93:21:9F:C2:73:E2:20:0F:4A:DE:E5:F7:19:1D:C6:56:BA:2A:2D:7B:2F:F5:D2:4C:D5:5C:4B:61:55:00:1E:40",
    "ttid": "tuya",
}

KEYSETS: dict[str, dict] = {
    "smartlife": SMART_LIFE,
    "tuya": TUYA_SMART_793,
    "793": TUYA_SMART_793,
    "classic": TUYA_SMART_CLASSIC,
}

REGIONS: dict[str, str] = {
    "us": "https://a1.tuyaus.com/api.json",
    "eu": "https://a1.tuyaeu.com/api.json",
    "cn": "https://a1.tuyacn.com/api.json",
    "in": "https://a1.tuyain.com/api.json",
}


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def mobile_hash(data: str) -> str:
    pre = hashlib.md5(data.encode("utf-8")).hexdigest()
    return pre[8:16] + pre[0:8] + pre[24:32] + pre[16:24]


def rsa_encrypt_no_padding(n_decimal: str, e: int, plaintext: bytes) -> str:
    n = int(n_decimal)
    k = (n.bit_length() + 7) // 8
    if len(plaintext) > k:
        raise ValueError("plaintext too long for RSA modulus")
    m = int.from_bytes(plaintext.rjust(k, b"\x00"), "big")
    c = pow(m, int(e), n)
    return format(c, f"0{2 * k}x")


def _http_get_json(url: str, timeout: float = 30.0) -> dict:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "poolheat-tuya/1.0 (SmartLife mobile API)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("unexpected mobile API response")
    return data


class TuyaMobileAPI:
    def __init__(self, keys: dict, endpoint: str, timeout: float = 30.0):
        self.keys = keys
        self.endpoint = endpoint
        self.timeout = timeout
        self.hmac_key = f"{keys['cert']}_{keys['secret2']}_{keys['secret']}"
        self.sid: Optional[str] = None

    def _sign(self, pairs: dict) -> str:
        values = {
            "a", "v", "lat", "lon", "lang", "deviceId", "imei", "imsi",
            "appVersion", "ttid", "isH5", "h5Token", "os", "clientId",
            "postData", "time", "requestId", "n4h5", "sid", "sp", "et",
        }
        parts = []
        for k in sorted(pairs.keys()):
            if k not in values or pairs[k] in (None, ""):
                continue
            if k == "postData":
                parts.append(f"{k}={mobile_hash(pairs[k])}")
            else:
                parts.append(f"{k}={pairs[k]}")
        return hmac.new(
            self.hmac_key.encode(),
            "||".join(parts).encode(),
            hashlib.sha256,
        ).hexdigest()

    def request(
        self,
        action: str,
        data: Any = None,
        *,
        sid: Optional[str] = None,
        gid: Any = None,
        requires_sid: bool = False,
    ) -> dict:
        if requires_sid and not (sid or self.sid):
            raise RuntimeError("sid required — login first")
        params: dict = {
            "a": action,
            "deviceId": "android_" + uuid.uuid4().hex[:16],
            "os": "Android",
            "lang": "en",
            "v": "1.0",
            "clientId": self.keys["key"],
            "time": str(int(__import__("time").time())),
            "et": "0.0.1",
            "ttid": self.keys["ttid"],
            "appVersion": "7.9.3",
            "appRnVersion": "5.11",
            "platform": "Android",
            "requestId": str(uuid.uuid4()),
        }
        if data is not None:
            params["postData"] = json.dumps(data, separators=(",", ":"))
        use_sid = sid if sid is not None else self.sid
        if use_sid:
            params["sid"] = use_sid
        if gid is not None:
            params["gid"] = str(gid)
        params["sign"] = self._sign(params)
        qs = urllib.parse.urlencode(params)
        url = self.endpoint + ("&" if "?" in self.endpoint else "?") + qs
        # endpoint already is .../api.json — use ? for first param
        if self.endpoint.endswith("api.json"):
            url = self.endpoint + "?" + qs
        try:
            return _http_get_json(url, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from e

    def login(self, email: str, password: str, country_code: str) -> dict:
        tok = self.request(
            "tuya.m.user.email.token.create",
            {"countryCode": str(country_code), "email": email},
        )
        if not tok.get("success"):
            raise RuntimeError(
                f"token.create failed: {tok.get('errorCode')} {tok.get('errorMsg')}"
            )
        res = tok["result"]
        enc = rsa_encrypt_no_padding(
            res["publicKey"],
            int(res.get("exponent", 3)),
            md5_hex(password).encode("utf-8"),
        )
        login = self.request(
            "tuya.m.user.email.password.login",
            {
                "countryCode": str(country_code),
                "email": email,
                "passwd": enc,
                "ifencrypt": 1,
                "options": {"group": 1},
                "token": res["token"],
            },
        )
        if not login.get("success"):
            raise RuntimeError(
                f"login failed: {login.get('errorCode')} {login.get('errorMsg')}"
            )
        result = login["result"]
        self.sid = result["sid"]
        domain = result.get("domain") or {}
        if domain.get("mobileApiUrl"):
            self.endpoint = domain["mobileApiUrl"].rstrip("/") + "/api.json"
        return result

    def list_devices(self) -> list[dict]:
        homes = self.request("tuya.m.location.list", requires_sid=True)
        if not homes.get("success"):
            raise RuntimeError(
                f"location.list failed: {homes.get('errorCode')} {homes.get('errorMsg')}"
            )
        out: list[dict] = []
        for g in homes.get("result") or []:
            gid = g.get("groupId") or g.get("homeId") or g.get("id")
            home_name = g.get("name")
            devs = self.request(
                "tuya.m.my.group.device.list",
                requires_sid=True,
                gid=gid,
            )
            if not devs.get("success"):
                continue
            arr = devs["result"]
            if isinstance(arr, dict):
                arr = arr.get("devices") or arr.get("list") or []
            for d in arr or []:
                if not isinstance(d, dict):
                    continue
                d = dict(d)
                d["_home"] = home_name
                d["_gid"] = gid
                out.append(d)
        return out


def fetch_devices_with_keys(
    email: str,
    password: str,
    *,
    country: str = "7",
    region: str = "eu",
    ecosystem: str = "smartlife",
) -> list[dict]:
    """
    Login + list devices with local_key.
    Returns slim list: [{name, id, key, product_id, home}, ...]
    """
    eco = str(ecosystem or "smartlife").strip().lower()
    if eco in ("smart_life", "sl"):
        eco = "smartlife"
    if eco in ("tuya_smart", "tuyasmart"):
        eco = "tuya"
    keys = KEYSETS.get(eco) or KEYSETS["smartlife"]
    reg = str(region or "eu").strip().lower()
    if reg not in REGIONS:
        reg = "eu"
    endpoint = REGIONS[reg]
    api = TuyaMobileAPI(keys, endpoint)
    api.login(str(email).strip(), str(password), str(country or "7").strip())
    devices = api.list_devices()
    slim: list[dict] = []
    for d in devices:
        slim.append(
            {
                "name": d.get("name"),
                "id": d.get("devId") or d.get("id"),
                "key": d.get("localKey") or d.get("local_key"),
                "product_id": d.get("productId") or d.get("product_id"),
                "uuid": d.get("uuid"),
                "mac": d.get("mac") or d.get("macAddress"),
                "home": d.get("_home"),
                "online": d.get("isOnline") if "isOnline" in d else d.get("online"),
                "category": d.get("category"),
            }
        )
    return slim


def ecosystems() -> list[dict]:
    return [
        {"id": "smartlife", "label": "Smart Life", "name": SMART_LIFE["name"]},
        {"id": "tuya", "label": "Tuya Smart", "name": TUYA_SMART_793["name"]},
        {"id": "classic", "label": "Tuya Smart (classic)", "name": TUYA_SMART_CLASSIC["name"]},
    ]
