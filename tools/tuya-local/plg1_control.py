#!/usr/bin/env python3
"""
LAN-only control for FinePower PLG-1 (Tuya protocol 3.4).
No cloud calls — only TCP 6668 to the plug.

Usage:
  cp plg1.env.example plg1.env   # fill TUYA_LOCAL_KEY
  python3 plg1_control.py status
  python3 plg1_control.py on
  python3 plg1_control.py off
  python3 plg1_control.py toggle
  python3 plg1_control.py discover   # id/version from LAN (no key)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import tinytuya
except ImportError:
    print("pip3 install tinytuya", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "plg1.env"


def load_env(path: Path = ENV_FILE) -> dict:
    cfg = {
        "TUYA_IP": "10.1.30.40",
        "TUYA_DEVICE_ID": "bf60efac4222a06088bew9",
        "TUYA_LOCAL_KEY": "",
        "TUYA_VERSION": "3.4",
    }
    # env vars override file
    for k in list(cfg.keys()):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in cfg:
                cfg[k] = v
    return cfg


def discover(ip: str) -> dict:
    """Broadcast/query device on LAN — no local_key needed."""
    info = tinytuya.find_device(address=ip)
    return info or {}


def device(cfg: dict) -> tinytuya.OutletDevice:
    key = (cfg.get("TUYA_LOCAL_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "TUYA_LOCAL_KEY empty. Put it in plg1.env (see KEY_EXTRACT.md)."
        )
    ver = float(cfg.get("TUYA_VERSION") or 3.4)
    d = tinytuya.OutletDevice(
        cfg["TUYA_DEVICE_ID"].strip(),
        cfg["TUYA_IP"].strip(),
        key,
        version=ver,
    )
    d.set_socketTimeout(5)
    d.set_socketPersistent(False)
    return d


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    cfg = load_env()

    if cmd in ("discover", "find", "scan"):
        ip = cfg["TUYA_IP"]
        print(f"discover {ip} …")
        info = discover(ip)
        print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
        if isinstance(info, dict) and info.get("id"):
            print(
                f"\n# suggested plg1.env lines:\n"
                f"TUYA_IP={info.get('ip') or ip}\n"
                f"TUYA_DEVICE_ID={info.get('id')}\n"
                f"TUYA_VERSION={info.get('version') or 3.4}\n"
                f"TUYA_LOCAL_KEY=   # fill after extract\n"
            )
        return 0

    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return 0

    d = device(cfg)

    if cmd == "status":
        st = d.status()
        print(json.dumps(st, indent=2, ensure_ascii=False, default=str))
        if isinstance(st, dict) and st.get("Error"):
            print(
                "\nHint: Err 914 = bad local_key or version. "
                "Re-extract key; do not re-pair in Smart Life without re-extract.",
                file=sys.stderr,
            )
            return 2
        return 0

    if cmd in ("on", "1", "true"):
        print(d.turn_on())
        return 0

    if cmd in ("off", "0", "false"):
        print(d.turn_off())
        return 0

    if cmd == "toggle":
        st = d.status()
        dps = (st or {}).get("dps") or {}
        # switch is usually dps "1"
        cur = dps.get("1")
        if cur is True or cur == 1 or str(cur).lower() in ("true", "on"):
            print("was on → off", d.turn_off())
        else:
            print("was off/unknown → on", d.turn_on())
        return 0

    if cmd.startswith("set:"):
        # set:1=true  or set:1=false
        body = cmd[4:]
        k, _, v = body.partition("=")
        val: object
        if v.lower() in ("true", "1", "on"):
            val = True
        elif v.lower() in ("false", "0", "off"):
            val = False
        else:
            try:
                val = int(v)
            except ValueError:
                val = v
        print(d.set_value(k, val))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
