#!/usr/bin/env python3
"""
User-friendly Tuya / Smart Life plug driver (FinePower PLG-1 and similar).

Auth modes (pick one):
  1) local_key  — pure LAN TCP:6668 (best, no cloud after key known)
  2) openapi    — Access ID + Secret from iot.tuya.com (pulls local_key once)
  3) skill      — email + password via legacy HA skill API
                (cloud on/off only; often NO local_key; may be blocked for new accounts)

Usage:
  export TUYA_EMAIL=...
  export TUYA_PASSWORD=...
  export TUYA_COUNTRY=7          # optional, default auto
  python3 tuya_plug.py login     # skill login + list devices
  python3 tuya_plug.py devices

  export TUYA_LOCAL_KEY=...
  python3 tuya_plug.py status
  python3 tuya_plug.py on
  python3 tuya_plug.py off

  export TUYA_API_KEY=...
  export TUYA_API_SECRET=...
  export TUYA_API_REGION=eu
  python3 tuya_plug.py sync-keys   # OpenAPI → devices.json + plg1.env

Never commit secrets. Use env or plg1.env (gitignored).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "plg1.env"
DEVICES_FILE = HERE / "devices.json"

# Defaults from LAN discovery of FinePower PLG-1
DEFAULTS = {
    "TUYA_IP": "10.1.30.40",
    "TUYA_DEVICE_ID": "bf60efac4222a06088bew9",
    "TUYA_LOCAL_KEY": "",
    "TUYA_VERSION": "3.4",
    "TUYA_EMAIL": "",
    "TUYA_PASSWORD": "",
    "TUYA_COUNTRY": "",  # empty = auto try
    "TUYA_BIZ": "tuya",  # tuya | smart_life | ""
    "TUYA_API_KEY": "",
    "TUYA_API_SECRET": "",
    "TUYA_API_REGION": "eu",
}


def load_cfg() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg.keys()):
        if os.environ.get(k) is not None:
            cfg[k] = os.environ[k]
    return cfg


def save_env_key(local_key: str, device_id: str | None = None, ip: str | None = None) -> None:
    cfg = load_cfg()
    if device_id:
        cfg["TUYA_DEVICE_ID"] = device_id
    if ip:
        cfg["TUYA_IP"] = ip
    cfg["TUYA_LOCAL_KEY"] = local_key
    lines = [
        "# auto-updated by tuya_plug.py — do not commit",
        f"TUYA_IP={cfg['TUYA_IP']}",
        f"TUYA_DEVICE_ID={cfg['TUYA_DEVICE_ID']}",
        f"TUYA_LOCAL_KEY={cfg['TUYA_LOCAL_KEY']}",
        f"TUYA_VERSION={cfg.get('TUYA_VERSION') or '3.4'}",
        "",
    ]
    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    print(f"wrote {ENV_FILE} (mode 600)")


# ── Skill (email/password) ──────────────────────────────────────────────────

SKILL_REGIONS = ("eu", "us", "cn", "in")
SKILL_COUNTRIES = ("7", "1", "44", "49", "33", "380", "375", "86", "61")


def _skill_auth_urls(region: str) -> str:
    if region == "cn":
        return "https://px1.tuyacn.com/homeassistant/auth.do"
    return f"https://px1.tuya{region}.com/homeassistant/auth.do"


def skill_login(
    email: str,
    password: str,
    *,
    country: str | None = None,
    biz: str = "tuya",
) -> dict[str, Any]:
    """
    Legacy Home Assistant skill login.
    Returns {access_token, refresh_token, region, expireTime, raw}.
    Raises RuntimeError with friendly message on failure.
    """
    import requests

    countries = [country] if country else list(SKILL_COUNTRIES)
    passwords = [password, hashlib.md5(password.encode("utf-8")).hexdigest()]
    bizs = [biz, "tuya", "smart_life", ""] if biz else ["tuya", "smart_life", ""]
    last_err = "unknown"
    rate_limited = False

    for region in SKILL_REGIONS:
        for cc in countries:
            if not cc:
                continue
            for b in bizs:
                for pw in passwords:
                    url = _skill_auth_urls(region)
                    try:
                        r = requests.post(
                            url,
                            data={
                                "userName": email,
                                "password": pw,
                                "countryCode": cc,
                                "bizType": b or "",
                                "from": "tuya",
                            },
                            timeout=20,
                        )
                        j = r.json()
                    except Exception as e:
                        last_err = str(e)
                        continue
                    if j.get("access_token") or j.get("accessToken"):
                        tok = j.get("access_token") or j.get("accessToken")
                        # region from token prefix
                        reg = region
                        if isinstance(tok, str) and len(tok) >= 2:
                            pref = tok[:2].upper()
                            if pref == "AY":
                                reg = "cn"
                            elif pref == "EU":
                                reg = "eu"
                            elif pref in ("US", "AZ"):
                                reg = "us"
                        return {
                            "access_token": tok,
                            "refresh_token": j.get("refresh_token") or j.get("refreshToken"),
                            "expires_in": j.get("expires_in"),
                            "region": reg,
                            "country": cc,
                            "biz": b,
                            "raw": j,
                        }
                    err = j.get("errorMsg") or j.get("msg") or j.get("error") or str(j)
                    last_err = str(err)
                    if "180" in last_err or "exceed" in last_err.lower():
                        rate_limited = True
                        time.sleep(3)
                    # wrong password — try next combo slowly
    if rate_limited:
        raise RuntimeError(
            f"Tuya skill API rate-limited (try again in ~3 min). Last: {last_err}"
        )
    raise RuntimeError(
        f"Skill login failed (email/password or country/app mismatch). Last: {last_err}\n"
        "Tips: use the same app account (Smart Life vs Tuya Smart); "
        "set TUYA_COUNTRY (7=RU, 1=US, 44=UK); "
        "or use OpenAPI (TUYA_API_KEY/SECRET) / paste local_key."
    )


def skill_discover(access_token: str, region: str) -> list[dict]:
    import requests

    if region == "cn":
        base = "https://px1.tuyacn.com"
    else:
        base = f"https://px1.tuya{region}.com"
    url = f"{base}/homeassistant/skill"
    header = {
        "name": "Discovery",
        "namespace": "discovery",
        "payloadVersion": 1,
    }
    payload = {"accessToken": access_token}
    body = {"header": header, "payload": payload}
    r = requests.post(url, json=body, timeout=25)
    j = r.json()
    if (j.get("header") or {}).get("code") != "SUCCESS":
        raise RuntimeError(f"discovery failed: {j}")
    return list((j.get("payload") or {}).get("devices") or [])


def skill_control(access_token: str, region: str, dev_id: str, turn_on: bool) -> dict:
    import requests

    if region == "cn":
        base = "https://px1.tuyacn.com"
    else:
        base = f"https://px1.tuya{region}.com"
    url = f"{base}/homeassistant/skill"
    header = {
        "name": "turnOnOff",
        "namespace": "control",
        "payloadVersion": 1,
    }
    payload = {
        "accessToken": access_token,
        "devId": dev_id,
        "value": "1" if turn_on else "0",
    }
    r = requests.post(url, json={"header": header, "payload": payload}, timeout=25)
    return r.json()


# ── OpenAPI (Access ID / Secret) ────────────────────────────────────────────

def openapi_sync_keys(cfg: dict[str, str]) -> list[dict]:
    """Use tinytuya.Cloud to list devices with local_key."""
    import tinytuya

    key = cfg.get("TUYA_API_KEY") or ""
    secret = cfg.get("TUYA_API_SECRET") or ""
    region = cfg.get("TUYA_API_REGION") or "eu"
    if not key or not secret:
        raise RuntimeError("Set TUYA_API_KEY and TUYA_API_SECRET (iot.tuya.com project)")
    cloud = tinytuya.Cloud(
        apiRegion=region,
        apiKey=key,
        apiSecret=secret,
    )
    devs = cloud.getdevices()
    if not isinstance(devs, list):
        # some versions return dict with error
        raise RuntimeError(f"getdevices failed: {devs}")
    out = []
    for d in devs:
        if not isinstance(d, dict):
            continue
        out.append(
            {
                "id": d.get("id") or d.get("dev_id"),
                "name": d.get("name"),
                "key": d.get("key") or d.get("local_key"),
                "mac": d.get("mac"),
                "uuid": d.get("uuid"),
                "product_id": d.get("product_id") or d.get("productId"),
                "category": d.get("category"),
                "raw": d,
            }
        )
    DEVICES_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {DEVICES_FILE} ({len(out)} devices)")
    # match known plug
    want = cfg.get("TUYA_DEVICE_ID") or ""
    for d in out:
        if want and d.get("id") == want and d.get("key"):
            save_env_key(str(d["key"]), device_id=str(d["id"]))
            break
    else:
        # first with key
        for d in out:
            if d.get("key"):
                print(f"hint: first device with key: {d.get('name')} id={d.get('id')}")
                break
    return out


# ── Local LAN ───────────────────────────────────────────────────────────────

def local_device(cfg: dict[str, str]):
    import tinytuya

    key = (cfg.get("TUYA_LOCAL_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "No TUYA_LOCAL_KEY. Run: sync-keys (OpenAPI) or set key in plg1.env after extract."
        )
    d = tinytuya.OutletDevice(
        cfg["TUYA_DEVICE_ID"].strip(),
        cfg["TUYA_IP"].strip(),
        key,
        version=float(cfg.get("TUYA_VERSION") or 3.4),
    )
    d.set_socketTimeout(5)
    return d


def local_discover(ip: str) -> dict:
    import tinytuya

    return tinytuya.find_device(address=ip) or {}


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_login(cfg: dict[str, str]) -> int:
    email = cfg.get("TUYA_EMAIL") or ""
    password = cfg.get("TUYA_PASSWORD") or ""
    if not email or not password:
        print(
            "Set TUYA_EMAIL and TUYA_PASSWORD (env or plg1.env).\n"
            "Example:\n  export TUYA_EMAIL='you@mail.com'\n  export TUYA_PASSWORD='...'\n"
            "  export TUYA_COUNTRY=7\n  python3 tuya_plug.py login",
            file=sys.stderr,
        )
        return 2
    print("Skill login (email/password)…")
    try:
        auth = skill_login(
            email,
            password,
            country=(cfg.get("TUYA_COUNTRY") or None) or None,
            biz=cfg.get("TUYA_BIZ") or "tuya",
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(
            "\nNote: legacy skill API often rejects modern accounts.\n"
            "User-friendly fallback:\n"
            "  1) iot.tuya.com → Access ID/Secret →  python3 tuya_plug.py sync-keys\n"
            "  2) or paste local_key into plg1.env → status/on/off (pure LAN)\n",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK region={auth['region']} country={auth['country']} biz={auth.get('biz')!r}"
    )
    # cache token (not the password)
    tok_path = HERE / "skill_token.json"
    tok_path.write_text(
        json.dumps(
            {
                "access_token": auth["access_token"],
                "refresh_token": auth.get("refresh_token"),
                "region": auth["region"],
                "ts": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(tok_path, 0o600)
    except OSError:
        pass
    print(f"token → {tok_path}")
    try:
        devs = skill_discover(auth["access_token"], auth["region"])
    except Exception as e:
        print(f"discover error: {e}", file=sys.stderr)
        return 1
    print(f"devices: {len(devs)}")
    for d in devs:
        # skill payload rarely includes local_key
        name = d.get("name") or d.get("customName") or "?"
        did = d.get("id") or d.get("devId") or "?"
        print(f"  - {name}  id={did}  keys={list(d.keys())}")
        if d.get("local_key") or d.get("localKey"):
            lk = d.get("local_key") or d.get("localKey")
            print(f"    local_key found → saving")
            save_env_key(str(lk), device_id=str(did))
    if not any(d.get("local_key") or d.get("localKey") for d in devs):
        print(
            "\nSkill API did not return local_key (typical).\n"
            "Cloud on/off still possible:  python3 tuya_plug.py cloud-on|cloud-off\n"
            "For pure LAN: use OpenAPI sync-keys or manual local_key."
        )
    (HERE / "skill_devices.json").write_text(
        json.dumps(devs, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return 0


def _load_skill_token() -> dict:
    p = HERE / "skill_token.json"
    if not p.is_file():
        raise RuntimeError("No skill_token.json — run: python3 tuya_plug.py login")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_cloud_switch(cfg: dict[str, str], on: bool) -> int:
    tok = _load_skill_token()
    dev_id = cfg["TUYA_DEVICE_ID"]
    r = skill_control(tok["access_token"], tok["region"], dev_id, on)
    print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    ok = (r.get("header") or {}).get("code") == "SUCCESS"
    return 0 if ok else 1


def cmd_status(cfg: dict[str, str]) -> int:
    d = local_device(cfg)
    st = d.status()
    print(json.dumps(st, indent=2, ensure_ascii=False, default=str))
    if isinstance(st, dict) and st.get("Error"):
        return 2
    return 0


def cmd_on_off(cfg: dict[str, str], on: bool) -> int:
    d = local_device(cfg)
    print(d.turn_on() if on else d.turn_off())
    return 0


def cmd_discover(cfg: dict[str, str]) -> int:
    info = local_discover(cfg["TUYA_IP"])
    print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "help").lower()
    cfg = load_cfg()

    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if cmd == "login":
        return cmd_login(cfg)
    if cmd == "discover":
        return cmd_discover(cfg)
    if cmd == "sync-keys":
        try:
            openapi_sync_keys(cfg)
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    if cmd == "status":
        try:
            return cmd_status(cfg)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    if cmd in ("on", "off"):
        try:
            return cmd_on_off(cfg, cmd == "on")
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    if cmd == "cloud-on":
        return cmd_cloud_switch(cfg, True)
    if cmd == "cloud-off":
        return cmd_cloud_switch(cfg, False)
    if cmd == "devices":
        if DEVICES_FILE.is_file():
            print(DEVICES_FILE.read_text(encoding="utf-8"))
            return 0
        if (HERE / "skill_devices.json").is_file():
            print((HERE / "skill_devices.json").read_text(encoding="utf-8"))
            return 0
        print("No devices cache. Run login or sync-keys.", file=sys.stderr)
        return 1

    print(f"unknown command: {cmd}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
