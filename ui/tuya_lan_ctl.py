#!/usr/bin/env python3
"""
Tuya LAN control/status via tinytuya.

Go devices-poller keeps one --daemon process (import tinytuya once).
CLI one-shot still works for debug.

  python3 tuya_lan_ctl.py --daemon
  python3 tuya_lan_ctl.py status --ip IP --id DEV --key KEY --version 3.5 --dps 20
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


def _as_bool(v):
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


def _pick_on(dps: dict, switch_dps: int):
    if not isinstance(dps, dict):
        return None
    for k in (str(switch_dps), "1", "20", "101", "102", "103", "2"):
        if k in dps:
            b = _as_bool(dps.get(k))
            if b is not None:
                return b
    return None


def _norm_ver(v) -> float:
    try:
        ver = float(v or 3.4)
    except (TypeError, ValueError):
        ver = 3.4
    if ver >= 3.45:
        return 3.5
    if ver >= 3.35:
        return 3.4
    if ver >= 3.2:
        return 3.3
    return 3.1


def _status_dps(st):
    if isinstance(st, dict) and st.get("Error"):
        return None, str(st.get("Error")), st
    dps = st.get("dps") if isinstance(st, dict) else None
    if not isinstance(dps, dict):
        dps = st if isinstance(st, dict) else {}
    return dps, None, st


def run_request(req: dict) -> dict:
    try:
        import tinytuya  # type: ignore
    except ImportError as e:
        return {"ok": False, "error": f"tinytuya missing: {e}"}

    action = str(req.get("action") or "status").strip().lower()
    ip = str(req.get("ip") or "").strip()
    device_id = str(req.get("id") or req.get("device_id") or "").strip()
    key = str(req.get("key") or "").strip()
    if not ip or not device_id or not key:
        return {"ok": False, "error": "ip/id/key required"}

    ver = _norm_ver(req.get("version"))
    switch_dps = int(req.get("dps") or 1)
    bright_dps = int(req.get("bright_dps") or 22)
    mode_dps = int(req.get("mode_dps") or 21)
    timeout = float(req.get("timeout") or 4.0)
    if timeout < 1.5:
        timeout = 1.5
    if timeout > 12:
        timeout = 12

    want_on = _as_bool(req.get("on")) if "on" in req and req.get("on") is not None else None
    brightness = req.get("brightness")
    mode = req.get("mode")

    dev = tinytuya.Device(device_id, ip, key, version=ver)
    dev.set_socketTimeout(timeout)
    dev.set_socketPersistent(False)

    if action == "status":
        dps, err, st = _status_dps(dev.status() or {})
        if err:
            return {
                "ok": False,
                "error": err,
                "err": st.get("Err") if isinstance(st, dict) else None,
                "backend": "tuya",
            }
        out = {
            "ok": True,
            "on": _pick_on(dps, switch_dps),
            "backend": "tuya",
            "dps": dps,
            "version": ver,
            "device_id": device_id,
        }
        bk = str(bright_dps)
        if bk in dps:
            try:
                out["brightness"] = int(dps[bk])
                out["brightness_dps"] = bright_dps
            except (TypeError, ValueError):
                pass
        mk = str(mode_dps)
        if mk in dps and dps[mk] is not None:
            out["mode"] = str(dps[mk])
            out["mode_dps"] = mode_dps
        return out

    if action != "set":
        return {"ok": False, "error": f"unknown action {action}"}
    if want_on is None and brightness is None and not mode:
        return {"ok": False, "error": "set requires on and/or brightness and/or mode"}

    last = None
    used_dps = switch_dps
    dps: dict = {}
    on = None

    if want_on is not None:
        # one DPS, bool first — do not scan 1+20 × bool+int (that blew the Go budget)
        tries = [(used_dps, want_on)]
        if used_dps == 1:
            tries.append((20, want_on))
        matched = False
        for sdps, val in tries:
            try:
                last = dev.set_value(int(sdps), val)
            except Exception as e:
                last = {"error": str(e)}
                continue
            dps, err, _st = _status_dps(dev.status() or {})
            if err or not isinstance(dps, dict):
                dps = dps if isinstance(dps, dict) else {}
                continue
            on = _pick_on(dps, int(sdps)) or _pick_on(dps, 20 if sdps != 20 else 1)
            if on is not None and on == want_on:
                used_dps = int(sdps)
                matched = True
                break
        if not matched:
            dps, _err, _st = _status_dps(dev.status() or {})
            if not isinstance(dps, dict):
                dps = {}
            on = _pick_on(dps, used_dps)

    if mode:
        mode_s = str(mode).strip().lower()
        if mode_s == "color":
            mode_s = "colour"
        if mode_s in ("white", "colour", "scene", "music"):
            try:
                last = dev.set_value(int(mode_dps), mode_s)
            except Exception as e:
                last = {"error": f"mode: {e}"}
            dps, _err, _st = _status_dps(dev.status() or {})
            if not isinstance(dps, dict):
                dps = {}

    if brightness is not None:
        raw_in = int(brightness)
        raw = int(round(raw_in * 10)) if raw_in <= 100 else max(0, min(1000, raw_in))
        last = dev.set_value(int(bright_dps), raw)
        dps, _err, _st = _status_dps(dev.status() or {})
        if not isinstance(dps, dict):
            dps = {}
        if on is None:
            on = _pick_on(dps, used_dps)

    if want_on is None and brightness is None and not mode:
        pass
    elif not dps and want_on is None:
        dps, _err, _st = _status_dps(dev.status() or {})
        if not isinstance(dps, dict):
            dps = {}

    out = {
        "ok": True,
        "on": on if on is not None else want_on,
        "backend": "tuya",
        "dps": dps,
        "version": ver,
        "device_id": device_id,
        "switch_dps": used_dps,
        "set_raw": last,
    }
    bk = str(bright_dps)
    if bk in dps:
        try:
            out["brightness"] = int(dps[bk])
        except (TypeError, ValueError):
            pass
    mk = str(mode_dps)
    if mk in dps and dps[mk] is not None:
        out["mode"] = str(dps[mk])
    elif mode:
        out["mode"] = str(mode).strip().lower()
    return out


def daemon_loop() -> int:
    # import once at start so the first UI click is not killed mid-import
    try:
        import tinytuya  # noqa: F401
    except ImportError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"tinytuya missing: {e}"}) + "\n")
        sys.stdout.flush()
        return 2
    sys.stdout.write(json.dumps({"ok": True, "ready": True}) + "\n")
    sys.stdout.flush()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            return 0
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                out = {"ok": False, "error": "request must be object"}
            else:
                out = run_request(req)
        except Exception as e:
            out = {"ok": False, "error": str(e), "backend": "tuya"}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        return daemon_loop()

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("status", "set"))
    ap.add_argument("--ip", required=True)
    ap.add_argument("--id", required=True, dest="device_id")
    ap.add_argument("--key", required=True)
    ap.add_argument("--version", type=float, default=3.4)
    ap.add_argument("--dps", type=int, default=1)
    ap.add_argument("--on", type=_bool_arg, default=None)
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--bright-dps", type=int, default=22)
    ap.add_argument("--mode", type=str, default=None)
    ap.add_argument("--mode-dps", type=int, default=21)
    ap.add_argument("--timeout", type=float, default=4.0)
    args = ap.parse_args()
    req = {
        "action": args.action,
        "ip": args.ip,
        "id": args.device_id,
        "key": args.key,
        "version": args.version,
        "dps": args.dps,
        "bright_dps": args.bright_dps,
        "mode_dps": args.mode_dps,
        "timeout": args.timeout,
    }
    if args.on is not None:
        req["on"] = args.on
    if args.brightness is not None:
        req["brightness"] = args.brightness
    if args.mode:
        req["mode"] = args.mode
    out = run_request(req)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
