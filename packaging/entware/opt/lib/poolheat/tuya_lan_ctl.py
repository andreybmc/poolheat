#!/usr/bin/env python3
"""
Tuya LAN one-shot control/status via tinytuya.

Used by Go devices-poller for protocol 3.5+ (and as fallback when Go
tuya-proto session fails). Stdout: one JSON object.

  python3 tuya_lan_ctl.py status  --ip IP --id DEV --key KEY --version 3.5 --dps 20
  python3 tuya_lan_ctl.py set     --ip IP --id DEV --key KEY --version 3.5 --dps 20 --on true
  python3 tuya_lan_ctl.py set     ... --brightness 500   # optional DPS bright (default 22)
"""
from __future__ import annotations

import argparse
import json
import sys


def _bool_arg(s: str) -> bool:
    v = str(s or "").strip().lower()
    if v in ("1", "true", "on", "yes", "y"):
        return True
    if v in ("0", "false", "off", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"bool expected, got {s!r}")


def _dps_bool(v) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return True
    if s in ("0", "false", "off", "no"):
        return False
    return None


def _pick_on(dps: dict, switch_dps: int) -> bool | None:
    if not isinstance(dps, dict):
        return None
    # configured first, then common light/switch indices
    for k in (str(switch_dps), "1", "20", "101", "102", "103", "2"):
        if k in dps:
            b = _dps_bool(dps.get(k))
            if b is not None:
                return b
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("status", "set"))
    ap.add_argument("--ip", required=True)
    ap.add_argument("--id", required=True, dest="device_id")
    ap.add_argument("--key", required=True)
    ap.add_argument("--version", type=float, default=3.4)
    ap.add_argument("--dps", type=int, default=1, help="switch DPS")
    ap.add_argument("--on", type=_bool_arg, default=None)
    ap.add_argument(
        "--brightness",
        type=int,
        default=None,
        help="brightness 0–100 (%%) or 0–1000 raw if >100",
    )
    ap.add_argument("--bright-dps", type=int, default=22)
    ap.add_argument(
        "--mode",
        type=str,
        default=None,
        help="work mode: white|colour|scene|music",
    )
    ap.add_argument("--mode-dps", type=int, default=21)
    ap.add_argument("--timeout", type=float, default=6.0)
    args = ap.parse_args()

    try:
        import tinytuya  # type: ignore
    except ImportError as e:
        print(json.dumps({"ok": False, "error": f"tinytuya missing: {e}"}))
        return 2

    ver = float(args.version or 3.4)
    # normalize common UI values
    if ver >= 3.45:
        ver = 3.5
    elif ver >= 3.35:
        ver = 3.4
    elif ver >= 3.2:
        ver = 3.3
    else:
        ver = 3.1

    try:
        dev = tinytuya.Device(
            args.device_id, args.ip, args.key, version=ver
        )
        dev.set_socketTimeout(float(args.timeout))
        dev.set_socketPersistent(False)

        if args.action == "status":
            st = dev.status() or {}
            if isinstance(st, dict) and st.get("Error"):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": str(st.get("Error")),
                            "err": st.get("Err"),
                            "backend": "tuya",
                        }
                    )
                )
                return 1
            dps = st.get("dps") if isinstance(st, dict) else None
            if not isinstance(dps, dict):
                dps = st if isinstance(st, dict) else {}
            on = _pick_on(dps, int(args.dps))
            out = {
                "ok": True,
                "on": on,
                "backend": "tuya",
                "dps": dps,
                "version": ver,
                "device_id": args.device_id,
            }
            # brightness if present (raw 0–1000)
            bk = str(args.bright_dps)
            if bk in dps:
                try:
                    out["brightness"] = int(dps[bk])
                    out["brightness_dps"] = int(args.bright_dps)
                except (TypeError, ValueError):
                    pass
            mk = str(args.mode_dps)
            if mk in dps and dps[mk] is not None:
                out["mode"] = str(dps[mk])
                out["mode_dps"] = int(args.mode_dps)
            print(json.dumps(out, ensure_ascii=False))
            return 0

        # set
        if args.on is None and args.brightness is None and not args.mode:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "set requires --on and/or --brightness and/or --mode",
                    }
                )
            )
            return 2

        last = None
        used_dps = int(args.dps)
        dps_try = [used_dps]
        # lights/dimmers often use 20 for switch when UI still has default 1
        if used_dps == 1:
            dps_try.append(20)

        if args.on is not None:
            want = bool(args.on)
            matched = False
            for sdps in dps_try:
                for val in (want, (1 if want else 0)):
                    try:
                        last = dev.set_value(int(sdps), val)
                    except Exception as e:
                        last = {"error": str(e)}
                        continue
                    st = dev.status() or {}
                    dps = st.get("dps") if isinstance(st, dict) else {}
                    if not isinstance(dps, dict):
                        dps = {}
                    on = _pick_on(dps, int(sdps))
                    if on is None:
                        on = _pick_on(dps, 20 if sdps != 20 else 1)
                    if on is not None and on == want:
                        used_dps = int(sdps)
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                st = dev.status() or {}
                dps = st.get("dps") if isinstance(st, dict) else {}
                if not isinstance(dps, dict):
                    dps = {}
                on = _pick_on(dps, used_dps)
        else:
            st = dev.status() or {}
            dps = st.get("dps") if isinstance(st, dict) else {}
            if not isinstance(dps, dict):
                dps = {}
            on = _pick_on(dps, used_dps)

        if args.mode:
            mode = str(args.mode).strip().lower()
            # normalize colour spelling
            if mode in ("color", "colour"):
                mode = "colour"
            if mode in ("white", "colour", "scene", "music"):
                try:
                    last = dev.set_value(int(args.mode_dps), mode)
                except Exception as e:
                    last = {"error": f"mode: {e}"}
                st = dev.status() or {}
                dps = st.get("dps") if isinstance(st, dict) else {}
                if not isinstance(dps, dict):
                    dps = {}

        if args.brightness is not None:
            raw_in = int(args.brightness)
            # accept 0–100 % or raw 0–1000
            if raw_in <= 100:
                raw = int(round(raw_in * 10))  # 0–1000
            else:
                raw = max(0, min(1000, raw_in))
            last = dev.set_value(int(args.bright_dps), raw)
            st = dev.status() or {}
            dps = st.get("dps") if isinstance(st, dict) else {}
            if not isinstance(dps, dict):
                dps = {}
            if on is None:
                on = _pick_on(dps, used_dps)

        out = {
            "ok": True,
            "on": on if on is not None else args.on,
            "backend": "tuya",
            "dps": dps,
            "version": ver,
            "device_id": args.device_id,
            "switch_dps": used_dps,
            "set_raw": last,
        }
        bk = str(args.bright_dps)
        if bk in dps:
            try:
                out["brightness"] = int(dps[bk])
            except (TypeError, ValueError):
                pass
        elif args.brightness is not None:
            out["brightness"] = (
                int(args.brightness) * 10
                if int(args.brightness) <= 100
                else int(args.brightness)
            )
        mk = str(args.mode_dps)
        if mk in dps and dps[mk] is not None:
            out["mode"] = str(dps[mk])
        elif args.mode:
            out["mode"] = str(args.mode).strip().lower()
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "backend": "tuya"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
