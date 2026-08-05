#!/usr/bin/env python3
"""
poolheat — Whatsminer thermal controller UI + API + history.

Local:
  cd Documents/poolheat/ui-demo && python3 serve.py

Entware / Keenetic Peak:
  /opt/etc/init.d/S99poolheat start
  http://<router-lan-ip>:8787/

Env overrides:
  POOLHEAT_WWW, POOLHEAT_DATA, POOLHEAT_CONFIG,
  POOLHEAT_BIND, POOLHEAT_PORT, POOLHEAT_MINER_HOST, POOLHEAT_MINER_PORT
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from Crypto.Cipher import AES
except ImportError:
    from Cryptodome.Cipher import AES  # type: ignore

from passlib.hash import md5_crypt


def _load_app_config() -> dict:
    """Resolve paths for local demo or Entware (/opt)."""
    # Prefer explicit env
    www = os.environ.get("POOLHEAT_WWW")
    data = os.environ.get("POOLHEAT_DATA")
    cfg_path = os.environ.get("POOLHEAT_CONFIG")

    # Entware layout if present
    if not www and Path("/opt/share/poolheat/www").is_dir():
        www = "/opt/share/poolheat/www"
    if not data and Path("/opt/var/poolheat").is_dir():
        data = "/opt/var/poolheat"
    if not cfg_path and Path("/opt/etc/poolheat/config.json").is_file():
        cfg_path = "/opt/etc/poolheat/config.json"

    # Local demo defaults
    here = Path(__file__).resolve().parent
    if not www:
        www = str(here)
    if not data:
        data = str(here)
    if not cfg_path:
        # optional local config next to serve.py
        local_cfg = here / "config.json"
        cfg_path = str(local_cfg) if local_cfg.is_file() else ""

    file_cfg: dict = {}
    if cfg_path and Path(cfg_path).is_file():
        try:
            file_cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        except Exception:
            file_cfg = {}

    bind = os.environ.get("POOLHEAT_BIND") or file_cfg.get("bind") or "0.0.0.0"
    port = int(os.environ.get("POOLHEAT_PORT") or file_cfg.get("http_port") or 8787)
    miner_host = (
        os.environ.get("POOLHEAT_MINER_HOST")
        or file_cfg.get("miner_host")
        or "192.168.1.10"
    )
    miner_port = int(
        os.environ.get("POOLHEAT_MINER_PORT") or file_cfg.get("miner_port") or 4028
    )
    api_password = file_cfg.get("api_password") or "admin"

    data_p = Path(data)
    data_p.mkdir(parents=True, exist_ok=True)

    return {
        "www": Path(www),
        "data": data_p,
        "cfg_path": cfg_path,
        "bind": bind,
        "http_port": port,
        "miner_host": miner_host,
        "miner_port": miner_port,
        "api_password": api_password,
        "file_cfg": file_cfg,
    }


_APP = _load_app_config()
HOST_MINER = _APP["miner_host"]
PORT_MINER = _APP["miner_port"]
HTTP_BIND = _APP["bind"]
HTTP_PORT = _APP["http_port"]
ROOT = _APP["www"]  # static files root
DATA = _APP["data"]
# Project display name (UI header / Telegram) — from config.json
PROJECT_NAME = str(
    (_APP.get("file_cfg") or {}).get("project_name") or "poolheat_WM"
).strip() or "poolheat_WM"
STATE_FILE = DATA / "last_commands.json"
DB_FILE = DATA / "history.db"
CONFIG_FILE = DATA / "history_config.json"
WEATHER_CFG_FILE = DATA / "weather_config.json"
POOL_CFG_FILE = DATA / "pool_config.json"
ZONE_CFG_FILE = DATA / "zone_map_config.json"
ZONE_PRESETS_FILE = DATA / "zone_map_presets.json"
POOL_PRESETS_FILE = DATA / "pool_presets.json"
FILTRATION_CFG_FILE = DATA / "filtration_config.json"
CHIPMAP_CFG_FILE = DATA / "chipmap_config.json"
CHIPMAP_CACHE_FILE = DATA / "chipmap_cache.json"
LUCI_PROXY_CFG_FILE = DATA / "luci_proxy_config.json"
TELEGRAM_CFG_FILE = DATA / "telegram_config.json"
# Policy / TG action log — survives restart (was RAM-only, wiped on OTA)
POLICY_EVENTS_FILE = DATA / "policy_events.json"
POLICY_EVENTS_MAX = 200  # keep last N on disk + in memory
# After OTA install + restart: notify the chat that started the update
UPDATE_NOTIFY_FILE = DATA / "update_notify.json"
DEFAULT_API_PASSWORD = _APP["api_password"]
# Live / control poll of miner API (UI + future policy loop). Not history sample interval.
POLL_INTERVAL_SEC = int(
    os.environ.get("POOLHEAT_POLL_INTERVAL")
    or _APP.get("file_cfg", {}).get("poll_interval_sec")
    or 5
)
POLL_INTERVAL_SEC = max(2, min(300, POLL_INTERVAL_SEC))
# Dry Run: ignore heat-zone auto (mode/limit/pct/MC); keep current miner mode.
# Only Safety Critical (chip temp) still writes.
_fc0 = _APP.get("file_cfg") or {}
DRY_RUN = bool(_fc0["dry_run"]) if "dry_run" in _fc0 else True

# Software version + GitHub updates
_DEFAULT_APP_VERSION = "0.3.43"
GITHUB_REPO = (
    os.environ.get("POOLHEAT_GITHUB_REPO")
    or (_APP.get("file_cfg") or {}).get("github_repo")
    or "andreybmc/poolheat"
).strip()
GITHUB_BRANCH = (
    os.environ.get("POOLHEAT_GITHUB_BRANCH")
    or (_APP.get("file_cfg") or {}).get("github_branch")
    or "main"
).strip() or "main"
_update_lock = threading.Lock()
_update_state: dict = {
    "busy": False,
    "last_check": None,
    "last_apply": None,
}
_miner_cfg_lock = threading.Lock()
_zone_cfg_lock = threading.Lock()
_policy_lock = threading.Lock()
_policy_stop = threading.Event()
_policy_ctrl: dict = {
    "heat_zone": None,  # z0|z1|z2|z3 sticky
    "safety_sticky": False,
    "last_key": None,
    "streak_key": None,
    "streak_count": 0,
    "last_apply_ts": 0.0,
    "last_event": None,
    "events": [],  # last POLICY_EVENTS_MAX — loaded/saved via policy_events.json
    "enabled": True,
    # Manual override: until unix ts — zone auto off, Safety still on
    "override_until_ts": 0.0,
    # Warmup (upfreq) started at (for max_warmup_wait_min)
    "warmup_since_ts": None,
    # Force Stop (emergency): sticky Suspend — above zones & Dry Run
    "force_stop": bool((_APP.get("file_cfg") or {}).get("force_stop", False)),
}

# Sensors usable as T_ctrl for heat-zone map (not Safety / chip Critical).
T_CTRL_SENSORS: tuple[str, ...] = (
    "liquid",
    "env",
    "chip_avg",
    "chip_max",
    "board_max",
)


def _normalize_t_ctrl_sensor(v) -> str:
    s = str(v or "liquid").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "liquid_temp": "liquid",
        "coolant": "liquid",
        "water": "liquid",
        "env_temp": "env",
        "ambient": "env",
        "environment": "env",
        "chip": "chip_max",
        "chipmax": "chip_max",
        "pcb": "board_max",
        "board": "board_max",
        "boards": "board_max",
        "pcb_max": "board_max",
    }
    s = aliases.get(s, s)
    return s if s in T_CTRL_SENSORS else "liquid"


def resolve_t_ctrl(
    live: dict | None, sensor: str | None = None
) -> tuple[float | None, str]:
    """Return (T_ctrl °C, normalized sensor id) from a live snapshot."""
    sens = _normalize_t_ctrl_sensor(sensor)
    live = live if isinstance(live, dict) else {}
    if sens == "liquid":
        return _f(live.get("liquid")), sens
    if sens == "env":
        return _f(live.get("env")), sens
    if sens == "chip_avg":
        return _f(live.get("chip_avg")), sens
    if sens == "chip_max":
        return _f(live.get("chip_max")), sens
    if sens == "board_max":
        boards = live.get("boards") or []
        vals: list[float] = []
        for b in boards:
            fb = _f(b)
            if fb is not None:
                vals.append(fb)
        return (max(vals) if vals else None), sens
    return _f(live.get("liquid")), "liquid"


DEFAULT_ZONE_CFG: dict = {
    # map schema: 2 = Z0 High · Z1 Normal · Z2 Reduced · Z3 No heat (T0/T1/T2)
    "zone_map_version": 2,
    # Which live field drives heat zones (T_ctrl). Safety Critical stays chip_max.
    "t_ctrl_sensor": "liquid",
    # T_ctrl thresholds (°C): cold → warm
    # ≤ T0 High · T0–T1 Normal · T1–T2 Reduced · ≥ T2 No heat
    "t0": 24.0,
    "t1": 26.0,
    "t2": 28.0,
    "h": 0.5,
    "t_crit": 70.0,
    "t_crit_clear": 65.0,
    "dwell_sec": 600,
    "settle_sec": 300,
    "streak": 3,
    # min seconds between any auto write (anti-spam even if state drifts)
    "min_write_interval_sec": 60,
    # Power Limit match tolerance (W) — within band = already OK
    "limit_tol_w": 100,
    # Warmup / upfreq gate
    "warmup_en": True,
    "warmup_downward_only": True,  # during warmup: only ↓ / Critical
    "max_warmup_wait_min": 30,  # after this, allow ↑ even if upfreq incomplete
    "zones": {
        "z0": {  # High Heat — coldest water
            "mode_en": True,
            "mode": "high",
            "work_en": True,
            "work": "resume",
            "lim_en": True,
            "lim": 7000,
            "pct_en": False,
            "pct": 100,
        },
        "z1": {  # Normal heat
            "mode_en": True,
            "mode": "normal",
            "work_en": True,
            "work": "resume",
            "lim_en": True,
            "lim": 5000,
            "pct_en": False,
            "pct": 100,
        },
        "z2": {  # Reduced heat
            "mode_en": True,
            "mode": "low",
            "work_en": True,
            "work": "resume",
            "lim_en": True,
            "lim": 2500,
            "pct_en": False,
            "pct": 70,
        },
        "z3": {  # No heat
            "mode_en": False,
            "mode": "low",
            "work_en": True,
            "work": "suspend",
            "lim_en": False,
            "lim": 0,
            "pct_en": False,
            "pct": 0,
        },
        "critical": {
            "on_crit": {
                "mode_en": True,
                "mode": "low",
                "work_en": True,
                "work": "suspend",
                "lim_en": False,
                "lim": 2500,
                "pct_en": False,
                "pct": 0,
            },
            "on_clear": {
                "mode_en": True,
                "mode": "low",
                "work_en": True,
                "work": "resume",
                "lim_en": True,
                "lim": 2500,
                "pct_en": False,
                "pct": 70,
            },
        },
    },
}
_zone_cfg: dict = {}


def _normalize_zone_entry(z: dict | None, default: dict) -> dict:
    out = dict(default)
    if not isinstance(z, dict):
        return out
    for key in ("mode_en", "work_en", "lim_en", "pct_en"):
        if key in z:
            out[key] = bool(z[key])
    # legacy sleep_en → Mining Control suspend
    if z.get("sleep_en") and "work_en" not in z:
        out["work_en"] = True
        out["work"] = "suspend"
    if "mode" in z and z["mode"] is not None:
        m = str(z["mode"]).lower()
        if m in ("low", "normal", "high"):
            out["mode"] = m
    if "work" in z and z["work"] is not None:
        w = str(z["work"]).lower()
        if w in ("resume", "suspend", "sleep"):
            out["work"] = "suspend" if w == "sleep" else w
    for key, lo, hi in (("lim", 0, 20000), ("pct", 0, 100)):
        if key in z and z[key] is not None:
            try:
                out[key] = max(lo, min(hi, float(z[key])))
            except (TypeError, ValueError):
                pass
    return out


def _load_zone_cfg() -> None:
    global _zone_cfg
    with _zone_cfg_lock:
        raw = _load_json(ZONE_CFG_FILE, DEFAULT_ZONE_CFG)
        cfg = dict(DEFAULT_ZONE_CFG)
        for key in (
            "t0",
            "t1",
            "t2",
            "h",
            "t_crit",
            "t_crit_clear",
            "dwell_sec",
            "settle_sec",
            "streak",
            "min_write_interval_sec",
            "limit_tol_w",
            "max_warmup_wait_min",
        ):
            if key in raw and raw[key] is not None:
                try:
                    if key in (
                        "dwell_sec",
                        "settle_sec",
                        "streak",
                        "min_write_interval_sec",
                        "limit_tol_w",
                        "max_warmup_wait_min",
                    ):
                        cfg[key] = int(float(raw[key]))
                    else:
                        cfg[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
        if "t_ctrl_sensor" in raw and raw.get("t_ctrl_sensor") is not None:
            cfg["t_ctrl_sensor"] = _normalize_t_ctrl_sensor(raw.get("t_ctrl_sensor"))
        else:
            cfg["t_ctrl_sensor"] = _normalize_t_ctrl_sensor(
                cfg.get("t_ctrl_sensor", "liquid")
            )
        for bkey in ("warmup_en", "warmup_downward_only"):
            if bkey in raw:
                cfg[bkey] = bool(raw[bkey])
        cfg["min_write_interval_sec"] = max(
            10, min(3600, int(cfg.get("min_write_interval_sec", 60) or 60))
        )
        cfg["limit_tol_w"] = max(10, min(2000, int(cfg.get("limit_tol_w", 100) or 100)))
        cfg["max_warmup_wait_min"] = max(
            1, min(240, int(cfg.get("max_warmup_wait_min", 30) or 30))
        )
        cfg["warmup_en"] = bool(cfg.get("warmup_en", True))
        cfg["warmup_downward_only"] = bool(cfg.get("warmup_downward_only", True))
        # clamps
        cfg["t0"] = float(cfg["t0"])
        cfg["t1"] = float(cfg["t1"])
        cfg["h"] = max(0.2, min(5.0, float(cfg.get("h", 0.5))))
        cfg["t_crit"] = float(cfg["t_crit"])
        cfg["t_crit_clear"] = float(cfg["t_crit_clear"])
        cfg["t_ctrl_sensor"] = _normalize_t_ctrl_sensor(cfg.get("t_ctrl_sensor"))
        zones_in = raw.get("zones") if isinstance(raw.get("zones"), dict) else {}
        ver = int(raw.get("zone_map_version") or 0)
        # migrate v1 (3 heat zones) → v2 (4 heat zones + T2)
        if ver < 2:
            old_t0 = float(cfg.get("t0", 26))
            old_t1 = float(cfg.get("t1", 28))
            # if t2 already present but version missing, treat t0/t1/t2 as-is for thresholds
            if "t2" in raw and raw.get("t2") is not None and "z3" in zones_in:
                cfg["t2"] = float(raw["t2"])
            else:
                h0 = float(cfg.get("h", 0.5))
                # old: ≤t0 Normal, mid Reduced, ≥t1 No heat
                cfg["t0"] = old_t0 - max(2.0, h0)
                cfg["t1"] = old_t0
                cfg["t2"] = old_t1
            if "z3" not in zones_in:
                old_z = dict(zones_in)
                zones_in = {
                    "z0": None,  # High → default
                    "z1": old_z.get("z0"),  # was Normal
                    "z2": old_z.get("z1"),  # was Reduced
                    "z3": old_z.get("z2"),  # was No heat
                    "critical": old_z.get("critical"),
                }
            cfg["zone_map_version"] = 2
        if "t2" not in cfg or cfg.get("t2") is None:
            cfg["t2"] = float(cfg.get("t1", 28)) + max(2.0, float(cfg.get("h", 0.5)))
        cfg["t2"] = float(cfg["t2"])
        # ensure t0 < t1 < t2
        if cfg["t0"] >= cfg["t1"]:
            cfg["t1"] = cfg["t0"] + max(cfg["h"], 0.5)
        if cfg["t1"] >= cfg["t2"]:
            cfg["t2"] = cfg["t1"] + max(cfg["h"], 0.5)
        cfg["zone_map_version"] = 2
        zones_out = {}
        for name, default in DEFAULT_ZONE_CFG["zones"].items():
            zin = zones_in.get(name)
            if name == "critical":
                # support on_crit / on_clear or legacy flat profile
                if isinstance(zin, dict) and (
                    "on_crit" in zin or "on_clear" in zin
                ):
                    zones_out[name] = {
                        "on_crit": _normalize_zone_entry(
                            zin.get("on_crit"), default["on_crit"]
                        ),
                        "on_clear": _normalize_zone_entry(
                            zin.get("on_clear"), default["on_clear"]
                        ),
                    }
                else:
                    # legacy single block → on_crit; on_clear keeps default
                    zones_out[name] = {
                        "on_crit": _normalize_zone_entry(zin, default["on_crit"]),
                        "on_clear": dict(default["on_clear"]),
                    }
            else:
                zones_out[name] = _normalize_zone_entry(zin, default)
        cfg["zones"] = zones_out
        _zone_cfg = cfg
        # persist migration so UI/API see v2 map (already under lock — write directly)
        try:
            _save_json(ZONE_CFG_FILE, _zone_cfg)
        except Exception:
            pass


def _save_zone_cfg() -> None:
    with _zone_cfg_lock:
        _save_json(ZONE_CFG_FILE, _zone_cfg)


def get_zone_cfg() -> dict:
    with _zone_cfg_lock:
        return json.loads(json.dumps(_zone_cfg))  # deep copy


# ── Zone map presets (named snapshots of zone_map_config) ───────────────────

_zone_presets_lock = threading.Lock()
_zone_presets: dict = {"presets": [], "active_id": None}


def _new_preset_id() -> str:
    return uuid.uuid4().hex[:12]


def _load_zone_presets() -> None:
    global _zone_presets
    with _zone_presets_lock:
        raw = _load_json(ZONE_PRESETS_FILE, {"presets": [], "active_id": None})
        if not isinstance(raw, dict):
            raw = {"presets": [], "active_id": None}
        presets = raw.get("presets") if isinstance(raw.get("presets"), list) else []
        clean: list[dict] = []
        for p in presets:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip() or _new_preset_id()
            name = str(p.get("name") or "").strip() or "Пресет"
            if len(name) > 64:
                name = name[:64]
            cfg = p.get("config") if isinstance(p.get("config"), dict) else {}
            clean.append(
                {
                    "id": pid,
                    "name": name,
                    "updated_ts": p.get("updated_ts")
                    or datetime.now().isoformat(timespec="seconds"),
                    "config": cfg,
                }
            )
        # seed default if empty
        if not clean:
            clean.append(
                {
                    "id": _new_preset_id(),
                    "name": "По умолчанию 24/26/28",
                    "updated_ts": datetime.now().isoformat(timespec="seconds"),
                    "config": json.loads(json.dumps(DEFAULT_ZONE_CFG)),
                }
            )
        _zone_presets = {
            "presets": clean,
            "active_id": raw.get("active_id") if raw.get("active_id") else None,
        }
        try:
            _save_json(ZONE_PRESETS_FILE, _zone_presets)
        except Exception:
            pass


def _save_zone_presets() -> None:
    with _zone_presets_lock:
        _save_json(ZONE_PRESETS_FILE, _zone_presets)


def list_zone_presets() -> dict:
    with _zone_presets_lock:
        items = []
        for p in _zone_presets.get("presets") or []:
            items.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "updated_ts": p.get("updated_ts"),
                    # lightweight: thresholds summary for UI
                    "summary": _preset_summary(p.get("config") or {}),
                }
            )
        return {
            "ok": True,
            "presets": items,
            "active_id": _zone_presets.get("active_id"),
        }


def _preset_summary(cfg: dict) -> str:
    try:
        t0 = cfg.get("t0", "—")
        t1 = cfg.get("t1", "—")
        t2 = cfg.get("t2", "—")
        h = cfg.get("h", "—")
        return f"T0={t0} T1={t1} T2={t2} h={h}"
    except Exception:
        return "—"


def get_zone_preset(preset_id: str) -> dict | None:
    pid = str(preset_id or "").strip()
    with _zone_presets_lock:
        for p in _zone_presets.get("presets") or []:
            if p.get("id") == pid:
                return json.loads(json.dumps(p))
    return None


def save_zone_preset(
    name: str,
    config: dict | None = None,
    *,
    preset_id: str | None = None,
) -> dict:
    """
    Create or update a named zone-map preset.
    config=None → snapshot current active zone map.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("name empty")
    if len(name) > 64:
        name = name[:64]
    if config is None:
        config = get_zone_cfg()
    if not isinstance(config, dict):
        raise ValueError("config must be object")
    # normalize via same path as active map
    cfg = _coerce_zone_config_dict(config)
    now = datetime.now().isoformat(timespec="seconds")
    with _zone_presets_lock:
        presets = list(_zone_presets.get("presets") or [])
        if preset_id:
            pid = str(preset_id).strip()
            found = False
            for i, p in enumerate(presets):
                if p.get("id") == pid:
                    presets[i] = {
                        "id": pid,
                        "name": name,
                        "updated_ts": now,
                        "config": cfg,
                    }
                    found = True
                    break
            if not found:
                raise ValueError(f"preset not found: {pid}")
            out_id = pid
        else:
            out_id = _new_preset_id()
            presets.append(
                {
                    "id": out_id,
                    "name": name,
                    "updated_ts": now,
                    "config": cfg,
                }
            )
        _zone_presets["presets"] = presets
    _save_zone_presets()
    return {"ok": True, "id": out_id, **list_zone_presets()}


def delete_zone_preset(preset_id: str) -> dict:
    pid = str(preset_id or "").strip()
    if not pid:
        raise ValueError("id empty")
    with _zone_presets_lock:
        presets = [p for p in (_zone_presets.get("presets") or []) if p.get("id") != pid]
        if len(presets) == len(_zone_presets.get("presets") or []):
            raise ValueError(f"preset not found: {pid}")
        _zone_presets["presets"] = presets
        if _zone_presets.get("active_id") == pid:
            _zone_presets["active_id"] = None
    _save_zone_presets()
    return list_zone_presets()


def apply_zone_preset(preset_id: str) -> dict:
    """Load preset into active zone map (persist zone_map_config.json)."""
    p = get_zone_preset(preset_id)
    if not p:
        raise ValueError(f"preset not found: {preset_id}")
    cfg = _coerce_zone_config_dict(p.get("config") or {})
    with _zone_cfg_lock:
        _zone_cfg.clear()
        _zone_cfg.update(cfg)
    _save_zone_cfg()
    with _zone_presets_lock:
        _zone_presets["active_id"] = p.get("id")
    _save_zone_presets()
    return {
        "ok": True,
        "id": p.get("id"),
        "name": p.get("name"),
        "config": get_zone_cfg(),
        "active_id": p.get("id"),
    }


# ── Mining pool presets (named url/user/pass snapshots) ─────────────────────
_pool_presets_lock = threading.Lock()
_pool_presets: dict = {"presets": [], "active_id": None}


def _normalize_pool_preset_pools(pools) -> list[dict]:
    """1–3 pool entries {url, user, pass} for write/update_pools."""
    out: list[dict] = []
    raw = pools if isinstance(pools, list) else []
    for p in raw[:3]:
        if not isinstance(p, dict):
            continue
        out.append(
            {
                "url": str(p.get("url") or p.get("URL") or "").strip(),
                "user": str(
                    p.get("user") or p.get("User") or p.get("worker") or ""
                ).strip(),
                "pass": str(
                    p.get("pass")
                    or p.get("password")
                    or p.get("Pass")
                    or "x"
                ),
            }
        )
    while len(out) < 3:
        out.append({"url": "", "user": "", "pass": "x"})
    # drop trailing empties but keep at least one slot for UI
    while len(out) > 1 and not out[-1].get("url") and not out[-1].get("user"):
        out.pop()
    if not any(x.get("url") for x in out):
        raise ValueError("at least one pool url required")
    return out


def _pool_preset_summary(pools: list) -> str:
    parts = []
    for p in pools or []:
        if not isinstance(p, dict):
            continue
        url = str(p.get("url") or "").strip()
        if not url:
            continue
        # short host-ish
        host = url.replace("stratum+tcp://", "").replace("stratum+ssl://", "")
        host = host.split("/")[0].split(":")[0]
        user = str(p.get("user") or "").strip()
        worker = user.split(".")[-1] if user else ""
        parts.append(host + (f"/{worker}" if worker else ""))
    return " · ".join(parts[:3]) if parts else "—"


def _load_pool_presets() -> None:
    global _pool_presets
    with _pool_presets_lock:
        raw = _load_json(POOL_PRESETS_FILE, {"presets": [], "active_id": None})
        if not isinstance(raw, dict):
            raw = {"presets": [], "active_id": None}
        clean = []
        for p in raw.get("presets") or []:
            if not isinstance(p, dict):
                continue
            try:
                pools = _normalize_pool_preset_pools(p.get("pools") or p.get("config"))
            except Exception:
                continue
            clean.append(
                {
                    "id": str(p.get("id") or "").strip() or _new_preset_id(),
                    "name": str(p.get("name") or "Пресет")[:64],
                    "updated_ts": p.get("updated_ts")
                    or datetime.now().isoformat(timespec="seconds"),
                    "pools": pools,
                    "summary": _pool_preset_summary(pools),
                }
            )
        _pool_presets = {
            "presets": clean,
            "active_id": raw.get("active_id") if raw.get("active_id") else None,
        }
        try:
            _save_json(POOL_PRESETS_FILE, _pool_presets)
        except Exception:
            pass


def _save_pool_presets() -> None:
    with _pool_presets_lock:
        _save_json(POOL_PRESETS_FILE, _pool_presets)


def list_pool_presets(*, include_secrets: bool = True) -> dict:
    with _pool_presets_lock:
        items = []
        for p in _pool_presets.get("presets") or []:
            pools = p.get("pools") or []
            if not include_secrets:
                pools = [
                    {
                        "url": x.get("url"),
                        "user": x.get("user"),
                        "pass": "••••" if x.get("pass") else "",
                        "pass_set": bool(x.get("pass")),
                    }
                    for x in pools
                    if isinstance(x, dict)
                ]
            items.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "updated_ts": p.get("updated_ts"),
                    "summary": p.get("summary") or _pool_preset_summary(p.get("pools") or []),
                    "pools": pools,
                }
            )
        return {
            "ok": True,
            "presets": items,
            "active_id": _pool_presets.get("active_id"),
        }


def get_pool_preset(preset_id: str) -> dict | None:
    pid = str(preset_id or "").strip()
    if not pid:
        return None
    with _pool_presets_lock:
        for p in _pool_presets.get("presets") or []:
            if p.get("id") == pid:
                return json.loads(json.dumps(p))
    return None


def snapshot_live_pools_for_preset() -> list[dict]:
    """Current ASIC pools → {url,user,pass}; password often unknown → 'x'."""
    body = fetch_mining_pools(force=True)
    if not body.get("ok"):
        raise RuntimeError(body.get("error") or "cannot read pools from miner")
    rows = body.get("pools") or []
    # order by pool number
    try:
        rows = sorted(rows, key=lambda r: int(r.get("pool") or 99))
    except Exception:
        pass
    out = []
    for r in rows[:3]:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if url in ("", "—"):
            continue
        out.append(
            {
                "url": url,
                "user": str(r.get("user") or "").strip(),
                # Whatsminer status API does not return password
                "pass": "x",
            }
        )
    if not out:
        raise ValueError("miner has no pools configured")
    return _normalize_pool_preset_pools(out)


def save_pool_preset(
    name: str,
    pools: list | None = None,
    *,
    preset_id: str | None = None,
    from_live: bool = False,
) -> dict:
    """Create/update pool preset. from_live → snapshot miner; pools=list → edit."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("name empty")
    if len(name) > 64:
        name = name[:64]

    if from_live:
        pools_norm = snapshot_live_pools_for_preset()
        # keep old passwords if same url+user when overwriting preset
        if preset_id:
            old = get_pool_preset(preset_id)
            if old:
                old_by_key = {
                    (
                        str(x.get("url") or "").strip(),
                        str(x.get("user") or "").strip(),
                    ): x
                    for x in (old.get("pools") or [])
                    if isinstance(x, dict)
                }
                for p in pools_norm:
                    key = (p.get("url") or "", p.get("user") or "")
                    prev = old_by_key.get(key)
                    if (
                        prev
                        and prev.get("pass")
                        and prev.get("pass") not in ("x", "••••", "")
                        and p.get("pass") in ("", "x")
                    ):
                        p["pass"] = prev["pass"]
    elif pools is not None:
        pools_norm = _normalize_pool_preset_pools(pools)
    elif preset_id:
        existing = get_pool_preset(preset_id)
        if not existing:
            raise ValueError(f"preset not found: {preset_id}")
        pools_norm = existing.get("pools") or []
    else:
        raise ValueError("pools list or from_live=true required")
    now = datetime.now().isoformat(timespec="seconds")
    with _pool_presets_lock:
        presets = list(_pool_presets.get("presets") or [])
        if preset_id:
            pid = str(preset_id).strip()
            found = False
            for i, p in enumerate(presets):
                if p.get("id") == pid:
                    presets[i] = {
                        "id": pid,
                        "name": name,
                        "updated_ts": now,
                        "pools": pools_norm,
                        "summary": _pool_preset_summary(pools_norm),
                    }
                    found = True
                    break
            if not found:
                raise ValueError(f"preset not found: {pid}")
            out_id = pid
        else:
            out_id = _new_preset_id()
            presets.append(
                {
                    "id": out_id,
                    "name": name,
                    "updated_ts": now,
                    "pools": pools_norm,
                    "summary": _pool_preset_summary(pools_norm),
                }
            )
        _pool_presets["presets"] = presets
    _save_pool_presets()
    return {"ok": True, "id": out_id, **list_pool_presets()}


def delete_pool_preset(preset_id: str) -> dict:
    pid = str(preset_id or "").strip()
    if not pid:
        raise ValueError("id empty")
    with _pool_presets_lock:
        presets = [p for p in (_pool_presets.get("presets") or []) if p.get("id") != pid]
        if len(presets) == len(_pool_presets.get("presets") or []):
            raise ValueError(f"preset not found: {pid}")
        _pool_presets["presets"] = presets
        if _pool_presets.get("active_id") == pid:
            _pool_presets["active_id"] = None
    _save_pool_presets()
    return list_pool_presets()


def apply_pool_preset(preset_id: str, *, password: str | None = None) -> dict:
    """Write preset pools to ASIC (update_pools) and mark active."""
    p = get_pool_preset(preset_id)
    if not p:
        raise ValueError(f"preset not found: {preset_id}")
    pools = _normalize_pool_preset_pools(p.get("pools") or [])
    pw = password or DEFAULT_API_PASSWORD
    resp = miner_write_cmd({"cmd": "update_pools", "pools": pools}, pw)
    out = _record_write(
        "pools",
        pools,
        resp,
        warning="pools preset applied · btminer restart may follow",
    )
    if isinstance(resp, dict) and resp.get("transport"):
        out["transport"] = resp.get("transport")
    with _pool_presets_lock:
        _pool_presets["active_id"] = p.get("id")
    _save_pool_presets()
    # invalidate live pools cache
    try:
        global _pools_cache, _pools_cache_ts
        with _pools_cache_lock:
            _pools_cache = None
            _pools_cache_ts = 0.0
    except Exception:
        pass
    out["ok"] = True
    out["id"] = p.get("id")
    out["name"] = p.get("name")
    out["pools"] = pools
    out["active_id"] = p.get("id")
    # include list for UI refresh
    try:
        out.update(list_pool_presets())
        out["ok"] = True
        out["id"] = p.get("id")
        out["name"] = p.get("name")
        out["active_id"] = p.get("id")
    except Exception:
        pass
    return out


def _coerce_zone_config_dict(req: dict) -> dict:
    """Normalize a zone-map payload (same rules as POST /api/zone/config)."""
    if not isinstance(req, dict):
        raise ValueError("expected object")
    cfg = dict(DEFAULT_ZONE_CFG)
    # start from current so partial updates still work when used that way
    with _zone_cfg_lock:
        base = dict(_zone_cfg) if _zone_cfg else dict(DEFAULT_ZONE_CFG)
    cfg.update({k: base.get(k) for k in DEFAULT_ZONE_CFG if k != "zones"})
    cfg["zones"] = json.loads(json.dumps(base.get("zones") or DEFAULT_ZONE_CFG["zones"]))
    for key in (
        "t0",
        "t1",
        "t2",
        "h",
        "t_crit",
        "t_crit_clear",
        "dwell_sec",
        "settle_sec",
        "streak",
        "min_write_interval_sec",
        "limit_tol_w",
        "max_warmup_wait_min",
    ):
        if key in req and req[key] is not None:
            try:
                if key in (
                    "dwell_sec",
                    "settle_sec",
                    "streak",
                    "min_write_interval_sec",
                    "limit_tol_w",
                    "max_warmup_wait_min",
                ):
                    cfg[key] = int(float(req[key]))
                else:
                    cfg[key] = float(req[key])
            except (TypeError, ValueError):
                pass
    if "t_ctrl_sensor" in req and req.get("t_ctrl_sensor") is not None:
        cfg["t_ctrl_sensor"] = _normalize_t_ctrl_sensor(req.get("t_ctrl_sensor"))
    for bkey in ("warmup_en", "warmup_downward_only"):
        if bkey in req:
            cfg[bkey] = bool(req[bkey])
    cfg["min_write_interval_sec"] = max(
        10, min(3600, int(cfg.get("min_write_interval_sec", 60) or 60))
    )
    cfg["limit_tol_w"] = max(10, min(2000, int(cfg.get("limit_tol_w", 100) or 100)))
    cfg["max_warmup_wait_min"] = max(
        1, min(240, int(cfg.get("max_warmup_wait_min", 30) or 30))
    )
    cfg["warmup_en"] = bool(cfg.get("warmup_en", True))
    cfg["warmup_downward_only"] = bool(cfg.get("warmup_downward_only", True))
    cfg["h"] = max(0.2, min(5.0, float(cfg.get("h", 0.5))))
    cfg["t_ctrl_sensor"] = _normalize_t_ctrl_sensor(
        cfg.get("t_ctrl_sensor", "liquid")
    )
    if "t2" not in cfg or cfg.get("t2") is None:
        cfg["t2"] = float(cfg.get("t1", 28)) + max(2.0, float(cfg["h"]))
    if float(cfg["t0"]) >= float(cfg["t1"]):
        raise ValueError("need t0 < t1")
    if float(cfg["t1"]) >= float(cfg["t2"]):
        raise ValueError("need t1 < t2")
    if float(cfg["t_crit_clear"]) >= float(cfg["t_crit"]):
        raise ValueError("need t_crit_clear < t_crit")
    zones_in = req.get("zones") if isinstance(req.get("zones"), dict) else {}
    zones_out = dict(cfg.get("zones") or {})
    for name, default in DEFAULT_ZONE_CFG["zones"].items():
        base_z = zones_out.get(name) or default
        zin = zones_in.get(name, base_z)
        if name == "critical":
            if not isinstance(base_z, dict):
                base_z = default
            if isinstance(zin, dict) and ("on_crit" in zin or "on_clear" in zin):
                zones_out[name] = {
                    "on_crit": _normalize_zone_entry(
                        zin.get("on_crit", base_z.get("on_crit")),
                        default["on_crit"],
                    ),
                    "on_clear": _normalize_zone_entry(
                        zin.get("on_clear", base_z.get("on_clear")),
                        default["on_clear"],
                    ),
                }
            else:
                zones_out[name] = {
                    "on_crit": _normalize_zone_entry(
                        zin if isinstance(zin, dict) else base_z.get("on_crit"),
                        default["on_crit"],
                    ),
                    "on_clear": _normalize_zone_entry(
                        base_z.get("on_clear") if isinstance(base_z, dict) else None,
                        default["on_clear"],
                    ),
                }
        else:
            zones_out[name] = _normalize_zone_entry(zin, default)
    cfg["zones"] = zones_out
    cfg["zone_map_version"] = 2
    return cfg


# ── Filtration pump · multi-backend ───────────────────────────────────────────
# Backends: tapo (P100/P110) · ewelink (Sonoff DIY LAN) · webhook · shelly · homeassistant
# Rule: MC Resume + auto_on_mining → force ON; optional auto_off_suspend.

FILTRATION_BACKENDS: list[dict] = [
    {
        "id": "tapo",
        "label": "Tapo P100 / P110",
        "hint": "LAN · email/password аккаунта Tapo · IP розетки",
    },
    {
        "id": "ewelink",
        "label": "eWeLink / Sonoff DIY",
        "hint": "LAN DIY mode · IP + deviceid (облако eWeLink — через Webhook)",
    },
    {
        "id": "webhook",
        "label": "Webhook (HTTP)",
        "hint": "URL on/off · IFTTT, n8n, Node-RED, eWeLink scene, …",
    },
    {
        "id": "shelly",
        "label": "Shelly",
        "hint": "LAN HTTP · Gen1 relay / Gen2 RPC",
    },
    {
        "id": "homeassistant",
        "label": "Home Assistant",
        "hint": "REST · long-lived token · switch/entity",
    },
]
_FILTRATION_BACKEND_IDS = {b["id"] for b in FILTRATION_BACKENDS}

DEFAULT_FILTRATION_CFG: dict = {
    "enabled": False,
    "backend": "tapo",
    # common / tapo / ewelink / shelly
    "ip": "",
    "email": "",
    "password": "",
    "device_id": "",  # eWeLink DIY deviceid
    "ewelink_port": 8081,
    # webhook
    "webhook_on_url": "",
    "webhook_off_url": "",
    "webhook_method": "GET",  # GET | POST
    "webhook_body_on": "",
    "webhook_body_off": "",
    "webhook_headers": "",  # JSON object as string
    # shelly
    "shelly_channel": 0,
    "shelly_gen": "auto",  # auto | 1 | 2
    # homeassistant
    "ha_url": "",
    "ha_token": "",
    "ha_entity_id": "",
    # policy
    "auto_on_mining": True,
    "auto_off_suspend": False,
    # allow manual pump OFF while Mining Control = Resume
    "allow_off_while_mining": False,
    # runtime
    "last_on": None,
    "last_error": None,
    "last_ok_ts": None,
    "last_sync_ts": None,
    "last_action": None,
}

_filtration_lock = threading.Lock()
_filtration_cfg: dict = dict(DEFAULT_FILTRATION_CFG)
_filtration_session: dict = {
    "cookie": None,
    "token": None,
    "last_try_ts": 0.0,
}


def list_filtration_backends() -> list[dict]:
    return list(FILTRATION_BACKENDS)


def _as_bool(v) -> bool:
    """Truthy parse for JSON/form bools (true/1/yes/on)."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y", "вкл", "enable", "enabled"):
        return True
    if s in ("0", "false", "no", "off", "n", "выкл", "disable", "disabled", ""):
        return False
    return bool(v)


def _load_filtration_cfg() -> None:
    global _filtration_cfg
    with _filtration_lock:
        raw = _load_json(FILTRATION_CFG_FILE, DEFAULT_FILTRATION_CFG)
        if not isinstance(raw, dict):
            raw = {}
        cfg = dict(DEFAULT_FILTRATION_CFG)
        for k in DEFAULT_FILTRATION_CFG:
            if k not in raw:
                continue
            cfg[k] = raw[k]
        cfg["enabled"] = _as_bool(cfg.get("enabled", False))
        be = str(cfg.get("backend") or "tapo").strip().lower()
        if be not in _FILTRATION_BACKEND_IDS:
            be = "tapo"
        cfg["backend"] = be
        for sk in (
            "ip",
            "email",
            "password",
            "device_id",
            "webhook_on_url",
            "webhook_off_url",
            "webhook_method",
            "webhook_body_on",
            "webhook_body_off",
            "webhook_headers",
            "shelly_gen",
            "ha_url",
            "ha_token",
            "ha_entity_id",
        ):
            cfg[sk] = str(cfg.get(sk) or "").strip() if sk != "password" and sk != "ha_token" else str(cfg.get(sk) or "")
        # keep secrets as-is
        cfg["password"] = str(raw.get("password") or cfg.get("password") or "")
        cfg["ha_token"] = str(raw.get("ha_token") or cfg.get("ha_token") or "")
        try:
            cfg["ewelink_port"] = max(1, min(65535, int(cfg.get("ewelink_port") or 8081)))
        except (TypeError, ValueError):
            cfg["ewelink_port"] = 8081
        try:
            cfg["shelly_channel"] = max(0, min(3, int(cfg.get("shelly_channel") or 0)))
        except (TypeError, ValueError):
            cfg["shelly_channel"] = 0
        wm = str(cfg.get("webhook_method") or "GET").upper()
        cfg["webhook_method"] = "POST" if wm == "POST" else "GET"
        sg = str(cfg.get("shelly_gen") or "auto").lower()
        cfg["shelly_gen"] = sg if sg in ("auto", "1", "2") else "auto"
        cfg["auto_on_mining"] = _as_bool(cfg.get("auto_on_mining", True))
        cfg["auto_off_suspend"] = _as_bool(cfg.get("auto_off_suspend", False))
        cfg["allow_off_while_mining"] = _as_bool(
            cfg.get("allow_off_while_mining", False)
        )
        if "last_on" in raw:
            cfg["last_on"] = (
                None if raw.get("last_on") is None else bool(raw.get("last_on"))
            )
        _filtration_cfg = cfg


def _save_filtration_cfg() -> None:
    """Atomic write; raise on failure so UI does not report false success."""
    with _filtration_lock:
        data = dict(_filtration_cfg)
    try:
        FILTRATION_CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        tmp = FILTRATION_CFG_FILE.with_suffix(FILTRATION_CFG_FILE.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(FILTRATION_CFG_FILE)
    except Exception as e:
        print(f"[filtration] save fail {FILTRATION_CFG_FILE}: {e}")
        raise RuntimeError(f"cannot save filtration config: {e}") from e


def get_filtration_cfg(*, redact: bool = True) -> dict:
    with _filtration_lock:
        cfg = dict(_filtration_cfg)
    if redact:
        if cfg.get("password"):
            p = str(cfg["password"])
            cfg["password_set"] = True
            cfg["password"] = (p[:2] + "…" + p[-2:]) if len(p) > 6 else "••••"
        else:
            cfg["password_set"] = False
        if cfg.get("ha_token"):
            t = str(cfg["ha_token"])
            cfg["ha_token_set"] = True
            cfg["ha_token"] = (t[:4] + "…" + t[-4:]) if len(t) > 10 else "••••"
        else:
            cfg["ha_token_set"] = False
    else:
        cfg["password_set"] = bool(cfg.get("password"))
        cfg["ha_token_set"] = bool(cfg.get("ha_token"))
    cfg["backends"] = list_filtration_backends()
    return cfg


def apply_filtration_cfg(req: dict) -> dict:
    """Update filtration settings from Settings UI."""
    if not isinstance(req, dict):
        raise ValueError("expected object")
    with _filtration_lock:
        # always accept enabled when present (UI always sends it)
        if "enabled" in req:
            _filtration_cfg["enabled"] = _as_bool(req.get("enabled"))
        if "backend" in req and req["backend"] is not None:
            be = str(req["backend"]).strip().lower()
            if be not in _FILTRATION_BACKEND_IDS:
                raise ValueError(f"unknown backend: {be}")
            _filtration_cfg["backend"] = be
        for sk in (
            "ip",
            "email",
            "device_id",
            "webhook_on_url",
            "webhook_off_url",
            "webhook_body_on",
            "webhook_body_off",
            "webhook_headers",
            "shelly_gen",
            "ha_url",
            "ha_entity_id",
        ):
            if sk in req and req[sk] is not None:
                _filtration_cfg[sk] = str(req[sk]).strip()
        if "password" in req and req["password"] is not None and str(req["password"]) != "":
            _filtration_cfg["password"] = str(req["password"])
        if "ha_token" in req and req["ha_token"] is not None and str(req["ha_token"]) != "":
            _filtration_cfg["ha_token"] = str(req["ha_token"])
        if "webhook_method" in req and req["webhook_method"] is not None:
            wm = str(req["webhook_method"]).upper()
            _filtration_cfg["webhook_method"] = "POST" if wm == "POST" else "GET"
        if "ewelink_port" in req and req["ewelink_port"] is not None:
            try:
                _filtration_cfg["ewelink_port"] = max(
                    1, min(65535, int(req["ewelink_port"]))
                )
            except (TypeError, ValueError):
                pass
        if "shelly_channel" in req and req["shelly_channel"] is not None:
            try:
                _filtration_cfg["shelly_channel"] = max(
                    0, min(3, int(req["shelly_channel"]))
                )
            except (TypeError, ValueError):
                pass
        if "auto_on_mining" in req:
            _filtration_cfg["auto_on_mining"] = _as_bool(req.get("auto_on_mining"))
        if "auto_off_suspend" in req:
            _filtration_cfg["auto_off_suspend"] = _as_bool(req.get("auto_off_suspend"))
        if "allow_off_while_mining" in req:
            _filtration_cfg["allow_off_while_mining"] = _as_bool(
                req.get("allow_off_while_mining")
            )
        _filtration_session["token"] = None
        _filtration_session["cookie"] = None
    _save_filtration_cfg()
    out = get_filtration_cfg(redact=True)
    # re-read file once so enabled is confirmed persisted
    try:
        raw = _load_json(FILTRATION_CFG_FILE, {})
        if isinstance(raw, dict) and "enabled" in raw:
            out["enabled"] = bool(raw.get("enabled"))
            with _filtration_lock:
                _filtration_cfg["enabled"] = bool(raw.get("enabled"))
    except Exception:
        pass
    return out


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n] * n)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n] * n):
        return data.rstrip(b"\x00")
    return data[:-n]


def _tapo_aes_encrypt(key: bytes, iv: bytes, plain: str) -> str:
    from Crypto.Cipher import AES  # type: ignore

    raw = _pkcs7_pad(plain.encode("utf-8"))
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(raw)).decode("ascii")


def _tapo_aes_decrypt(key: bytes, iv: bytes, b64: str) -> str:
    from Crypto.Cipher import AES  # type: ignore

    cipher = AES.new(key, AES.MODE_CBC, iv)
    raw = cipher.decrypt(base64.b64decode(b64))
    return _pkcs7_unpad(raw).decode("utf-8", errors="replace")


def _tapo_sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _tapo_parse_session_cookie(set_cookie: str | None) -> str | None:
    if not set_cookie:
        return None
    # Prefer TP_SESSIONID=... (device may send multiple Set-Cookie)
    for part in re.split(r",(?=[A-Za-z_]+=)", set_cookie):
        part = part.strip()
        pair = part.split(";")[0].strip()
        if pair.upper().startswith("TP_SESSIONID="):
            return pair
    return set_cookie.split(";")[0].strip() or None


def _tapo_http_json(
    url: str,
    payload: dict,
    *,
    cookie: str | None = None,
    timeout: float = 5.0,
) -> tuple[dict, str | None]:
    """POST JSON; return (body, set-cookie value for TP_SESSIONID if any)."""
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "poolheat-tapo/1.0",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        set_cookie = resp.headers.get("Set-Cookie") or resp.headers.get("set-cookie")
    cookie_out = _tapo_parse_session_cookie(set_cookie)
    try:
        j = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"tapo bad json: {e}: {body[:120]}") from e
    if not isinstance(j, dict):
        raise RuntimeError("tapo response not object")
    return j, cookie_out


def _tapo_http_bytes(
    url: str,
    data: bytes,
    *,
    cookie: str | None = None,
    content_type: str = "application/octet-stream",
    timeout: float = 6.0,
) -> tuple[int, bytes, str | None]:
    """POST raw bytes (KLAP handshake/request). Returns (status, body, cookie)."""
    headers = {
        "Content-Type": content_type,
        "User-Agent": "poolheat-tapo/1.0",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            set_cookie = resp.headers.get("Set-Cookie") or resp.headers.get("set-cookie")
        return status, body, _tapo_parse_session_cookie(set_cookie)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        set_cookie = None
        try:
            set_cookie = e.headers.get("Set-Cookie") if e.headers else None
        except Exception:
            pass
        return int(e.code), body, _tapo_parse_session_cookie(set_cookie)


class _KlapSession:
    """AES-CBC session after KLAP handshake (python-kasa compatible)."""

    def __init__(self, local_seed: bytes, remote_seed: bytes, user_hash: bytes):
        self._key = hashlib.sha256(
            b"lsk" + local_seed + remote_seed + user_hash
        ).digest()[:16]
        full_iv = hashlib.sha256(b"iv" + local_seed + remote_seed + user_hash).digest()
        self._iv = full_iv[:12]
        self._seq = int.from_bytes(full_iv[-4:], "big", signed=True)
        self._sig = hashlib.sha256(
            b"ldk" + local_seed + remote_seed + user_hash
        ).digest()[:28]

    def encrypt(self, msg: bytes) -> tuple[bytes, int]:
        from Crypto.Cipher import AES  # type: ignore

        self._seq += 1
        iv_seq = self._iv + struct.pack(">l", self._seq)
        cipher = AES.new(self._key, AES.MODE_CBC, iv_seq)
        ct = cipher.encrypt(_pkcs7_pad(msg))
        sig = hashlib.sha256(self._sig + struct.pack(">l", self._seq) + ct).digest()
        return sig + ct, self._seq

    def decrypt(self, msg: bytes) -> bytes:
        from Crypto.Cipher import AES  # type: ignore

        iv_seq = self._iv + struct.pack(">l", self._seq)
        cipher = AES.new(self._key, AES.MODE_CBC, iv_seq)
        return _pkcs7_unpad(cipher.decrypt(msg[32:]))


class _TapoKlapLocal:
    """
    Modern Tapo LAN protocol (KLAP).
    Newer firmware rejects legacy securePassthrough with error_code=1003.
    """

    def __init__(self, ip: str, email: str, password: str):
        self.ip = ip.strip()
        self.email = email.strip()
        self.password = password
        self.terminal_uuid = str(uuid.uuid4())
        self.cookie: str | None = None
        self._session: _KlapSession | None = None
        self._proto: str = "v2"  # v1 | v2

    def _base(self) -> str:
        return f"http://{self.ip}/app"

    @staticmethod
    def _auth_hash_v1(username: str, password: str) -> bytes:
        # md5(md5(user)+md5(pass))
        return hashlib.md5(
            hashlib.md5(username.encode("utf-8")).digest()
            + hashlib.md5(password.encode("utf-8")).digest()
        ).digest()

    @staticmethod
    def _auth_hash_v2(username: str, password: str) -> bytes:
        # sha256(sha1(user)+sha1(pass)) — newer Tapo/Kasa
        return hashlib.sha256(
            hashlib.sha1(username.encode("utf-8")).digest()
            + hashlib.sha1(password.encode("utf-8")).digest()
        ).digest()

    @staticmethod
    def _h1_hash(local_seed: bytes, remote_seed: bytes, auth: bytes, proto: str) -> bytes:
        if proto == "v1":
            return hashlib.sha256(local_seed + auth).digest()
        return hashlib.sha256(local_seed + remote_seed + auth).digest()

    @staticmethod
    def _h2_hash(local_seed: bytes, remote_seed: bytes, auth: bytes, proto: str) -> bytes:
        if proto == "v1":
            return hashlib.sha256(remote_seed + auth).digest()
        return hashlib.sha256(remote_seed + local_seed + auth).digest()

    def _candidate_auths(self) -> list[tuple[str, bytes]]:
        """(label, auth_hash) pairs to try against handshake1 server hash."""
        email = self.email
        pw = self.password
        email_sha1 = _tapo_sha1_hex(email)
        out: list[tuple[str, bytes]] = []
        for proto in ("v2", "v1"):
            gen = self._auth_hash_v2 if proto == "v2" else self._auth_hash_v1
            for label, user in (
                (f"{proto}:email", email),
                (f"{proto}:email_sha1hex", email_sha1),
                (f"{proto}:blank", ""),
            ):
                # blank only once per proto
                if user == "" and pw:
                    out.append((f"{proto}:blank_creds", gen("", "")))
                    continue
                out.append((label, gen(user, pw if user else "")))
            # empty password variants rare
            out.append((f"{proto}:email_empty_pw", gen(email, "")))
        # de-dupe by hash
        seen: set[bytes] = set()
        uniq: list[tuple[str, bytes]] = []
        for lab, h in out:
            if h in seen:
                continue
            seen.add(h)
            uniq.append((lab, h))
        return uniq

    def connect(self) -> None:
        import secrets as _secrets

        local_seed = _secrets.token_bytes(16)
        status, body, cookie = _tapo_http_bytes(
            f"{self._base()}/handshake1",
            local_seed,
            timeout=6.0,
        )
        if status != 200:
            raise RuntimeError(f"KLAP handshake1 HTTP {status}")
        if len(body) < 48:
            # JSON error or unexpected
            try:
                j = json.loads(body.decode("utf-8", errors="replace"))
                raise RuntimeError(
                    f"KLAP handshake1 error_code={j.get('error_code')} (not KLAP?)"
                )
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError(f"KLAP handshake1 bad body len={len(body)}")
        remote_seed = body[:16]
        server_hash = body[16:48]
        if cookie:
            self.cookie = cookie

        matched: tuple[str, bytes] | None = None
        for label, auth in self._candidate_auths():
            proto = "v2" if label.startswith("v2") else "v1"
            if self._h1_hash(local_seed, remote_seed, auth, proto) == server_hash:
                matched = (label, auth)
                self._proto = proto
                break
        if not matched:
            raise RuntimeError(
                "KLAP auth mismatch (bad email/password? case-sensitive)"
            )
        _lab, auth_hash = matched

        h2 = self._h2_hash(local_seed, remote_seed, auth_hash, self._proto)
        status2, body2, cookie2 = _tapo_http_bytes(
            f"{self._base()}/handshake2",
            h2,
            cookie=self.cookie,
            timeout=6.0,
        )
        if cookie2:
            self.cookie = cookie2
        if status2 != 200:
            raise RuntimeError(f"KLAP handshake2 HTTP {status2} (bad email/password?)")
        self._session = _KlapSession(local_seed, remote_seed, auth_hash)

    def _request(self, method: str, params: dict | None = None) -> dict:
        if not self._session:
            raise RuntimeError("KLAP not connected")
        payload = {
            "method": method,
            "params": params or {},
            "requestTimeMils": int(time.time() * 1000),
            "terminalUUID": self.terminal_uuid,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        enc, seq = self._session.encrypt(raw)
        status, body, _ = _tapo_http_bytes(
            f"{self._base()}/request?seq={seq}",
            enc,
            cookie=self.cookie,
            timeout=6.0,
        )
        if status == 403:
            self._session = None
            raise RuntimeError("KLAP session expired (403) — retry")
        if status != 200:
            raise RuntimeError(f"KLAP request HTTP {status}")
        try:
            plain = self._session.decrypt(body)
            return json.loads(plain.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"KLAP decrypt/parse: {e}") from e

    def get_on(self) -> bool:
        inner = self._request("get_device_info")
        if int(inner.get("error_code") or 0) != 0:
            raise RuntimeError(
                f"get_device_info error_code={inner.get('error_code')}"
            )
        res = inner.get("result") or {}
        return bool(res.get("device_on"))

    def set_on(self, on: bool) -> None:
        inner = self._request("set_device_info", {"device_on": bool(on)})
        if int(inner.get("error_code") or 0) != 0:
            raise RuntimeError(
                f"set_device_info error_code={inner.get('error_code')}"
            )


class _TapoP100Local:
    """Legacy local Tapo client (RSA handshake + securePassthrough)."""

    def __init__(self, ip: str, email: str, password: str):
        self.ip = ip.strip()
        self.email = email.strip()
        self.password = password
        self.terminal_uuid = str(uuid.uuid4())
        self.cookie: str | None = None
        self.token: str | None = None
        self.key: bytes | None = None
        self.iv: bytes | None = None
        from Crypto.PublicKey import RSA  # type: ignore

        self._rsa = RSA.generate(1024)
        self._pub_pem = self._rsa.publickey().export_key("PEM").decode("utf-8")
        self._priv_pem = self._rsa.export_key("PEM")

    def _url(self, with_token: bool = False) -> str:
        base = f"http://{self.ip}/app"
        if with_token and self.token:
            return f"{base}?token={self.token}"
        return base

    def handshake(self) -> None:
        from Crypto.Cipher import PKCS1_v1_5  # type: ignore
        from Crypto.PublicKey import RSA  # type: ignore

        payload = {
            "method": "handshake",
            "params": {"key": self._pub_pem, "requestTimeMils": 0},
        }
        j, cookie = _tapo_http_json(self._url(), payload, timeout=4.0)
        if j.get("error_code") not in (0, None) and j.get("error_code") != 0:
            raise RuntimeError(f"handshake error_code={j.get('error_code')}")
        key_b64 = (j.get("result") or {}).get("key")
        if not key_b64:
            raise RuntimeError("handshake: no key (firmware may need KLAP / update)")
        if cookie:
            self.cookie = cookie
        enc = base64.b64decode(key_b64)
        cipher = PKCS1_v1_5.new(RSA.import_key(self._priv_pem))
        dec = cipher.decrypt(enc, None)
        if not dec or len(dec) < 32:
            raise RuntimeError("handshake decrypt failed")
        self.key = dec[:16]
        self.iv = dec[16:32]

    def _encode_creds(self) -> tuple[str, str]:
        # PyP100: password = base64(plain); username = base64(sha1hex(email))
        enc_pw = base64.b64encode(self.password.encode("utf-8")).decode("ascii")
        enc_em = base64.b64encode(_tapo_sha1_hex(self.email).encode("utf-8")).decode(
            "ascii"
        )
        return enc_em, enc_pw

    def login(self) -> None:
        if not self.key or not self.iv:
            raise RuntimeError("handshake first")
        enc_em, enc_pw = self._encode_creds()
        payload = {
            "method": "login_device",
            "params": {"password": enc_pw, "username": enc_em},
            "requestTimeMils": 0,
        }
        secure = {
            "method": "securePassthrough",
            "params": {"request": _tapo_aes_encrypt(self.key, self.iv, json.dumps(payload))},
        }
        j, cookie = _tapo_http_json(
            self._url(), secure, cookie=self.cookie, timeout=4.0
        )
        if cookie:
            self.cookie = cookie
        if j.get("error_code") not in (0, None) and int(j.get("error_code") or 0) != 0:
            raise RuntimeError(f"login passthrough error_code={j.get('error_code')}")
        resp_b64 = (j.get("result") or {}).get("response")
        if not resp_b64:
            raise RuntimeError("login: empty response")
        inner = json.loads(_tapo_aes_decrypt(self.key, self.iv, resp_b64))
        if int(inner.get("error_code") or 0) != 0:
            # retry with sha1 password encoding (some firmwares)
            enc_pw2 = base64.b64encode(
                hashlib.sha1(self.password.encode("utf-8")).digest()
            ).decode("ascii")
            payload2 = {
                "method": "login_device",
                "params": {"password": enc_pw2, "username": enc_em},
                "requestTimeMils": 0,
            }
            secure2 = {
                "method": "securePassthrough",
                "params": {
                    "request": _tapo_aes_encrypt(self.key, self.iv, json.dumps(payload2))
                },
            }
            j2, _ = _tapo_http_json(
                self._url(), secure2, cookie=self.cookie, timeout=4.0
            )
            resp_b64 = (j2.get("result") or {}).get("response")
            if not resp_b64:
                raise RuntimeError(
                    f"login error_code={inner.get('error_code')} (bad email/password?)"
                )
            inner = json.loads(_tapo_aes_decrypt(self.key, self.iv, resp_b64))
            if int(inner.get("error_code") or 0) != 0:
                raise RuntimeError(
                    f"login error_code={inner.get('error_code')} (bad email/password?)"
                )
        self.token = (inner.get("result") or {}).get("token")
        if not self.token:
            raise RuntimeError("login: no token")

    def _device_request(self, method: str, params: dict | None = None) -> dict:
        if not self.token or not self.key or not self.iv:
            raise RuntimeError("not logged in")
        payload = {
            "method": method,
            "params": params or {},
            "requestTimeMils": 0,
            "terminalUUID": self.terminal_uuid,
        }
        secure = {
            "method": "securePassthrough",
            "params": {"request": _tapo_aes_encrypt(self.key, self.iv, json.dumps(payload))},
        }
        j, _ = _tapo_http_json(
            self._url(with_token=True), secure, cookie=self.cookie, timeout=4.0
        )
        if int(j.get("error_code") or 0) != 0:
            raise RuntimeError(f"{method} error_code={j.get('error_code')}")
        resp_b64 = (j.get("result") or {}).get("response")
        if not resp_b64:
            raise RuntimeError(f"{method}: empty response")
        inner = json.loads(_tapo_aes_decrypt(self.key, self.iv, resp_b64))
        if int(inner.get("error_code") or 0) != 0:
            raise RuntimeError(f"{method} inner error_code={inner.get('error_code')}")
        return inner

    def connect(self) -> None:
        self.handshake()
        self.login()

    def get_on(self) -> bool:
        inner = self._device_request("get_device_info")
        res = inner.get("result") or {}
        return bool(res.get("device_on"))

    def set_on(self, on: bool) -> None:
        self._device_request("set_device_info", {"device_on": bool(on)})


def _tapo_connect_client(ip: str, email: str, password: str):
    """
    Prefer KLAP (modern FW). Fall back to legacy securePassthrough.
    Returns client with get_on/set_on.
    """
    klap_err: Exception | None = None
    try:
        c = _TapoKlapLocal(ip, email, password)
        c.connect()
        return c
    except Exception as e:
        klap_err = e
        low = str(e).lower()
        # hard auth failure — no point trying legacy with same wrong password
        if "auth mismatch" in low or "bad email/password" in low:
            raise
    try:
        c2 = _TapoP100Local(ip, email, password)
        c2.connect()
        return c2
    except Exception as e2:
        # prefer KLAP error when legacy is 1003 (expected on new FW)
        low2 = str(e2).lower()
        if "1003" in low2 or "handshake" in low2:
            if klap_err:
                raise RuntimeError(f"Tapo KLAP: {klap_err}") from klap_err
        raise RuntimeError(f"Tapo: {e2}") from e2


def _filtration_cfg_snapshot() -> dict:
    with _filtration_lock:
        return dict(_filtration_cfg)


def _filtration_http(
    url: str,
    *,
    method: str = "GET",
    body: str | bytes | None = None,
    headers: dict | None = None,
    timeout: float = 6.0,
) -> tuple[int, str]:
    method = (method or "GET").upper()
    hdrs = {"User-Agent": "poolheat-filtration/1.0"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None and method in ("POST", "PUT", "PATCH"):
        if isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read().decode("utf-8", errors="replace")


def _filtration_user_error(
    exc: BaseException,
    *,
    on: bool | None = None,
    backend: str | None = None,
    lang: str = "ru",
) -> str:
    """
    Laconic filtration errors for UI / Telegram.

    Не удалось активировать фильтрацию:
    Tapo email/password не настроен
    """
    en = str(lang or "ru").lower().startswith("en")
    raw = str(exc or "").strip()
    low = raw.lower()
    be = str(backend or "").strip().lower()

    if en:
        head_on = "Could not enable filtration:"
        head_off = "Could not disable filtration:"
        head_test = "Filtration check failed:"
    else:
        head_on = "Не удалось активировать фильтрацию:"
        head_off = "Не удалось выключить фильтрацию:"
        head_test = "Не удалось проверить фильтрацию:"

    if on is True:
        head = head_on
    elif on is False:
        head = head_off
    else:
        head = head_test

    # --- classify ---
    reason = None
    if be == "tapo" or "tapo" in low:
        if (
            "email/password empty" in low
            or "email empty" in low
            or "password empty" in low
            or ("email" in low and "password" in low and "empty" in low)
            or "не настроен" in low
        ):
            reason = (
                "Tapo email/password not configured"
                if en
                else "Tapo email/password не настроен"
            )
        elif (
            "bad email/password" in low
            or "auth mismatch" in low
            or "login error" in low
            or "login_device" in low
            or "invalid" in low
            or "unauthorized" in low
            or "error_code=-1501" in low
            or "error_code= -1501" in low
            or "не действитель" in low
        ):
            reason = (
                "Tapo email/password invalid"
                if en
                else "Tapo email/password не действительны"
            )
        elif "1003" in low or ("klap" in low and ("handshake" in low or "auth" in low)):
            reason = (
                "Tapo KLAP auth failed (email/password?)"
                if en
                else "Tapo KLAP: email/password не действительны"
            )
        elif "ip empty" in low or ("ip" in low and "empty" in low):
            reason = "Tapo IP not configured" if en else "Tapo IP не настроен"
        elif (
            "timed out" in low
            or "timeout" in low
            or "unreachable" in low
            or "no route" in low
            or "network is unreachable" in low
        ):
            # local plug first (handshake is LAN)
            reason = (
                "Tapo device unreachable"
                if en
                else "Tapo устройство недоступно"
            )
        elif (
            "connection refused" in low
            or "reset by peer" in low
            or "name or service not known" in low
            or "nodename nor servname" in low
            or "failed to resolve" in low
        ):
            reason = (
                "Tapo device unreachable"
                if en
                else "Tapo устройство недоступно"
            )
        elif "handshake" in low or "decrypt" in low or "1003" in low:
            # often new FW (KLAP) or wrong protocol — not pure LAN timeout
            reason = (
                "Tapo protocol/auth failed (KLAP / password)"
                if en
                else "Tapo: ошибка протокола/пароля (KLAP)"
            )
        elif "server" in low or "cloud" in low or "api.tapo" in low:
            reason = (
                "Tapo server unavailable"
                if en
                else "Tapo сервер недоступен"
            )
        else:
            reason = (
                "Tapo device unreachable"
                if en
                else "Tapo устройство недоступно"
            )
    elif "disabled" in low or "отключ" in low:
        reason = (
            "filtration disabled in settings"
            if en
            else "фильтрация отключена в настройках"
        )
    elif "майнинг" in low or "mining" in low:
        reason = raw  # already laconic RU
    else:
        # generic backends — keep short
        reason = raw[:120] if raw else ("unknown error" if en else "неизвестная ошибка")

    return f"{head}\n{reason}"


def _filtration_backend_tapo(on: bool | None, cfg: dict) -> dict:
    """on=None → read only; else set. Uses KLAP (new FW) or legacy."""
    ip = str(cfg.get("ip") or "").strip()
    email = str(cfg.get("email") or "").strip()
    password = str(cfg.get("password") or "")
    if not ip:
        raise ValueError("Tapo IP empty")
    if not email or not password:
        raise ValueError("Tapo email/password empty")
    try:
        c = _tapo_connect_client(ip, email, password)
    except Exception as e:
        # re-raise with markers for _filtration_user_error
        low = str(e).lower()
        if (
            "email/password" in low
            or "login" in low
            or "auth mismatch" in low
            or "error_code" in low
        ):
            raise RuntimeError(f"Tapo {e}") from e
        if "timed out" in low or "timeout" in low:
            raise RuntimeError(f"Tapo device timeout: {e}") from e
        if "refused" in low or "unreachable" in low or "reset" in low:
            raise RuntimeError(f"Tapo device unreachable: {e}") from e
        raise RuntimeError(f"Tapo: {e}") from e
    if on is None:
        return {"on": c.get_on(), "backend": "tapo", "ip": ip}
    try:
        c.set_on(bool(on))
    except Exception as e:
        raise RuntimeError(f"Tapo device: {e}") from e
    try:
        got = c.get_on()
    except Exception:
        got = bool(on)
    return {"on": bool(got), "backend": "tapo", "ip": ip}


def _filtration_backend_ewelink(on: bool | None, cfg: dict) -> dict:
    """Sonoff DIY LAN (eWeLink DIY mode) · port 8081."""
    ip = str(cfg.get("ip") or "").strip()
    device_id = str(cfg.get("device_id") or "").strip()
    port = int(cfg.get("ewelink_port") or 8081)
    if not ip:
        raise ValueError("eWeLink IP empty")
    if not device_id:
        raise ValueError("eWeLink device_id empty (DIY deviceid)")
    base = f"http://{ip}:{port}"
    if on is None:
        # info
        url = f"{base}/zeroconf/info"
        payload = json.dumps({"deviceid": device_id, "data": {}})
        code, text = _filtration_http(url, method="POST", body=payload)
        j = json.loads(text) if text else {}
        data = j.get("data") if isinstance(j, dict) else {}
        sw = None
        if isinstance(data, dict):
            sw = data.get("switch")
        on_v = str(sw).lower() in ("on", "1", "true") if sw is not None else None
        return {"on": on_v, "backend": "ewelink", "ip": ip, "http": code}
    url = f"{base}/zeroconf/switch"
    payload = json.dumps(
        {"deviceid": device_id, "data": {"switch": "on" if on else "off"}}
    )
    code, text = _filtration_http(url, method="POST", body=payload)
    j = json.loads(text) if text.strip().startswith("{") else {}
    err = j.get("error") if isinstance(j, dict) else None
    if err not in (0, None, "0"):
        raise RuntimeError(f"eWeLink DIY error={err}: {text[:120]}")
    return {"on": bool(on), "backend": "ewelink", "ip": ip, "http": code}


def _filtration_backend_webhook(on: bool | None, cfg: dict) -> dict:
    if on is None:
        # no reliable read for generic webhooks
        return {
            "on": cfg.get("last_on"),
            "backend": "webhook",
            "note": "webhook has no state read",
        }
    url = str(
        cfg.get("webhook_on_url") if on else cfg.get("webhook_off_url") or ""
    ).strip()
    if not url:
        raise ValueError("webhook URL empty (on/off)")
    method = str(cfg.get("webhook_method") or "GET").upper()
    body = str(
        cfg.get("webhook_body_on") if on else cfg.get("webhook_body_off") or ""
    )
    headers = {}
    raw_h = str(cfg.get("webhook_headers") or "").strip()
    if raw_h:
        try:
            h = json.loads(raw_h)
            if isinstance(h, dict):
                headers = {str(k): str(v) for k, v in h.items()}
        except Exception as e:
            raise ValueError(f"webhook_headers JSON: {e}") from e
    code, text = _filtration_http(
        url,
        method=method if body or method == "POST" else method,
        body=body if method == "POST" else None,
        headers=headers or None,
    )
    if code >= 400:
        raise RuntimeError(f"webhook HTTP {code}: {text[:120]}")
    return {"on": bool(on), "backend": "webhook", "http": code}


def _filtration_backend_shelly(on: bool | None, cfg: dict) -> dict:
    ip = str(cfg.get("ip") or "").strip()
    if not ip:
        raise ValueError("Shelly IP empty")
    ch = int(cfg.get("shelly_channel") or 0)
    gen = str(cfg.get("shelly_gen") or "auto").lower()

    def gen1_set(turn: bool) -> tuple[int, str]:
        t = "on" if turn else "off"
        return _filtration_http(f"http://{ip}/relay/{ch}?turn={t}")

    def gen1_get() -> bool | None:
        try:
            code, text = _filtration_http(f"http://{ip}/relay/{ch}")
            j = json.loads(text)
            if isinstance(j, dict) and "ison" in j:
                return bool(j["ison"])
        except Exception:
            return None
        return None

    def gen2_set(turn: bool) -> tuple[int, str]:
        # Shelly Gen2 RPC
        payload = json.dumps(
            {"id": 1, "method": "Switch.Set", "params": {"id": ch, "on": bool(turn)}}
        )
        return _filtration_http(
            f"http://{ip}/rpc", method="POST", body=payload
        )

    def gen2_get() -> bool | None:
        try:
            payload = json.dumps(
                {"id": 1, "method": "Switch.GetStatus", "params": {"id": ch}}
            )
            code, text = _filtration_http(
                f"http://{ip}/rpc", method="POST", body=payload
            )
            j = json.loads(text)
            res = j.get("result") if isinstance(j, dict) else None
            if isinstance(res, dict) and "output" in res:
                return bool(res["output"])
        except Exception:
            return None
        return None

    if on is None:
        if gen in ("1", "auto"):
            v = gen1_get()
            if v is not None:
                return {"on": v, "backend": "shelly", "gen": "1", "ip": ip}
        if gen in ("2", "auto"):
            v = gen2_get()
            if v is not None:
                return {"on": v, "backend": "shelly", "gen": "2", "ip": ip}
        return {"on": cfg.get("last_on"), "backend": "shelly", "ip": ip}

    if gen == "1":
        code, text = gen1_set(bool(on))
        if code >= 400:
            raise RuntimeError(f"Shelly gen1 HTTP {code}: {text[:80]}")
        return {"on": bool(on), "backend": "shelly", "gen": "1", "ip": ip}
    if gen == "2":
        code, text = gen2_set(bool(on))
        if code >= 400:
            raise RuntimeError(f"Shelly gen2 HTTP {code}: {text[:80]}")
        return {"on": bool(on), "backend": "shelly", "gen": "2", "ip": ip}
    # auto: try gen1 then gen2
    try:
        code, text = gen1_set(bool(on))
        if code < 400:
            return {"on": bool(on), "backend": "shelly", "gen": "1", "ip": ip}
    except Exception:
        pass
    code, text = gen2_set(bool(on))
    if code >= 400:
        raise RuntimeError(f"Shelly HTTP {code}: {text[:80]}")
    return {"on": bool(on), "backend": "shelly", "gen": "2", "ip": ip}


def _filtration_backend_ha(on: bool | None, cfg: dict) -> dict:
    base = str(cfg.get("ha_url") or "").rstrip("/")
    token = str(cfg.get("ha_token") or "")
    entity = str(cfg.get("ha_entity_id") or "").strip()
    if not base:
        raise ValueError("Home Assistant URL empty")
    if not token:
        raise ValueError("Home Assistant token empty")
    if not entity:
        raise ValueError("Home Assistant entity_id empty")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if on is None:
        code, text = _filtration_http(
            f"{base}/api/states/{urllib.parse.quote(entity, safe='')}",
            method="GET",
            headers=headers,
        )
        j = json.loads(text) if text else {}
        st = str(j.get("state") or "").lower() if isinstance(j, dict) else ""
        on_v = st in ("on", "open", "true", "1")
        return {"on": on_v, "backend": "homeassistant", "entity": entity, "http": code}
    domain = entity.split(".", 1)[0] if "." in entity else "switch"
    service = "turn_on" if on else "turn_off"
    payload = json.dumps({"entity_id": entity})
    code, text = _filtration_http(
        f"{base}/api/services/{domain}/{service}",
        method="POST",
        body=payload,
        headers=headers,
    )
    if code >= 400:
        raise RuntimeError(f"HA HTTP {code}: {text[:120]}")
    return {"on": bool(on), "backend": "homeassistant", "entity": entity, "http": code}


def _filtration_dispatch(on: bool | None) -> dict:
    """on=None read; else set. Uses current cfg backend."""
    cfg = _filtration_cfg_snapshot()
    be = str(cfg.get("backend") or "tapo").lower()
    if be == "tapo":
        return _filtration_backend_tapo(on, cfg)
    if be in ("ewelink", "sonoff", "sonoff_diy"):
        return _filtration_backend_ewelink(on, cfg)
    if be == "webhook":
        return _filtration_backend_webhook(on, cfg)
    if be == "shelly":
        return _filtration_backend_shelly(on, cfg)
    if be in ("homeassistant", "ha"):
        return _filtration_backend_ha(on, cfg)
    raise ValueError(f"unknown backend: {be}")


def filtration_test() -> dict:
    """Backend connectivity + optional state read."""
    try:
        with _filtration_lock:
            if not _filtration_cfg.get("enabled"):
                # allow test even if disabled for setup
                pass
            be = str(_filtration_cfg.get("backend") or "tapo")
        out = _filtration_dispatch(None)
        on = out.get("on")
        with _filtration_lock:
            if on is not None:
                _filtration_cfg["last_on"] = bool(on)
            _filtration_cfg["last_error"] = None
            _filtration_cfg["last_ok_ts"] = datetime.now().isoformat(timespec="seconds")
            _filtration_cfg["last_action"] = f"test:{be}"
        _save_filtration_cfg()
        return {"ok": True, "backend": be, **out}
    except Exception as e:
        pretty = _filtration_user_error(e, on=None, backend=be, lang="ru")
        with _filtration_lock:
            _filtration_cfg["last_error"] = pretty
            _filtration_cfg["last_action"] = "test_fail"
        _save_filtration_cfg()
        return {"ok": False, "error": pretty}


def filtration_set(on: bool, *, source: str = "manual", force: bool = False) -> dict:
    """
    Turn filtration on/off via selected backend.
    OFF while mining is refused unless force (auto-sync / internal).
    """
    on = bool(on)
    with _filtration_lock:
        if not _filtration_cfg.get("enabled"):
            raise RuntimeError("filtration disabled in settings")
        be = str(_filtration_cfg.get("backend") or "tapo")
    if not on and not force:
        with _filtration_lock:
            allow_off = bool(_filtration_cfg.get("allow_off_while_mining", False))
        if not allow_off:
            try:
                live = fetch_live()
                if _live_work(live) == "resume":
                    raise RuntimeError(
                        "нельзя выключить фильтрацию при майнинге"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
    with _filtration_lock:
        prev_on = _filtration_cfg.get("last_on")
    try:
        out = _filtration_dispatch(on)
        got = out.get("on")
        if got is None:
            got = on
        with _filtration_lock:
            _filtration_cfg["last_on"] = bool(got)
            _filtration_cfg["last_error"] = None
            _filtration_cfg["last_ok_ts"] = datetime.now().isoformat(timespec="seconds")
            _filtration_cfg["last_sync_ts"] = _filtration_cfg["last_ok_ts"]
            _filtration_cfg["last_action"] = f"{source}:{be}:{'on' if on else 'off'}"
        _save_filtration_cfg()
        # TG push on real state change. Skip source=telegram — handler already
        # replies with the same text + updated main keyboard.
        if prev_on is not bool(got) and str(source or "") != "telegram":
            try:
                _tg_notify_filtration(bool(got), source=source)
            except Exception as _tg_e:
                print(f"[filtration] tg notify: {_tg_e}")
        return {"ok": True, "on": bool(got), "source": source, "backend": be, **out}
    except Exception as e:
        pretty = _filtration_user_error(e, on=on, backend=be, lang="ru")
        with _filtration_lock:
            _filtration_cfg["last_error"] = pretty
            _filtration_cfg["last_action"] = f"{source}_fail"
        _save_filtration_cfg()
        return {
            "ok": False,
            "error": pretty,
            "source": source,
            "backend": be,
        }


def filtration_sync_with_mining(measured_work: str | None) -> None:
    """
    Called from policy_tick.
    - mining (resume) + auto_on_mining → ensure ON
    - suspend + auto_off_suspend → ensure OFF
    """
    now = time.time()
    with _filtration_lock:
        if not _filtration_cfg.get("enabled"):
            return
        auto_on = bool(_filtration_cfg.get("auto_on_mining", True))
        auto_off = bool(_filtration_cfg.get("auto_off_suspend", False))
        last_on = _filtration_cfg.get("last_on")
        be = str(_filtration_cfg.get("backend") or "tapo")
        # readiness check per backend
        if be == "tapo" and not str(_filtration_cfg.get("ip") or "").strip():
            return
        if be == "ewelink" and (
            not str(_filtration_cfg.get("ip") or "").strip()
            or not str(_filtration_cfg.get("device_id") or "").strip()
        ):
            return
        if be == "webhook" and not (
            str(_filtration_cfg.get("webhook_on_url") or "").strip()
            or str(_filtration_cfg.get("webhook_off_url") or "").strip()
        ):
            return
        if be == "shelly" and not str(_filtration_cfg.get("ip") or "").strip():
            return
        if be == "homeassistant" and not (
            str(_filtration_cfg.get("ha_url") or "").strip()
            and str(_filtration_cfg.get("ha_entity_id") or "").strip()
        ):
            return
        last_err = _filtration_cfg.get("last_error")
        last_act = str(_filtration_cfg.get("last_action") or "")
        if last_err and last_act.endswith("_fail"):
            try:
                last_try = float(_filtration_session.get("last_try_ts") or 0)
            except Exception:
                last_try = 0.0
            if now - last_try < 20.0:
                return
        _filtration_session["last_try_ts"] = now
    work = str(measured_work or "").lower()
    want: bool | None = None
    src = "auto"
    if work == "resume" and auto_on:
        if last_on is not True:
            want = True
            src = "auto_mining"
    elif work in ("suspend", "sleep") and auto_off:
        if last_on is not False:
            want = False
            src = "auto_suspend"
    if want is None:
        return
    try:
        filtration_set(want, source=src, force=True)
    except Exception as e:
        print(f"[filtration] sync fail: {e}")


def get_filtration_status(*, probe_live: bool = False) -> dict:
    """
    Filtration UI/TG snapshot.

    mining state comes from live cache / policy by default — NEVER hit the miner
    on every keyboard rebuild (was making Events/Status 5s+).
    probe_live=True only for rare explicit checks.
    """
    cfg = get_filtration_cfg(redact=True)
    mining = None
    try:
        live = None
        if probe_live:
            live = fetch_live()
        else:
            with _cache_lock:
                if isinstance(_cache, dict) and _cache.get("ok"):
                    live = dict(_cache)
        if isinstance(live, dict) and live:
            mining = _live_work(live) == "resume"
        if mining is None:
            with _policy_lock:
                mw = str(_policy_ctrl.get("measured_work") or "").strip().lower()
            if mw in ("resume", "mining"):
                mining = True
            elif mw in ("suspend", "sleep"):
                mining = False
    except Exception:
        pass
    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled")),
        "backend": cfg.get("backend") or "tapo",
        "backends": cfg.get("backends") or list_filtration_backends(),
        "on": cfg.get("last_on"),
        "mining": mining,
        "auto_on_mining": bool(cfg.get("auto_on_mining", True)),
        "auto_off_suspend": bool(cfg.get("auto_off_suspend", False)),
        "allow_off_while_mining": bool(cfg.get("allow_off_while_mining", False)),
        "ip": cfg.get("ip"),
        "email": cfg.get("email"),
        "device_id": cfg.get("device_id"),
        "ewelink_port": cfg.get("ewelink_port"),
        "webhook_on_url": cfg.get("webhook_on_url"),
        "webhook_off_url": cfg.get("webhook_off_url"),
        "webhook_method": cfg.get("webhook_method"),
        "webhook_body_on": cfg.get("webhook_body_on"),
        "webhook_body_off": cfg.get("webhook_body_off"),
        "webhook_headers": cfg.get("webhook_headers"),
        "shelly_channel": cfg.get("shelly_channel"),
        "shelly_gen": cfg.get("shelly_gen"),
        "ha_url": cfg.get("ha_url"),
        "ha_entity_id": cfg.get("ha_entity_id"),
        "password_set": bool(cfg.get("password_set")),
        "ha_token_set": bool(cfg.get("ha_token_set")),
        "last_error": cfg.get("last_error"),
        "last_ok_ts": cfg.get("last_ok_ts"),
        "last_action": cfg.get("last_action"),
        # OFF blocked while mining unless allow_off_while_mining
        "can_turn_off": bool(cfg.get("allow_off_while_mining", False))
        or not (bool(cfg.get("enabled")) and mining is True),
    }


# ── Chip map (LuCI Miner API Log scrape) ─────────────────────────────────────
# Source: https://<miner>/cgi-bin/luci/admin/status/btminerapi
# Per-chip: C0..C263 × slot 0..3 · temp · freq · vol · nonce · pct · err
# Hash map metric = nonce (work found); pct is a separate API field, not hashrate.

DEFAULT_CHIPMAP_CFG: dict = {
    "enabled": True,
    "poll_interval_sec": 30,  # separate from miner live poll
    "web_user": "admin",
    "web_password": "",  # empty → DEFAULT_API_PASSWORD
    "web_scheme": "https",  # https | http
    "verify_tls": False,
    "persist_cache": True,  # write chipmap_cache.json under DATA
}

_chipmap_lock = threading.Lock()
_chipmap_cfg: dict = dict(DEFAULT_CHIPMAP_CFG)
_chipmap_cache: dict = {
    "ok": False,
    "ts": None,
    "fetch_ms": None,
    "error": None,
    "boards": [],
    "chip_count": 0,
    "temp_min": None,
    "temp_max": None,
    "temp_avg": None,
}
_chipmap_stop = threading.Event()

_CHIP_LINE_RE = re.compile(
    r"C(\d+)\s+freq:(\d+)\s+vol:(\d+)\s+temp:([\d.]+)\s+"
    r"nonce:(\d+)\s+err:(\d+)\s+crc:(\d+)\s+"
    r"x:(\d+)\s*/\s*(\d+)\s+repeat:(\d+)\s+"
    r"pct:\s*([\d.]+)\s*%\s*/\s*([\d.]+)\s*%",
    re.I,
)
_SLOT_HDR_RE = re.compile(
    r"slot:\s*(\d+)\s*,\s*freq:\s*([\d.]+)\s*,\s*temp:\s*([\d.]+)",
    re.I,
)


def _load_chipmap_cfg() -> None:
    global _chipmap_cfg
    with _chipmap_lock:
        raw = _load_json(CHIPMAP_CFG_FILE, DEFAULT_CHIPMAP_CFG)
        if not isinstance(raw, dict):
            raw = {}
        cfg = dict(DEFAULT_CHIPMAP_CFG)
        cfg["enabled"] = bool(raw.get("enabled", True))
        try:
            pi = int(raw.get("poll_interval_sec", 30) or 30)
        except (TypeError, ValueError):
            pi = 30
        cfg["poll_interval_sec"] = max(10, min(600, pi))
        cfg["web_user"] = str(raw.get("web_user") or "admin").strip() or "admin"
        cfg["web_password"] = str(raw.get("web_password") or "")
        sch = str(raw.get("web_scheme") or "https").strip().lower()
        cfg["web_scheme"] = "http" if sch == "http" else "https"
        cfg["verify_tls"] = bool(raw.get("verify_tls", False))
        cfg["persist_cache"] = bool(raw.get("persist_cache", True))
        _chipmap_cfg = cfg


def _save_chipmap_cfg() -> None:
    with _chipmap_lock:
        _save_json(CHIPMAP_CFG_FILE, _chipmap_cfg)


def _chipmap_persist_cache_enabled() -> bool:
    with _chipmap_lock:
        return bool(_chipmap_cfg.get("persist_cache", True))


def _chipmap_cache_is_persistable(data: dict | None) -> bool:
    """Only persist successful scrapes with board/chip data."""
    if not isinstance(data, dict):
        return False
    if data.get("reason") in ("suspend", "disabled", "error"):
        return False
    if data.get("ok") is False and not data.get("boards"):
        return False
    boards = data.get("boards")
    if not isinstance(boards, list) or not boards:
        return False
    try:
        n = int(data.get("chip_count") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = sum(len(b.get("chips") or []) for b in boards if isinstance(b, dict))
    return n > 0


def _clear_chipmap_cache_disk() -> None:
    """Remove on-disk chipmap cache (when persist is turned off)."""
    try:
        if CHIPMAP_CACHE_FILE.is_file():
            CHIPMAP_CACHE_FILE.unlink()
            print("[chipmap] disk cache removed (persist_cache off)")
    except Exception as e:
        print(f"[chipmap] cache unlink: {e}")


def _save_chipmap_cache_disk(data: dict | None = None) -> None:
    """Write last good chipmap to disk (survives service restart)."""
    try:
        if not _chipmap_persist_cache_enabled():
            return
        with _chipmap_lock:
            payload = dict(data) if isinstance(data, dict) else dict(_chipmap_cache)
        if not _chipmap_cache_is_persistable(payload):
            return
        # strip volatile / huge noise if any
        to_save = {
            k: payload.get(k)
            for k in (
                "ok",
                "ts",
                "fetch_ms",
                "error",
                "reason",
                "message",
                "source",
                "host",
                "miner_type",
                "boards",
                "chip_count",
                "board_count",
                "temp_min",
                "temp_max",
                "temp_avg",
                "nonce_total",
                "hashrate_th",
                "mining_elapsed",
            )
            if k in payload or k in ("ok", "ts", "boards", "chip_count")
        }
        to_save["ok"] = True
        to_save["persisted"] = True
        to_save["persisted_at"] = datetime.now().isoformat(timespec="seconds")
        CHIPMAP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHIPMAP_CACHE_FILE.write_text(
            json.dumps(to_save, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[chipmap] cache save: {e}")


def _load_chipmap_cache_disk() -> None:
    """Restore last chipmap into RAM on boot (if persist_cache enabled)."""
    global _chipmap_cache
    try:
        if not _chipmap_persist_cache_enabled():
            return
        if not CHIPMAP_CACHE_FILE.is_file():
            return
        raw = json.loads(CHIPMAP_CACHE_FILE.read_text(encoding="utf-8"))
        if not _chipmap_cache_is_persistable(raw):
            return
        raw = dict(raw)
        raw["stale"] = True
        raw["source"] = raw.get("source") or "disk"
        raw["from_disk"] = True
        with _chipmap_lock:
            _chipmap_cache.clear()
            _chipmap_cache.update(raw)
        n = raw.get("chip_count") or 0
        print(f"[chipmap] restored cache from disk · chips {n} · ts {raw.get('ts')}")
    except Exception as e:
        print(f"[chipmap] cache load: {e}")


def get_chipmap_cfg(*, redact: bool = True) -> dict:
    with _chipmap_lock:
        cfg = dict(_chipmap_cfg)
    if redact:
        if cfg.get("web_password"):
            p = str(cfg["web_password"])
            cfg["web_password_set"] = True
            cfg["web_password"] = (p[:2] + "…" + p[-2:]) if len(p) > 6 else "••••"
        else:
            # empty means fallback to miner API password
            cfg["web_password_set"] = bool(DEFAULT_API_PASSWORD)
            cfg["web_password"] = ""
            cfg["web_password_uses_miner"] = True
    else:
        cfg["web_password_set"] = bool(cfg.get("web_password") or DEFAULT_API_PASSWORD)
    return cfg


def apply_chipmap_cfg(req: dict) -> dict:
    if not isinstance(req, dict):
        raise ValueError("expected object")
    with _chipmap_lock:
        if "enabled" in req:
            _chipmap_cfg["enabled"] = bool(req["enabled"])
        if "poll_interval_sec" in req and req["poll_interval_sec"] is not None:
            try:
                _chipmap_cfg["poll_interval_sec"] = max(
                    10, min(600, int(req["poll_interval_sec"]))
                )
            except (TypeError, ValueError):
                pass
        if "web_user" in req and req["web_user"] is not None:
            _chipmap_cfg["web_user"] = str(req["web_user"]).strip() or "admin"
        if "web_password" in req and req["web_password"] is not None and str(req["web_password"]) != "":
            _chipmap_cfg["web_password"] = str(req["web_password"])
        if "web_scheme" in req and req["web_scheme"] is not None:
            sch = str(req["web_scheme"]).strip().lower()
            _chipmap_cfg["web_scheme"] = "http" if sch == "http" else "https"
        if "verify_tls" in req:
            _chipmap_cfg["verify_tls"] = bool(req["verify_tls"])
        if "persist_cache" in req:
            _chipmap_cfg["persist_cache"] = bool(req["persist_cache"])
    turned_off = (
        isinstance(req, dict)
        and "persist_cache" in req
        and not bool(req.get("persist_cache"))
    )
    _save_chipmap_cfg()
    if turned_off:
        _clear_chipmap_cache_disk()
    return get_chipmap_cfg(redact=True)


def _chipmap_web_password() -> str:
    with _chipmap_lock:
        pw = str(_chipmap_cfg.get("web_password") or "").strip()
    return pw if pw else str(DEFAULT_API_PASSWORD or "admin")


def _chipmap_base_url() -> str:
    with _chipmap_lock:
        scheme = _chipmap_cfg.get("web_scheme") or "https"
    with _miner_cfg_lock:
        host = HOST_MINER
    return f"{scheme}://{host}"


def parse_chipmap_log(text: str) -> dict:
    """Parse LuCI Miner API Log textarea into boards/chips structure."""
    boards_map: dict[int, dict] = {}
    current_slot: int | None = None
    temps: list[float] = []

    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        mh = _SLOT_HDR_RE.search(line)
        if mh:
            current_slot = int(mh.group(1))
            if current_slot not in boards_map:
                boards_map[current_slot] = {
                    "slot": current_slot,
                    "board_freq": _f(mh.group(2)),
                    "board_temp": _f(mh.group(3)),
                    "chips": [],
                }
            else:
                boards_map[current_slot]["board_freq"] = _f(mh.group(2))
                boards_map[current_slot]["board_temp"] = _f(mh.group(3))
            continue
        mc = _CHIP_LINE_RE.search(line)
        if mc and current_slot is not None:
            if current_slot not in boards_map:
                boards_map[current_slot] = {
                    "slot": current_slot,
                    "board_freq": None,
                    "board_temp": None,
                    "chips": [],
                }
            temp = _f(mc.group(4))
            pct = _f(mc.group(11))
            chip = {
                "id": int(mc.group(1)),
                "freq": int(mc.group(2)),
                "vol": int(mc.group(3)),
                "temp": temp,
                "nonce": int(mc.group(5)),
                "err": int(mc.group(6)),
                "crc": int(mc.group(7)),
                "x": int(mc.group(8)),
                "x2": int(mc.group(9)),
                "repeat": int(mc.group(10)),
                "pct": pct,
                "pct2": _f(mc.group(12)),
            }
            boards_map[current_slot]["chips"].append(chip)
            if temp is not None:
                temps.append(float(temp))

    boards = [boards_map[k] for k in sorted(boards_map.keys())]
    for b in boards:
        b["chips"].sort(key=lambda c: c["id"])
        b["chip_count"] = len(b["chips"])
        cts = [c["temp"] for c in b["chips"] if c.get("temp") is not None]
        b["temp_min"] = min(cts) if cts else None
        b["temp_max"] = max(cts) if cts else None
        b["temp_avg"] = (sum(cts) / len(cts)) if cts else None
        nsum = sum(int(c.get("nonce") or 0) for c in b["chips"])
        b["nonce_sum"] = nsum
        # relative share of board hash work (nonce / board total)
        for c in b["chips"]:
            try:
                n = int(c.get("nonce") or 0)
            except (TypeError, ValueError):
                n = 0
            c["hash_share"] = (
                round(100.0 * n / nsum, 3) if nsum > 0 else 0.0
            )

    chip_count = sum(b["chip_count"] for b in boards)
    total_nonce = sum(int(b.get("nonce_sum") or 0) for b in boards)
    return {
        "boards": boards,
        "chip_count": chip_count,
        "board_count": len(boards),
        "nonce_total": total_nonce,
        "temp_min": min(temps) if temps else None,
        "temp_max": max(temps) if temps else None,
        "temp_avg": (sum(temps) / len(temps)) if temps else None,
    }


def _chipmap_attach_hash_estimates(payload: dict) -> dict:
    """
    Per-chip hashrate estimate using mining Elapsed as time base.

    If nonce is cumulative since mining start:
      nonce_rate = nonce / elapsed          (nonce/s)
      est_th     = HR_total × nonce / Σnonce (TH/s)

    Share method is preferred for TH/s (elapsed cancels out when nonce
    is proportional to work over the same window). Elapsed is still used
    for nonce/s and status, and as a sanity check (elapsed > 0).
    """
    if not isinstance(payload, dict):
        return payload
    if payload.get("reason") == "suspend":
        return payload
    boards = payload.get("boards")
    if not isinstance(boards, list) or not boards:
        return payload

    elapsed = None
    hr = None
    try:
        live = fetch_live()
        elapsed = _f(live.get("elapsed"))
        hr = _f(live.get("hashrate_th"))
    except Exception:
        pass

    total_nonce = int(payload.get("nonce_total") or 0)
    if total_nonce <= 0:
        total_nonce = 0
        for b in boards:
            for c in b.get("chips") or []:
                try:
                    total_nonce += int(c.get("nonce") or 0)
                except (TypeError, ValueError):
                    pass
        payload["nonce_total"] = total_nonce

    payload["mining_elapsed"] = elapsed
    payload["hashrate_th"] = hr
    el = float(elapsed) if elapsed is not None and float(elapsed) > 0 else None
    hr_ok = hr is not None and float(hr) > 0

    for b in boards:
        for c in b.get("chips") or []:
            try:
                n = int(c.get("nonce") or 0)
            except (TypeError, ValueError):
                n = 0
            if el is not None:
                c["nonce_rate"] = round(n / el, 6)  # nonce / s over mining elapsed
            else:
                c["nonce_rate"] = None
            if total_nonce > 0:
                share = n / float(total_nonce)
                c["hash_share_asic"] = round(100.0 * share, 4)
                if hr_ok:
                    # primary estimate: distribute measured ASIC HR by nonce share
                    c["est_th"] = round(float(hr) * share, 4)
                else:
                    c["est_th"] = None
            else:
                c["hash_share_asic"] = 0.0
                c["est_th"] = None
    return payload


def _chipmap_is_mining() -> tuple[bool | None, str]:
    """
    Whether boards are powered for chip log.
    Returns (is_mining, detail):
      True  — mining / boards expected live
      False — suspend / sleep (chip map N/A by design)
      None  — miner live API unreachable (real offline / auth issue possible)
    """
    try:
        live = fetch_live()
    except Exception as e:
        return None, str(e)
    try:
        work = str(live.get("work_measured") or "").strip().lower()
        if not work:
            work = _measured_work_state(live)
        if work in ("suspend", "sleep"):
            return False, "suspend"
        if work in ("resume", "mining"):
            return True, "mining"
        # fallback measured
        mw = _measured_work_state(live)
        if mw in ("sleep", "suspend"):
            return False, "suspend"
        return True, mw or "mining"
    except Exception as e:
        return None, str(e)


def _chipmap_suspend_payload(*, fetch_ms: int | None = None) -> dict:
    """Friendly non-error status when ASIC is online but Suspend (no board power)."""
    msg = (
        "Mining Suspend · карты чипов доступны только при майнинге "
        "(когда есть питание на платах)."
    )
    out = {
        "ok": True,
        "reason": "suspend",
        "message": msg,
        "error": None,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "fetch_ms": fetch_ms if fetch_ms is not None else 0,
        "source": "mining_state",
        "boards": [],
        "board_count": 0,
        "chip_count": 0,
        "temp_min": None,
        "temp_max": None,
        "temp_avg": None,
        "stale": False,
    }
    with _chipmap_lock:
        prev = dict(_chipmap_cache)
        # keep last good boards in RAM (and on disk) for after restart / resume
        if (
            prev.get("boards")
            and prev.get("chip_count")
            and prev.get("reason") != "suspend"
        ):
            out["boards"] = prev["boards"]
            out["board_count"] = prev.get("board_count") or len(prev["boards"])
            out["chip_count"] = prev.get("chip_count")
            out["temp_min"] = prev.get("temp_min")
            out["temp_max"] = prev.get("temp_max")
            out["temp_avg"] = prev.get("temp_avg")
            out["miner_type"] = prev.get("miner_type")
            out["stale"] = True
            out["last_good_ts"] = prev.get("ts")
            out["message"] = msg + " · показан последний кеш"
        _chipmap_cache.clear()
        _chipmap_cache.update(out)
    # never overwrite disk with empty suspend payload
    return dict(out)


def fetch_chipmap_from_luci(*, force: bool = False) -> dict:
    """
    Login to miner LuCI and scrape Miner API Log.
    Returns cache-shaped dict with ok/error.
    Suspend (miner offline boards) → ok + reason=suspend, not an access error.
    """
    t0 = time.time()
    with _chipmap_lock:
        enabled = bool(_chipmap_cfg.get("enabled", True))
        user = str(_chipmap_cfg.get("web_user") or "admin")
        verify = bool(_chipmap_cfg.get("verify_tls", False))
    if not enabled and not force:
        with _chipmap_lock:
            out = dict(_chipmap_cache)
        out["ok"] = False
        out["error"] = "chipmap disabled"
        out["reason"] = "disabled"
        return out

    # Suspend first: empty chip log is expected — do not scrape LuCI / show auth errors
    mining, mining_detail = _chipmap_is_mining()
    if mining is False:
        return _chipmap_suspend_payload(
            fetch_ms=int((time.time() - t0) * 1000)
        )

    password = _chipmap_web_password()
    base = _chipmap_base_url()
    try:
        import ssl as _ssl
        import http.cookiejar

        ctx = _ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPCookieProcessor(cj),
        )
        opener.addheaders = [("User-Agent", "poolheat-chipmap/1.0")]

        # login
        login_body = urllib.parse.urlencode(
            {"luci_username": user, "luci_password": password}
        ).encode("utf-8")
        login_req = urllib.request.Request(
            f"{base}/cgi-bin/luci",
            data=login_body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with opener.open(login_req, timeout=10) as resp:
            _ = resp.read(8192)

        # API log page
        page_req = urllib.request.Request(
            f"{base}/cgi-bin/luci/admin/status/btminerapi",
            method="GET",
        )
        with opener.open(page_req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # extract textarea#syslog
        m = re.search(
            r'<textarea[^>]*id=["\']syslog["\'][^>]*>(.*?)</textarea>',
            html,
            re.I | re.S,
        )
        if not m:
            # fallback: whole page text
            log_text = re.sub(r"<[^>]+>", "\n", html)
        else:
            log_text = m.group(1)
            # unescape basic HTML entities
            log_text = (
                log_text.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&")
                .replace("&quot;", '"')
            )

        parsed = parse_chipmap_log(log_text)
        if not parsed.get("chip_count"):
            # Re-check suspend: empty log while stopped is normal
            mining2, _ = _chipmap_is_mining()
            if mining2 is False:
                return _chipmap_suspend_payload(
                    fetch_ms=int((time.time() - t0) * 1000)
                )
            raise RuntimeError(
                "no chip lines parsed — check web password / page content "
                "(or miner not hashing yet)"
            )

        ms = int((time.time() - t0) * 1000)
        out = {
            "ok": True,
            "reason": None,
            "message": None,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "fetch_ms": ms,
            "error": None,
            "source": "luci:btminerapi",
            "host": base,
            **parsed,
        }
        # model string for physical layout (snake / chips_per_domain / slot_link)
        try:
            ident = get_miner_identity_cached(force=False)
            if isinstance(ident, dict):
                mt = (ident.get("miner_type") or ident.get("model") or "").strip()
                if mt:
                    out["miner_type"] = mt
        except Exception:
            pass
        out = _chipmap_attach_hash_estimates(out)
        with _chipmap_lock:
            _chipmap_cache.clear()
            _chipmap_cache.update(out)
        _save_chipmap_cache_disk(out)
        return dict(out)
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        # If we only failed LuCI while miner is suspended — not an access error
        mining3, _ = _chipmap_is_mining()
        if mining3 is False:
            return _chipmap_suspend_payload(fetch_ms=ms)
        with _chipmap_lock:
            prev = dict(_chipmap_cache)
            _chipmap_cache["ok"] = False
            _chipmap_cache["reason"] = "error"
            _chipmap_cache["message"] = None
            _chipmap_cache["error"] = str(e)
            _chipmap_cache["fetch_ms"] = ms
            _chipmap_cache["ts"] = datetime.now().isoformat(timespec="seconds")
            # keep last good boards if any
            out = dict(_chipmap_cache)
            if prev.get("boards") and prev.get("reason") not in ("suspend",):
                out["boards"] = prev["boards"]
                out["chip_count"] = prev.get("chip_count")
                out["board_count"] = prev.get("board_count")
                out["temp_min"] = prev.get("temp_min")
                out["temp_max"] = prev.get("temp_max")
                out["temp_avg"] = prev.get("temp_avg")
                out["miner_type"] = prev.get("miner_type")
                out["stale"] = True
                out["last_good_ts"] = prev.get("ts")
                _chipmap_cache.update(out)
        return out


def get_chipmap(*, force: bool = False) -> dict:
    """Return cached chipmap; optionally force refresh."""
    if force:
        return fetch_chipmap_from_luci(force=True)
    with _chipmap_lock:
        if _chipmap_cache.get("boards"):
            out = dict(_chipmap_cache)
        else:
            out = None
    if out is None:
        return fetch_chipmap_from_luci(force=True)
    # refresh est_th / nonce_rate from current HR + mining elapsed
    if out.get("reason") != "suspend":
        out = _chipmap_attach_hash_estimates(out)
    # keep miner_type fresh for layout lookup even on cache hit
    if not out.get("miner_type"):
        try:
            ident = get_miner_identity_cached(force=False)
            if isinstance(ident, dict):
                mt = (ident.get("miner_type") or ident.get("model") or "").strip()
                if mt:
                    out["miner_type"] = mt
        except Exception:
            pass
    return out


def chipmap_loop() -> None:
    """Background poll of LuCI chip log — own interval, independent of live poll."""
    _chipmap_stop.wait(timeout=4.0)
    while not _chipmap_stop.is_set():
        with _chipmap_lock:
            enabled = bool(_chipmap_cfg.get("enabled", True))
            interval = max(10, int(_chipmap_cfg.get("poll_interval_sec") or 30))
        if enabled:
            try:
                fetch_chipmap_from_luci(force=True)
            except Exception as e:
                print(f"[chipmap] poll: {e}")
        _chipmap_stop.wait(timeout=interval)


# ── LuCI reverse proxy (:8788 → miner web UI) ────────────────────────────────

def _import_luci_proxy():
    """Load luci_proxy module from same dir as serve.py (Entware or local)."""
    try:
        import luci_proxy  # type: ignore

        return luci_proxy
    except ImportError:
        import sys as _sys

        _lib = Path(__file__).resolve().parent
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        import luci_proxy  # type: ignore

        return luci_proxy


try:
    luci_proxy = _import_luci_proxy()
except Exception as _lp_err:
    luci_proxy = None  # type: ignore
    print(f"[luci-proxy] module unavailable: {_lp_err}")

DEFAULT_LUCI_PROXY_CFG: dict = {
    "enabled": False,
    "bind": "0.0.0.0",
    "listen_port": 8788,
    "target_scheme": "https",
    "target_port": 443,
    "verify_tls": False,
}

_luci_proxy_cfg: dict = dict(DEFAULT_LUCI_PROXY_CFG)
_luci_proxy_cfg_lock = threading.Lock()


def _load_luci_proxy_cfg() -> None:
    global _luci_proxy_cfg
    raw = _load_json(LUCI_PROXY_CFG_FILE, DEFAULT_LUCI_PROXY_CFG)
    if not isinstance(raw, dict):
        raw = {}
    cfg = dict(DEFAULT_LUCI_PROXY_CFG)
    cfg["enabled"] = bool(raw.get("enabled", False))
    bind = str(raw.get("bind") or HTTP_BIND or "0.0.0.0").strip() or "0.0.0.0"
    cfg["bind"] = bind
    try:
        lp = int(raw.get("listen_port", 8788) or 8788)
    except (TypeError, ValueError):
        lp = 8788
    cfg["listen_port"] = max(1, min(65535, lp))
    sch = str(raw.get("target_scheme") or "https").strip().lower()
    cfg["target_scheme"] = "http" if sch == "http" else "https"
    try:
        default_port = 80 if cfg["target_scheme"] == "http" else 443
        tp = int(raw.get("target_port", default_port) or default_port)
    except (TypeError, ValueError):
        tp = 80 if cfg["target_scheme"] == "http" else 443
    cfg["target_port"] = max(1, min(65535, tp))
    cfg["verify_tls"] = bool(raw.get("verify_tls", False))
    with _luci_proxy_cfg_lock:
        _luci_proxy_cfg = cfg


def _save_luci_proxy_cfg() -> None:
    with _luci_proxy_cfg_lock:
        _save_json(LUCI_PROXY_CFG_FILE, _luci_proxy_cfg)


def get_luci_proxy_cfg() -> dict:
    with _luci_proxy_cfg_lock:
        cfg = dict(_luci_proxy_cfg)
    st = {}
    if luci_proxy is not None:
        try:
            st = luci_proxy.status()
        except Exception as e:
            st = {"error": str(e), "running": False}
    cfg["target_host"] = HOST_MINER
    return {
        "ok": True,
        "config": cfg,
        "status": st,
        "url_hint": f"http://<router-ip>:{cfg.get('listen_port') or 8788}/",
    }


def apply_luci_proxy_cfg(req: dict) -> dict:
    """Update config, persist, start/stop proxy module."""
    if not isinstance(req, dict):
        raise ValueError("expected object")
    with _luci_proxy_cfg_lock:
        if "enabled" in req:
            _luci_proxy_cfg["enabled"] = bool(req["enabled"])
        if "bind" in req and req["bind"] is not None and str(req["bind"]).strip():
            _luci_proxy_cfg["bind"] = str(req["bind"]).strip()
        if "listen_port" in req and req["listen_port"] is not None:
            try:
                _luci_proxy_cfg["listen_port"] = max(
                    1, min(65535, int(req["listen_port"]))
                )
            except (TypeError, ValueError):
                pass
        if "target_scheme" in req and req["target_scheme"] is not None:
            sch = str(req["target_scheme"]).strip().lower()
            _luci_proxy_cfg["target_scheme"] = "http" if sch == "http" else "https"
        if "target_port" in req and req["target_port"] is not None:
            try:
                _luci_proxy_cfg["target_port"] = max(
                    1, min(65535, int(req["target_port"]))
                )
            except (TypeError, ValueError):
                pass
        if "verify_tls" in req:
            _luci_proxy_cfg["verify_tls"] = bool(req["verify_tls"])
        snap = dict(_luci_proxy_cfg)
    _save_luci_proxy_cfg()
    _luci_proxy_sync_runtime(snap)
    return get_luci_proxy_cfg()


def _luci_proxy_sync_runtime(cfg: dict | None = None) -> None:
    """Push settings into luci_proxy module and start/stop."""
    if luci_proxy is None:
        return
    if cfg is None:
        with _luci_proxy_cfg_lock:
            cfg = dict(_luci_proxy_cfg)
    try:
        luci_proxy.set_host_resolver(lambda: HOST_MINER)
        luci_proxy.configure(
            enabled=bool(cfg.get("enabled")),
            bind=str(cfg.get("bind") or HTTP_BIND or "0.0.0.0"),
            listen_port=int(cfg.get("listen_port") or 8788),
            target_scheme=str(cfg.get("target_scheme") or "https"),
            target_port=int(
                cfg.get("target_port")
                or (80 if cfg.get("target_scheme") == "http" else 443)
            ),
            verify_tls=bool(cfg.get("verify_tls", False)),
            target_host=str(HOST_MINER or ""),
        )
        luci_proxy.apply()
    except Exception as e:
        print(f"[luci-proxy] apply: {e}")


def _miner_config_path() -> Path:
    """Where to persist miner host/port/password."""
    cfg = _APP.get("cfg_path") or ""
    if cfg:
        return Path(cfg)
    # Entware default
    if Path("/opt/etc/poolheat").is_dir():
        return Path("/opt/etc/poolheat/config.json")
    return DATA / "config.json"


def get_project_name() -> str:
    """Runtime name; re-sync from config.json if file has a value."""
    global PROJECT_NAME
    with _miner_cfg_lock:
        n = str(PROJECT_NAME or "").strip()
        path = _miner_config_path()
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("project_name") is not None:
                    fn = str(raw.get("project_name") or "").strip()
                    if fn:
                        PROJECT_NAME = fn
                        n = fn
            except Exception:
                pass
        return n or "poolheat_WM"


def get_miner_settings() -> dict:
    # ensure project_name is fresh from disk
    pname = get_project_name()
    with _miner_cfg_lock:
        return {
            "miner_host": HOST_MINER,
            "miner_port": int(PORT_MINER),
            "api_password": DEFAULT_API_PASSWORD,
            "poll_interval_sec": int(POLL_INTERVAL_SEC),
            "dry_run": bool(DRY_RUN),
            "project_name": pname,
            "host": f"{HOST_MINER}:{PORT_MINER}",
            "config_path": str(_miner_config_path()),
        }


def apply_miner_settings(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    poll_interval_sec: int | None = None,
    dry_run: bool | None = None,
    project_name: str | None = None,
    *,
    persist: bool = True,
) -> dict:
    """Update runtime miner target; optionally write config.json."""
    global HOST_MINER, PORT_MINER, DEFAULT_API_PASSWORD, POLL_INTERVAL_SEC, DRY_RUN
    global PROJECT_NAME, _cache, _cache_ts

    with _miner_cfg_lock:
        if host is not None:
            h = str(host).strip()
            if not h:
                raise ValueError("miner_host empty")
            # basic sanity: no spaces
            if any(c.isspace() for c in h):
                raise ValueError("miner_host invalid")
            HOST_MINER = h
        if port is not None:
            p = int(port)
            if p < 1 or p > 65535:
                raise ValueError("miner_port out of range")
            PORT_MINER = p
        if password is not None and str(password) != "":
            DEFAULT_API_PASSWORD = str(password)
        if poll_interval_sec is not None:
            pi = int(poll_interval_sec)
            if pi < 2 or pi > 300:
                raise ValueError("poll_interval_sec must be 2–300")
            POLL_INTERVAL_SEC = pi
        if dry_run is not None:
            DRY_RUN = bool(dry_run)
        if project_name is not None:
            pn = str(project_name).strip() or "poolheat_WM"
            if len(pn) > 64:
                pn = pn[:64]
            PROJECT_NAME = pn

        settings = {
            "miner_host": HOST_MINER,
            "miner_port": int(PORT_MINER),
            "api_password": DEFAULT_API_PASSWORD,
            "poll_interval_sec": int(POLL_INTERVAL_SEC),
            "dry_run": bool(DRY_RUN),
            "project_name": str(PROJECT_NAME or "poolheat_WM"),
            "host": f"{HOST_MINER}:{PORT_MINER}",
        }
        # keep LuCI proxy target in sync with miner host
        if host is not None and luci_proxy is not None:
            try:
                luci_proxy.configure(target_host=HOST_MINER)
            except Exception:
                pass

        if persist:
            path = _miner_config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if path.is_file():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            existing["miner_host"] = HOST_MINER
            existing["miner_port"] = int(PORT_MINER)
            existing["api_password"] = DEFAULT_API_PASSWORD
            existing["poll_interval_sec"] = int(POLL_INTERVAL_SEC)
            existing["dry_run"] = bool(DRY_RUN)
            # project_name: only overwrite when explicitly provided in this call;
            # otherwise keep file value (don't clobber via dry_run-only saves)
            if project_name is not None:
                existing["project_name"] = str(PROJECT_NAME or "poolheat_WM")
            elif existing.get("project_name"):
                PROJECT_NAME = str(existing["project_name"]).strip() or PROJECT_NAME
                existing["project_name"] = str(PROJECT_NAME or "poolheat_WM")
            else:
                existing["project_name"] = str(PROJECT_NAME or "poolheat_WM")
            settings["project_name"] = str(existing.get("project_name") or "poolheat_WM")
            # keep other keys (bind, http_port, comment…)
            if "bind" not in existing:
                existing["bind"] = HTTP_BIND
            if "http_port" not in existing:
                existing["http_port"] = HTTP_PORT
            path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            settings["config_path"] = str(path)
            settings["saved"] = True
            settings["saved"] = True
        else:
            settings["saved"] = False

    # drop live cache so next poll hits new host
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0
    return settings


# ── Config backup / restore (settings JSON bundle) ───────────────────────────
CONFIG_BACKUP_FORMAT = "poolheat_config_backup"
CONFIG_BACKUP_VERSION = 1
# Keys in config.json that are host/runtime facts — rewritten on every boot
_CONFIG_META_KEY = "meta"


def _read_json_file(path: Path) -> dict | None:
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def build_runtime_meta() -> dict:
    """
    Host + software facts for config.json meta (and backups).
    Safe to call often; does not touch miner network unless identity already cached.
    """
    router_brief: dict = {}
    try:
        r = _collect_router_info()
        router_brief = {
            "vendor": r.get("vendor"),
            "model": r.get("model"),
            "model_code": r.get("model_code"),
            "hostname": r.get("hostname"),
            "os_title": r.get("os_title"),
            "os_release": r.get("os_release"),
            "arch": r.get("arch"),
            "hw_version": r.get("hw_version"),
            "source": r.get("source"),
        }
    except Exception as e:
        router_brief = {"error": str(e)}

    miner_type = None
    miner_sn = None
    try:
        # disk / RAM identity cache only — no live fetch
        mem = None
        try:
            mem = _IDENT_CACHE.get("data") if isinstance(_IDENT_CACHE, dict) else None
        except NameError:
            mem = None
        disk = None
        try:
            disk = _load_miner_id_disk()
        except Exception:
            disk = None
        src = mem if isinstance(mem, dict) and (mem.get("miner_type") or mem.get("minersn")) else disk
        if isinstance(src, dict):
            miner_type = src.get("miner_type") or src.get("model")
            miner_sn = src.get("minersn") or src.get("miner_sn")
    except Exception:
        pass

    try:
        uname = os.uname()
        host_os = {
            "sysname": uname.sysname,
            "release": uname.release,
            "machine": uname.machine,
            "nodename": uname.nodename,
        }
    except Exception:
        host_os = {}

    return {
        "app_version": get_app_version(),
        "github_repo": GITHUB_REPO,
        "github_branch": GITHUB_BRANCH,
        "project_name": get_project_name(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "router": router_brief,
        "miner": {
            "host": HOST_MINER,
            "port": int(PORT_MINER),
            "type": miner_type,
            "sn": miner_sn,
        },
        "paths": {
            "config": str(_miner_config_path()),
            "data": str(DATA),
            "www": str(ROOT),
            "db": str(DB_FILE),
        },
        "bind": f"{HTTP_BIND}:{HTTP_PORT}",
        "dry_run": bool(DRY_RUN),
        "host_os": host_os,
    }


def refresh_config_meta(*, include_router: bool = True) -> dict:
    """
    Overwrite config.json → meta with current host/software facts.
    Called on service start and before backup download. User settings untouched.
    """
    meta = build_runtime_meta() if include_router else {
        "app_version": get_app_version(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": get_project_name(),
        "miner": {"host": HOST_MINER, "port": int(PORT_MINER)},
        "paths": {
            "config": str(_miner_config_path()),
            "data": str(DATA),
            "www": str(ROOT),
        },
        "bind": f"{HTTP_BIND}:{HTTP_PORT}",
        "dry_run": bool(DRY_RUN),
    }
    path = _miner_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_json_file(path) or {}
        existing[_CONFIG_META_KEY] = meta
        # also mirror version at top-level for quick glance
        existing["app_version"] = meta.get("app_version")
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[config] meta refresh: {e}")
        meta["_write_error"] = str(e)
    return meta


def build_config_backup() -> dict:
    """
    Full settings snapshot for download.
    Includes secrets (API password, TG token, filtration passwords) so restore works.
    Does not include history.db / runtime caches / policy events.
    Refreshes host meta into config.json first.
    """
    meta = refresh_config_meta(include_router=True)

    app_path = _miner_config_path()
    app_file = _read_json_file(app_path) or {}
    # ensure runtime miner keys present even if file is sparse
    ms = get_miner_settings()
    app_merged = dict(app_file)
    for k in (
        "miner_host",
        "miner_port",
        "api_password",
        "poll_interval_sec",
        "dry_run",
        "project_name",
        "bind",
        "http_port",
    ):
        if k not in app_merged and k in ms:
            app_merged[k] = ms[k]
        if k in ("miner_host", "miner_port", "api_password", "poll_interval_sec", "dry_run", "project_name"):
            app_merged[k] = ms.get(k, app_merged.get(k))
    app_merged[_CONFIG_META_KEY] = meta
    app_merged["app_version"] = meta.get("app_version")

    with _hist_cfg_lock:
        history = dict(_hist_cfg)
    with _weather_cfg_lock:
        weather = dict(_weather_cfg)
    with _pool_cfg_lock:
        pool = dict(_pool_cfg)

    zone = get_zone_cfg()
    with _zone_presets_lock:
        zone_presets = json.loads(json.dumps(_zone_presets))

    filtration = get_filtration_cfg(redact=False)
    filtration.pop("backends", None)
    chipmap = get_chipmap_cfg(redact=False)
    with _luci_proxy_cfg_lock:
        luci_proxy_cfg = dict(_luci_proxy_cfg)
    telegram = get_telegram_cfg(redact=False)
    telegram.pop("status", None)
    with _pool_presets_lock:
        pool_presets = json.loads(json.dumps(_pool_presets))

    return {
        "format": CONFIG_BACKUP_FORMAT,
        "version": CONFIG_BACKUP_VERSION,
        "app_version": meta.get("app_version") or get_app_version(),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": get_project_name(),
        "meta": meta,
        "configs": {
            "app": app_merged,
            "history": history,
            "weather": weather,
            "pool": pool,
            "zone_map": zone,
            "zone_presets": zone_presets,
            "pool_presets": pool_presets,
            "filtration": filtration,
            "chipmap": chipmap,
            "luci_proxy": luci_proxy_cfg,
            "telegram": telegram,
        },
        "notes": (
            "Restore via UI Settings → Backup or POST /api/config/restore. "
            "Contains secrets (API password, Telegram token, filtration passwords). "
            "meta / app_version are host facts — rewritten on each service start."
        ),
    }


def restore_config_backup(payload: dict, *, sections: list[str] | None = None) -> dict:
    """
    Apply backup bundle. `sections` optional filter of configs keys;
    default = all present sections.
    """
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    # accept raw {configs:{…}} or full backup wrapper or single legacy flat
    if payload.get("format") == CONFIG_BACKUP_FORMAT or "configs" in payload:
        cfgs = payload.get("configs")
        if not isinstance(cfgs, dict):
            raise ValueError("missing configs object")
    else:
        # allow paste of single config.json as app only
        if any(k in payload for k in ("miner_host", "api_password", "project_name")):
            cfgs = {"app": payload}
        else:
            raise ValueError(
                "unknown backup format (need format=poolheat_config_backup or configs{})"
            )

    want = set(sections) if sections else set(cfgs.keys())
    applied: list[str] = []
    errors: dict[str, str] = {}

    # 1) app / miner config
    if "app" in want and isinstance(cfgs.get("app"), dict):
        try:
            app = dict(cfgs["app"])
            # never restore stale host meta from another machine
            app.pop(_CONFIG_META_KEY, None)
            app.pop("app_version", None)
            apply_miner_settings(
                host=app.get("miner_host"),
                port=app.get("miner_port"),
                password=app.get("api_password"),
                poll_interval_sec=app.get("poll_interval_sec"),
                dry_run=app.get("dry_run") if "dry_run" in app else None,
                project_name=app.get("project_name"),
                persist=True,
            )
            # preserve extra keys (bind, http_port, force_stop, …)
            path = _miner_config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = _read_json_file(path) or {}
            for k, v in app.items():
                if k in (
                    "miner_host",
                    "miner_port",
                    "api_password",
                    "poll_interval_sec",
                    "dry_run",
                    "project_name",
                    _CONFIG_META_KEY,
                    "app_version",
                ):
                    continue  # already applied / host facts
                existing[k] = v
            # re-apply runtime keys on top
            ms = get_miner_settings()
            for k in (
                "miner_host",
                "miner_port",
                "api_password",
                "poll_interval_sec",
                "dry_run",
                "project_name",
            ):
                existing[k] = ms.get(k)
            path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            applied.append("app")
        except Exception as e:
            errors["app"] = str(e)

    # 2) history
    if "history" in want and isinstance(cfgs.get("history"), dict):
        try:
            req = cfgs["history"]
            with _hist_cfg_lock:
                if "enabled" in req:
                    _hist_cfg["enabled"] = bool(req["enabled"])
                if "retention_days" in req:
                    _hist_cfg["retention_days"] = max(
                        1, min(90, int(req["retention_days"]))
                    )
                if "sample_interval_sec" in req:
                    _hist_cfg["sample_interval_sec"] = max(
                        5, min(3600, int(req["sample_interval_sec"]))
                    )
            _save_hist_cfg()
            applied.append("history")
        except Exception as e:
            errors["history"] = str(e)

    # 3) weather
    if "weather" in want and isinstance(cfgs.get("weather"), dict):
        try:
            req = cfgs["weather"]
            with _weather_cfg_lock:
                for k, v in req.items():
                    if k in (
                        "enabled",
                        "city",
                        "country",
                        "admin1",
                        "timezone",
                        "latitude",
                        "longitude",
                        "refresh_interval_sec",
                    ):
                        _weather_cfg[k] = v
                if "enabled" in req:
                    _weather_cfg["enabled"] = bool(req["enabled"])
                if "latitude" in req and req["latitude"] is not None:
                    _weather_cfg["latitude"] = float(req["latitude"])
                if "longitude" in req and req["longitude"] is not None:
                    _weather_cfg["longitude"] = float(req["longitude"])
            _save_weather_cfg()
            applied.append("weather")
        except Exception as e:
            errors["weather"] = str(e)

    # 4) pool
    if "pool" in want and isinstance(cfgs.get("pool"), dict):
        try:
            req = cfgs["pool"]
            with _pool_cfg_lock:
                for key in (
                    "length_m",
                    "width_m",
                    "depth_m",
                    "flow_m3h",
                    "hex_delta_c",
                    "shape",
                    "comment",
                    "water_sensor",
                ):
                    if key in req and req[key] is not None:
                        if key == "water_sensor":
                            _pool_cfg[key] = _normalize_pool_water_sensor(req[key])
                        elif key in ("shape", "comment"):
                            _pool_cfg[key] = str(req[key])
                        else:
                            _pool_cfg[key] = float(req[key])
            _save_pool_cfg()
            applied.append("pool")
        except Exception as e:
            errors["pool"] = str(e)

    # 5) zone map
    if "zone_map" in want and isinstance(cfgs.get("zone_map"), dict):
        try:
            cfg = _coerce_zone_config_dict(cfgs["zone_map"])
            with _zone_cfg_lock:
                _zone_cfg.clear()
                _zone_cfg.update(cfg)
            _save_zone_cfg()
            applied.append("zone_map")
        except Exception as e:
            errors["zone_map"] = str(e)

    # 6) zone presets
    if "zone_presets" in want and isinstance(cfgs.get("zone_presets"), dict):
        try:
            global _zone_presets
            raw = cfgs["zone_presets"]
            with _zone_presets_lock:
                _zone_presets = {
                    "presets": list(raw.get("presets") or []),
                    "active_id": raw.get("active_id"),
                }
            _save_zone_presets()
            # re-normalize via loader
            _load_zone_presets()
            applied.append("zone_presets")
        except Exception as e:
            errors["zone_presets"] = str(e)

    # 6b) mining pool presets
    if "pool_presets" in want and isinstance(cfgs.get("pool_presets"), dict):
        try:
            global _pool_presets
            raw = cfgs["pool_presets"]
            with _pool_presets_lock:
                _pool_presets = {
                    "presets": list(raw.get("presets") or []),
                    "active_id": raw.get("active_id"),
                }
            _save_pool_presets()
            _load_pool_presets()
            applied.append("pool_presets")
        except Exception as e:
            errors["pool_presets"] = str(e)

    # 7) filtration
    if "filtration" in want and isinstance(cfgs.get("filtration"), dict):
        try:
            apply_filtration_cfg(cfgs["filtration"])
            applied.append("filtration")
        except Exception as e:
            errors["filtration"] = str(e)

    # 8) chipmap
    if "chipmap" in want and isinstance(cfgs.get("chipmap"), dict):
        try:
            apply_chipmap_cfg(cfgs["chipmap"])
            applied.append("chipmap")
        except Exception as e:
            errors["chipmap"] = str(e)

    # 8b) luci proxy
    if "luci_proxy" in want and isinstance(cfgs.get("luci_proxy"), dict):
        try:
            apply_luci_proxy_cfg(cfgs["luci_proxy"])
            applied.append("luci_proxy")
        except Exception as e:
            errors["luci_proxy"] = str(e)

    # 9) telegram
    if "telegram" in want and isinstance(cfgs.get("telegram"), dict):
        try:
            req = cfgs["telegram"]
            with _tg_cfg_lock:
                for k, v in req.items():
                    if k in ("status", "bot_token_set"):
                        continue
                    if k == "bot_token" and isinstance(v, str) and (
                        "…" in v or v.startswith("••••")
                    ):
                        continue  # skip redacted
                    _tg_cfg[k] = v
                if "enabled" in req:
                    _tg_cfg["enabled"] = bool(req["enabled"])
            _save_telegram_cfg(force=True)
            applied.append("telegram")
        except Exception as e:
            errors["telegram"] = str(e)

    if not applied and errors:
        raise RuntimeError("restore failed: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    if not applied:
        raise ValueError("nothing to restore (empty sections)")

    # always rewrite host meta after restore (this machine, current version)
    try:
        meta = refresh_config_meta(include_router=True)
    except Exception as e:
        meta = {"error": str(e)}

    return {
        "ok": True,
        "applied": applied,
        "errors": errors or None,
        "app_version": get_app_version(),
        "meta": meta,
    }


# Rectangular pool geometry + circulation through heat exchanger
DEFAULT_POOL_CFG = {
    "length_m": 8.0,
    "width_m": 4.0,
    "depth_m": 1.5,
    # circulation flow through heat exchanger, m³/h
    "flow_m3h": 12.0,
    # ΔT heat exchanger (water in − water out), °C
    "hex_delta_c": 5.0,
    # Which live field is pool water temperature for heat balance / Tw
    # (same ids as zone T_ctrl sensors)
    "water_sensor": "liquid",
    # optional notes
    "shape": "rect",
    "comment": "",
}

# Sensors selectable as «вода в бассейне» (pool water °C)
POOL_WATER_SENSORS: tuple[str, ...] = (
    "liquid",
    "env",
    "chip_avg",
    "chip_max",
    "board_max",
)

DEFAULT_HISTORY_CFG = {
    "enabled": True,
    "retention_days": 7,
    "sample_interval_sec": 30,
    "prune_every_samples": 20,
}

# Open-Meteo — no API key. City presets for quick pick (RU + common).
DEFAULT_WEATHER_CFG = {
    "enabled": True,
    "city": "Москва",
    "country": "RU",
    "admin1": "",
    "latitude": 55.7558,
    "longitude": 37.6173,
    "timezone": "Europe/Moscow",
    # how often to re-fetch Open-Meteo (server cache TTL + UI poll)
    "refresh_interval_sec": 600,
}

WEATHER_PRESETS = [
    {"city": "Москва", "country": "RU", "admin1": "", "latitude": 55.7558, "longitude": 37.6173, "timezone": "Europe/Moscow"},
    {"city": "Санкт-Петербург", "country": "RU", "admin1": "", "latitude": 59.9343, "longitude": 30.3351, "timezone": "Europe/Moscow"},
    {"city": "Екатеринбург", "country": "RU", "admin1": "", "latitude": 56.8389, "longitude": 60.6057, "timezone": "Asia/Yekaterinburg"},
    {"city": "Новосибирск", "country": "RU", "admin1": "", "latitude": 55.0084, "longitude": 82.9357, "timezone": "Asia/Novosibirsk"},
    {"city": "Томск", "country": "RU", "admin1": "", "latitude": 56.4977, "longitude": 84.9744, "timezone": "Asia/Tomsk"},
    {"city": "Красноярск", "country": "RU", "admin1": "", "latitude": 56.0153, "longitude": 92.8932, "timezone": "Asia/Krasnoyarsk"},
    {"city": "Иркутск", "country": "RU", "admin1": "", "latitude": 52.2869, "longitude": 104.3050, "timezone": "Asia/Irkutsk"},
    {"city": "Хабаровск", "country": "RU", "admin1": "", "latitude": 48.4827, "longitude": 135.0838, "timezone": "Asia/Vladivostok"},
]

_weather_cfg_lock = threading.Lock()
_weather_cfg: dict = dict(DEFAULT_WEATHER_CFG)
_weather_cache_lock = threading.Lock()
_weather_cache: dict | None = None
_weather_cache_ts = 0.0
WEATHER_CACHE_TTL_DEFAULT = 600.0  # 10 min

_pool_cfg_lock = threading.Lock()
_pool_cfg: dict = dict(DEFAULT_POOL_CFG)

_cache: dict | None = None
_cache_ts = 0.0
_cache_lock = threading.Lock()
CACHE_TTL = 2.0
# Serialize TCP 4028 access so live poll / collector / privileged writes
# do not race and exhaust Whatsminer "over max connect" sessions.
_miner_io_lock = threading.RLock()

_state_lock = threading.Lock()
_state: dict = {
    "power_pct_cmd": None,
    "power_limit_cmd": None,
    "mode_cmd": None,
    "work_cmd": None,  # Mining Control: sleep (suspend) | resume
    "last_write": None,
    "last_write_result": None,
    # Frozen Cause text per active firmware error (code@ts → {code,ts,cause})
    # so hashrate in Cause does not change on every poll
    "miner_error_cache": {},
}

_hist_cfg_lock = threading.Lock()
_hist_cfg: dict = dict(DEFAULT_HISTORY_CFG)
_collector_stop = threading.Event()
_db_lock = threading.Lock()


def _load_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**default, **data}
        except Exception:
            pass
    return dict(default)


def _save_json(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_state() -> None:
    global _state
    _state.update(_load_json(STATE_FILE, _state))


def _save_state() -> None:
    _save_json(STATE_FILE, _state)


def _load_hist_cfg() -> None:
    global _hist_cfg
    with _hist_cfg_lock:
        _hist_cfg = _load_json(CONFIG_FILE, DEFAULT_HISTORY_CFG)
        # clamp
        _hist_cfg["retention_days"] = max(1, min(90, int(_hist_cfg["retention_days"])))
        _hist_cfg["sample_interval_sec"] = max(5, min(3600, int(_hist_cfg["sample_interval_sec"])))
        _hist_cfg["enabled"] = bool(_hist_cfg["enabled"])


def _save_hist_cfg() -> None:
    with _hist_cfg_lock:
        _save_json(CONFIG_FILE, _hist_cfg)


def _weather_refresh_sec(cfg: dict | None = None) -> int:
    """Clamp weather refresh interval (seconds)."""
    c = cfg if isinstance(cfg, dict) else _weather_cfg
    try:
        sec = int(c.get("refresh_interval_sec") or WEATHER_CACHE_TTL_DEFAULT)
    except (TypeError, ValueError):
        sec = int(WEATHER_CACHE_TTL_DEFAULT)
    return max(60, min(86400, sec))


def _load_weather_cfg() -> None:
    global _weather_cfg
    with _weather_cfg_lock:
        _weather_cfg = _load_json(WEATHER_CFG_FILE, DEFAULT_WEATHER_CFG)
        _weather_cfg["enabled"] = bool(_weather_cfg.get("enabled", True))
        try:
            _weather_cfg["latitude"] = float(_weather_cfg.get("latitude", DEFAULT_WEATHER_CFG["latitude"]))
            _weather_cfg["longitude"] = float(_weather_cfg.get("longitude", DEFAULT_WEATHER_CFG["longitude"]))
        except (TypeError, ValueError):
            _weather_cfg["latitude"] = DEFAULT_WEATHER_CFG["latitude"]
            _weather_cfg["longitude"] = DEFAULT_WEATHER_CFG["longitude"]
        if not _weather_cfg.get("city"):
            _weather_cfg["city"] = DEFAULT_WEATHER_CFG["city"]
        _weather_cfg["refresh_interval_sec"] = _weather_refresh_sec(_weather_cfg)


def _save_weather_cfg() -> None:
    with _weather_cfg_lock:
        _save_json(WEATHER_CFG_FILE, _weather_cfg)


def _load_pool_cfg() -> None:
    global _pool_cfg
    with _pool_cfg_lock:
        _pool_cfg = _load_json(POOL_CFG_FILE, DEFAULT_POOL_CFG)
        for key, default in (
            ("length_m", 8.0),
            ("width_m", 4.0),
            ("depth_m", 1.5),
            ("flow_m3h", 12.0),
            ("hex_delta_c", 5.0),
        ):
            try:
                v = float(_pool_cfg.get(key, default))
            except (TypeError, ValueError):
                v = default
            # sane clamps
            if key == "flow_m3h":
                v = max(0.0, min(500.0, v))
            elif key == "hex_delta_c":
                v = max(0.1, min(40.0, v))
            else:
                v = max(0.1, min(200.0, v))
            _pool_cfg[key] = v
        shape = str(_pool_cfg.get("shape") or "rect").lower()
        if shape in ("round", "circular", "circle"):
            shape = "circle"
        elif shape in ("ellipse", "elliptic", "oval"):
            shape = "oval"
        else:
            shape = "rect"
        _pool_cfg["shape"] = shape
        _pool_cfg["comment"] = str(_pool_cfg.get("comment") or "")
        _pool_cfg["water_sensor"] = _normalize_pool_water_sensor(
            _pool_cfg.get("water_sensor", "liquid")
        )


def _normalize_pool_water_sensor(v) -> str:
    """Pool water °C source — subset of T_ctrl sensor ids."""
    s = _normalize_t_ctrl_sensor(v)
    if s not in POOL_WATER_SENSORS:
        return "liquid"
    return s


def resolve_pool_water(
    live: dict | None, sensor: str | None = None
) -> tuple[float | None, str]:
    """Pool water temperature from live snapshot + pool_config.water_sensor."""
    if sensor is None:
        with _pool_cfg_lock:
            sensor = _pool_cfg.get("water_sensor") or "liquid"
    return resolve_t_ctrl(live, _normalize_pool_water_sensor(sensor))


def _save_pool_cfg() -> None:
    with _pool_cfg_lock:
        _save_json(POOL_CFG_FILE, _pool_cfg)


def pool_derived(cfg: dict | None = None) -> dict:
    """
    Volume (m³) and surface area (m²).
    shapes:
      rect  — L×W×D prism, S = L×W
      circle — diameter = L, S = π(L/2)², V = S×D
      oval  — ellipse axes L×W, S = π(L/2)(W/2), V = S×D
    """
    with _pool_cfg_lock:
        c = dict(cfg or _pool_cfg)
    L = float(c.get("length_m") or 0)
    W = float(c.get("width_m") or 0)
    D = float(c.get("depth_m") or 0)
    flow = float(c.get("flow_m3h") or 0)
    try:
        hex_dT = float(c.get("hex_delta_c") or 0)
    except (TypeError, ValueError):
        hex_dT = 0.0
    shape = str(c.get("shape") or "rect").lower()
    if shape in ("round", "circular", "circle"):
        shape = "circle"
    elif shape in ("ellipse", "elliptic", "oval"):
        shape = "oval"
    else:
        shape = "rect"

    if shape == "circle":
        r = L / 2.0
        surface_m2 = math.pi * r * r
        volume_m3 = surface_m2 * D
        formula_v = "V = π·(Ø/2)²·D"
        formula_s = "S = π·(Ø/2)²"
    elif shape == "oval":
        surface_m2 = math.pi * (L / 2.0) * (W / 2.0)
        volume_m3 = surface_m2 * D
        formula_v = "V = π·(L/2)·(W/2)·D"
        formula_s = "S = π·(L/2)·(W/2)"
    else:
        volume_m3 = L * W * D
        surface_m2 = L * W
        formula_v = "V = L × W × D"
        formula_s = "S = L × W"

    turnover_h = (volume_m3 / flow) if flow > 0 else None
    mass_kg = volume_m3 * 1000.0
    # HEX heat capacity: ṁ · c · ΔT
    # ṁ [kg/s] = flow_m3h · 1000 / 3600; c = 4186 J/(kg·K)
    hex_power_kw = None
    if flow > 0 and hex_dT > 0:
        m_dot = flow * 1000.0 / 3600.0
        hex_power_kw = round(m_dot * 4186.0 * hex_dT / 1000.0, 3)
    return {
        "length_m": L,
        "width_m": W,
        "depth_m": D,
        "flow_m3h": flow,
        "hex_delta_c": hex_dT,
        "hex_power_kw": hex_power_kw,
        "volume_m3": round(volume_m3, 3),
        "surface_m2": round(surface_m2, 3),
        "turnover_h": round(turnover_h, 3) if turnover_h is not None else None,
        "mass_kg": round(mass_kg, 1),
        "shape": shape,
        "formula_v": formula_v,
        "formula_s": formula_s,
        "comment": c.get("comment") or "",
        "water_sensor": _normalize_pool_water_sensor(c.get("water_sensor")),
        "water_sensors": list(POOL_WATER_SENSORS),
    }


def _http_get_json(url: str, timeout: float = 12.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "poolheat/0.1 (weather; Open-Meteo)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("unexpected weather response")
    return data


# WMO weather codes → short RU labels (Open-Meteo)
_WMO_RU = {
    0: "ясно",
    1: "преим. ясно",
    2: "перем. облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "морось",
    53: "морось",
    55: "морось",
    61: "дождь",
    63: "дождь",
    65: "ливень",
    71: "снег",
    73: "снег",
    75: "снегопад",
    80: "ливень",
    81: "ливень",
    82: "сильный ливень",
    95: "гроза",
    96: "гроза с градом",
    99: "гроза с градом",
}


def weather_search_cities(query: str, count: int = 12) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    count = max(1, min(25, int(count)))
    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode(
            {"name": q, "count": count, "language": "ru", "format": "json"}
        )
    )
    data = _http_get_json(url)
    results = data.get("results") or []
    out: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "city": r.get("name") or q,
                "country": r.get("country_code") or r.get("country") or "",
                "admin1": r.get("admin1") or "",
                "latitude": float(r["latitude"]),
                "longitude": float(r["longitude"]),
                "timezone": r.get("timezone") or "auto",
                "label": ", ".join(
                    x
                    for x in [
                        r.get("name"),
                        r.get("admin1"),
                        r.get("country_code") or r.get("country"),
                    ]
                    if x
                ),
            }
        )
    return out


def fetch_weather_current(cfg: dict | None = None, *, force: bool = False) -> dict:
    """Current outdoor weather for configured city (Open-Meteo)."""
    global _weather_cache, _weather_cache_ts
    with _weather_cfg_lock:
        c = dict(cfg or _weather_cfg)

    if not c.get("enabled", True):
        return {"ok": True, "enabled": False, "city": c.get("city"), "temp_c": None}

    now = time.time()
    ttl = float(_weather_refresh_sec(c))
    with _weather_cache_lock:
        if (
            not force
            and _weather_cache is not None
            and (now - _weather_cache_ts) < ttl
            and _weather_cache.get("ok")
        ):
            out = dict(_weather_cache)
            out["refresh_interval_sec"] = int(ttl)
            return out

    lat = float(c["latitude"])
    lon = float(c["longitude"])
    tz = c.get("timezone") or "auto"
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "timezone": tz,
                "wind_speed_unit": "ms",
            }
        )
    )
    try:
        data = _http_get_json(url)
        cur = data.get("current") or {}
        code = cur.get("weather_code")
        try:
            code_i = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_i = None
        body = {
            "ok": True,
            "enabled": True,
            "city": c.get("city"),
            "country": c.get("country"),
            "admin1": c.get("admin1"),
            "latitude": lat,
            "longitude": lon,
            "timezone": data.get("timezone") or tz,
            "temp_c": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_ms": cur.get("wind_speed_10m"),
            "weather_code": code_i,
            "weather_text": _WMO_RU.get(code_i, "—") if code_i is not None else "—",
            "observed_at": cur.get("time"),
            "refresh_interval_sec": int(ttl),
            "source": "open-meteo",
            "cached": False,
        }
        with _weather_cache_lock:
            _weather_cache = dict(body)
            _weather_cache["cached"] = True
            _weather_cache_ts = now
        body["cached"] = False
        return body
    except Exception as e:
        with _weather_cache_lock:
            stale = dict(_weather_cache) if _weather_cache else None
        if stale and stale.get("ok"):
            stale = dict(stale)
            stale["stale"] = True
            stale["error"] = str(e)
            return stale
        return {
            "ok": False,
            "enabled": True,
            "city": c.get("city"),
            "error": str(e),
            "temp_c": None,
        }


_load_state()
_load_hist_cfg()
_load_weather_cfg()
_load_pool_cfg()
_load_zone_cfg()
_load_zone_presets()
_load_pool_presets()
_load_filtration_cfg()
_load_chipmap_cfg()
_load_chipmap_cache_disk()
_load_luci_proxy_cfg()
_luci_proxy_sync_runtime()


# ─── DB ───────────────────────────────────────────────────────────────────────


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    ts REAL NOT NULL PRIMARY KEY,
                    ts_iso TEXT NOT NULL,
                    liquid REAL,
                    env REAL,
                    chip_min REAL,
                    chip_avg REAL,
                    chip_max REAL,
                    board0 REAL,
                    board1 REAL,
                    board2 REAL,
                    board3 REAL,
                    power REAL,
                    power_limit REAL,
                    power_limit_set REAL,
                    power_pct_cmd REAL,
                    freq REAL,
                    hashrate_th REAL,
                    mode TEXT,
                    hash_stable INTEGER,
                    online INTEGER DEFAULT 1,
                    work_state TEXT,
                    upfreq_ok INTEGER
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
            # migrate older DBs
            cols = {r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
            if "hashrate_th" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN hashrate_th REAL")
            if "online" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN online INTEGER DEFAULT 1")
            if "work_state" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN work_state TEXT")
            if "upfreq_ok" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN upfreq_ok INTEGER")
            if "outdoor_c" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN outdoor_c REAL")
            if "eff_jt" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN eff_jt REAL")
            # Miner error journal (last 100 kept by app logic)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS miner_error_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seen_ts REAL NOT NULL,
                    seen_iso TEXT NOT NULL,
                    code TEXT NOT NULL,
                    cause TEXT,
                    miner_ts TEXT,
                    miner_sn TEXT,
                    miner_type TEXT,
                    miner_mac TEXT,
                    miner_label TEXT,
                    component_sn TEXT,
                    component_tag TEXT,
                    UNIQUE(code, miner_ts)
                )
                """
            )
            # migrate older journals
            err_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(miner_error_log)").fetchall()
            }
            for col, decl in (
                ("miner_sn", "TEXT"),
                ("miner_type", "TEXT"),
                ("miner_mac", "TEXT"),
                ("miner_label", "TEXT"),
                ("component_sn", "TEXT"),
                ("component_tag", "TEXT"),
            ):
                if col not in err_cols:
                    try:
                        conn.execute(
                            f"ALTER TABLE miner_error_log ADD COLUMN {col} {decl}"
                        )
                    except Exception:
                        pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_miner_err_seen ON miner_error_log(seen_ts DESC)"
            )
            conn.commit()
        finally:
            conn.close()


MINER_ERROR_LOG_MAX = 100
_IDENT_CACHE: dict = {"ts": 0.0, "data": None}
_IDENT_CACHE_TTL = 45.0
_BACKFILL_DONE = False
INFO_CACHE_FILE = DATA / "info_cache.json"
MINER_ID_CACHE_FILE = DATA / "miner_identity_cache.json"


def _miner_display_label(ident: dict | None) -> str:
    """Prefer ASIC SN; else model · MAC."""
    if not isinstance(ident, dict):
        return "—"
    sn = (ident.get("minersn") or "").strip()
    if sn:
        return sn
    parts: list[str] = []
    mt = (ident.get("miner_type") or "").strip()
    mac = (ident.get("mac") or "").strip()
    if mt:
        parts.append(mt)
    if mac:
        parts.append(mac)
    return " · ".join(parts) if parts else "—"


def _sm_slot_from_error(code: str | None, cause: str | None) -> int | None:
    """SM0..SM3 → slot 0..3 from cause text or known board error codes."""
    text = f"{cause or ''} {code or ''}"
    m = re.search(r"\bSM\s*([0-3])\b", text, re.I)
    if m:
        return int(m.group(1))
    c = str(code or "").strip().lstrip("0") or str(code or "").strip()
    if not c.isdigit():
        return None
    # last digit = board index for SM* families
    families = (
        "300", "301", "302", "303",
        "320", "321", "322", "323",
        "350", "351", "352", "353",
        "410", "411", "412", "413",
        "420", "421", "422", "423",
        "430", "431", "432", "433",
        "440", "441", "442", "443",
        "510", "511", "512", "513",
        "530", "531", "532", "533",
        "540", "541", "542", "543",
        "550", "551", "552", "553",
        "560", "561", "562", "563",
        "5110", "5111", "5112", "5113",
    )
    if c in families or any(c.startswith(p) and len(c) == len(p) for p in ("30", "32", "35", "41", "42", "43", "44", "51", "53", "54", "55", "56")):
        d = int(c[-1])
        if 0 <= d <= 3:
            return d
    # 5110-5113 frequency up
    if c.startswith("511") and len(c) == 4:
        d = int(c[-1])
        if 0 <= d <= 3:
            return d
    return None


def _is_psu_error_code(code: str | None) -> bool:
    c = str(code or "").strip().lstrip("0") or ""
    if not c.isdigit():
        return False
    n = int(c)
    return 200 <= n < 300


def _component_for_error(
    code: str | None,
    cause: str | None,
    ident: dict | None,
) -> tuple[str | None, str | None]:
    """
    Component SN for the failing part.
    SM2 reading chip id error → PCB SN of slot 2.
    PSU family → powersn.
    """
    ident = ident if isinstance(ident, dict) else {}
    slot = _sm_slot_from_error(code, cause)
    if slot is not None:
        boards = ident.get("boards") or []
        pcb = None
        for b in boards:
            if not isinstance(b, dict):
                continue
            try:
                if int(b.get("slot")) == slot:
                    pcb = b.get("pcb_sn")
                    break
            except (TypeError, ValueError):
                continue
        if not pcb and 0 <= slot < len(boards) and isinstance(boards[slot], dict):
            pcb = boards[slot].get("pcb_sn")
        return (str(pcb) if pcb else None), f"SM{slot}"
    if _is_psu_error_code(code):
        psn = ident.get("powersn")
        return (str(psn) if psn else None), "PSU"
    return None, None


def _load_miner_id_disk() -> dict | None:
    try:
        if not MINER_ID_CACHE_FILE.is_file():
            return None
        d = json.loads(MINER_ID_CACHE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _save_miner_id_disk(data: dict) -> None:
    try:
        MINER_ID_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _identity_usable(data: dict | None) -> bool:
    if not isinstance(data, dict):
        return False
    return bool(
        data.get("ok")
        or data.get("mac")
        or data.get("miner_type")
        or data.get("minersn")
        or (data.get("boards") or [])
    )


def get_miner_identity_cached(force: bool = False) -> dict:
    """Cached miner identity (type, mac, sn, boards PCB SN). Falls back to last good."""
    global _BACKFILL_DONE
    now = time.time()
    if (
        not force
        and _IDENT_CACHE.get("data")
        and (now - float(_IDENT_CACHE.get("ts") or 0)) < _IDENT_CACHE_TTL
        and _identity_usable(_IDENT_CACHE.get("data"))
    ):
        return dict(_IDENT_CACHE["data"])
    data: dict
    try:
        data = _collect_miner_identity()
    except Exception as e:
        data = {"ok": False, "error": str(e), "boards": []}
    if _identity_usable(data):
        _IDENT_CACHE["ts"] = now
        _IDENT_CACHE["data"] = data
        _save_miner_id_disk(data)
        if not _BACKFILL_DONE:
            try:
                n = backfill_miner_error_log_identity(ident=data)
                if n:
                    print(f"error log backfill: {n} rows")
                _BACKFILL_DONE = True
            except Exception as e:
                print(f"error log backfill: {e}")
        return dict(data)
    # live failed — use memory / disk last-good
    if _identity_usable(_IDENT_CACHE.get("data")):
        return dict(_IDENT_CACHE["data"])
    disk = _load_miner_id_disk()
    if _identity_usable(disk):
        _IDENT_CACHE["ts"] = now
        _IDENT_CACHE["data"] = disk
        return dict(disk)
    _IDENT_CACHE["ts"] = now
    _IDENT_CACHE["data"] = data
    return dict(data)


def log_miner_errors(
    entries: list[dict],
    *,
    miner_ctx: dict | None = None,
) -> int:
    """
    Append new miner errors to journal (dedupe by code+miner_ts).
    Stores miner identity + component SN for the miner active at log time.
    Keep only last MINER_ERROR_LOG_MAX rows. Returns number inserted.
    """
    if not entries:
        return 0
    ident = miner_ctx if isinstance(miner_ctx, dict) else get_miner_identity_cached()
    label = _miner_display_label(ident)
    miner_sn = (ident.get("minersn") or None) if isinstance(ident, dict) else None
    miner_type = (ident.get("miner_type") or None) if isinstance(ident, dict) else None
    miner_mac = (ident.get("mac") or None) if isinstance(ident, dict) else None
    if miner_sn == "":
        miner_sn = None

    now = time.time()
    iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    with _db_lock:
        conn = _db_connect()
        try:
            for e in entries:
                code = str(e.get("code") or "").strip()
                if not code:
                    continue
                miner_ts = e.get("ts")
                miner_ts_s = str(miner_ts) if miner_ts not in (None, "") else ""
                cause = e.get("cause") or e.get("message") or ""
                # allow per-entry override of component
                comp_sn = e.get("component_sn")
                comp_tag = e.get("component_tag")
                if not comp_sn and not comp_tag:
                    comp_sn, comp_tag = _component_for_error(code, cause, ident)
                try:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO miner_error_log
                            (seen_ts, seen_iso, code, cause, miner_ts,
                             miner_sn, miner_type, miner_mac, miner_label,
                             component_sn, component_tag)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            now,
                            iso,
                            code,
                            str(cause),
                            miner_ts_s,
                            miner_sn,
                            miner_type,
                            miner_mac,
                            label,
                            comp_sn,
                            comp_tag,
                        ),
                    )
                    inserted += int(cur.rowcount or 0)
                except Exception:
                    continue
            # prune oldest beyond limit
            conn.execute(
                """
                DELETE FROM miner_error_log
                WHERE id NOT IN (
                    SELECT id FROM miner_error_log
                    ORDER BY seen_ts DESC, id DESC
                    LIMIT ?
                )
                """,
                (MINER_ERROR_LOG_MAX,),
            )
            conn.commit()
        finally:
            conn.close()
    return inserted


def backfill_miner_error_log_identity(ident: dict | None = None) -> int:
    """
    Fill miner/component columns for old journal rows using Info identity
    (current live or last-good cache). Returns number of rows updated.
    """
    if ident is None:
        try:
            ident = get_miner_identity_cached(force=False)
        except Exception:
            ident = _load_miner_id_disk() or {}
    if not _identity_usable(ident):
        return 0
    label = _miner_display_label(ident)
    miner_sn = ident.get("minersn") or None
    if miner_sn == "":
        miner_sn = None
    miner_type = ident.get("miner_type") or None
    miner_mac = ident.get("mac") or None
    updated = 0
    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute(
                """
                SELECT id, code, cause, miner_label, component_sn, component_tag
                FROM miner_error_log
                """
            )
            rows = cur.fetchall()
            for r in rows:
                fields: dict = {}
                if not (r["miner_label"] or "").strip():
                    fields["miner_sn"] = miner_sn
                    fields["miner_type"] = miner_type
                    fields["miner_mac"] = miner_mac
                    fields["miner_label"] = label
                if not (r["component_sn"] or "").strip():
                    csn, ctag = _component_for_error(r["code"], r["cause"], ident)
                    if csn:
                        fields["component_sn"] = csn
                    if ctag and not (r["component_tag"] or "").strip():
                        fields["component_tag"] = ctag
                elif not (r["component_tag"] or "").strip():
                    _, ctag = _component_for_error(r["code"], r["cause"], ident)
                    if ctag:
                        fields["component_tag"] = ctag
                if not fields:
                    continue
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE miner_error_log SET {sets} WHERE id=?",
                    (*fields.values(), r["id"]),
                )
                updated += 1
            conn.commit()
        finally:
            conn.close()
    return updated


def _display_cause(code: str | None, cause: str | None) -> str:
    """Prefer official Whatsminer web Cause when stored text is a placeholder."""
    c = str(code or "").strip()
    msg = (cause or "").strip()
    if msg and msg.lower() not in (f"error code {c}".lower(), f"error {c}".lower()):
        return msg
    return _official_cause(c) or msg or (f"Error code {c}" if c else "—")


def query_miner_error_log(limit: int = 100) -> list[dict]:
    limit = max(1, min(100, int(limit)))
    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute(
                """
                SELECT id, seen_ts, seen_iso, code, cause, miner_ts,
                       miner_sn, miner_type, miner_mac, miner_label,
                       component_sn, component_tag
                FROM miner_error_log
                ORDER BY seen_ts DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = []
            for r in cur.fetchall():
                code = r["code"]
                cause = _display_cause(code, r["cause"])
                # display miner: stored label, or rebuild
                mlabel = (r["miner_label"] or "").strip()
                if not mlabel:
                    mlabel = _miner_display_label(
                        {
                            "minersn": r["miner_sn"],
                            "miner_type": r["miner_type"],
                            "mac": r["miner_mac"],
                        }
                    )
                ctag = (r["component_tag"] or "").strip()
                csn = (r["component_sn"] or "").strip()
                if not csn and not ctag:
                    csn2, ctag2 = _component_for_error(code, cause, None)
                    csn, ctag = (csn2 or ""), (ctag2 or "")
                comp_disp = csn or "—"
                if ctag and csn:
                    comp_disp = f"{ctag} · {csn}"
                elif ctag and not csn:
                    comp_disp = ctag
                rows.append(
                    {
                        "id": r["id"],
                        "seen_ts": r["seen_ts"],
                        "seen_iso": r["seen_iso"],
                        "code": code,
                        "cause": cause,
                        "miner_ts": r["miner_ts"] or None,
                        "miner_sn": r["miner_sn"] or None,
                        "miner_type": r["miner_type"] or None,
                        "miner_mac": r["miner_mac"] or None,
                        "miner": mlabel if mlabel != "—" else "—",
                        "component_sn": csn or None,
                        "component_tag": ctag or None,
                        "component": comp_disp,
                    }
                )
            return rows
        finally:
            conn.close()


def insert_sample(row: dict) -> None:
    with _db_lock:
        conn = _db_connect()
        try:
            uf = row.get("upfreq_ok")
            if uf is None:
                uf_db = None
            else:
                uf_db = 0 if uf in (0, False, "0") else 1
            conn.execute(
                """
                INSERT OR REPLACE INTO samples (
                    ts, ts_iso, liquid, env, chip_min, chip_avg, chip_max,
                    board0, board1, board2, board3,
                    power, power_limit, power_limit_set, power_pct_cmd,
                    freq, hashrate_th, mode, hash_stable, online, work_state,
                    upfreq_ok, outdoor_c, eff_jt
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["ts"],
                    row["ts_iso"],
                    row.get("liquid"),
                    row.get("env"),
                    row.get("chip_min"),
                    row.get("chip_avg"),
                    row.get("chip_max"),
                    row.get("board0"),
                    row.get("board1"),
                    row.get("board2"),
                    row.get("board3"),
                    row.get("power"),
                    row.get("power_limit"),
                    row.get("power_limit_set"),
                    row.get("power_pct_cmd"),
                    row.get("freq"),
                    row.get("hashrate_th"),
                    row.get("mode"),
                    row.get("hash_stable"),
                    0 if row.get("online") in (0, False, "0") else 1,
                    row.get("work_state"),
                    uf_db,
                    row.get("outdoor_c"),
                    row.get("eff_jt"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def prune_old(retention_days: int) -> int:
    cutoff = time.time() - retention_days * 86400
    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def clear_history_samples() -> int:
    """Delete all history samples (charts DB)."""
    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute("DELETE FROM samples")
            conn.commit()
            n = cur.rowcount
            try:
                conn.execute("VACUUM")
            except Exception:
                pass
            return n
        finally:
            conn.close()


def clear_miner_error_log() -> int:
    """Delete all miner error journal rows + freeze cache."""
    global _state
    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute("DELETE FROM miner_error_log")
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
    with _state_lock:
        _state["miner_error_cache"] = {}
        _save_state()
    return n


def query_history(
    hours: float | None = None,
    since: float | None = None,
    until: float | None = None,
    max_points: int = 2000,
) -> list[dict]:
    now = time.time()
    if since is None:
        if hours is None:
            hours = 24.0
        since = now - float(hours) * 3600
    if until is None:
        until = now
    max_points = max(10, min(20000, int(max_points)))

    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS c FROM samples WHERE ts >= ? AND ts <= ?",
                (since, until),
            )
            total = int(cur.fetchone()["c"])
            if total == 0:
                return []

            cur = conn.execute(
                """
                SELECT * FROM samples
                WHERE ts >= ? AND ts <= ?
                ORDER BY ts ASC
                """,
                (since, until),
            )
            all_rows = cur.fetchall()
            stride = max(1, (len(all_rows) + max_points - 1) // max_points)
            rows = all_rows[::stride][:max_points]
            return [dict(r) for r in rows]
        finally:
            conn.close()


def history_stats() -> dict:
    with _db_lock:
        conn = _db_connect()
        try:
            cur = conn.execute("SELECT COUNT(*) AS c, MIN(ts) AS tmin, MAX(ts) AS tmax FROM samples")
            row = cur.fetchone()
            return {
                "count": int(row["c"] or 0),
                "oldest_ts": row["tmin"],
                "newest_ts": row["tmax"],
                "oldest_iso": datetime.fromtimestamp(row["tmin"]).isoformat(timespec="seconds")
                if row["tmin"]
                else None,
                "newest_iso": datetime.fromtimestamp(row["tmax"]).isoformat(timespec="seconds")
                if row["tmax"]
                else None,
                "db_path": str(DB_FILE),
                "db_size_bytes": DB_FILE.stat().st_size if DB_FILE.exists() else 0,
            }
        finally:
            conn.close()


# ─── miner I/O ────────────────────────────────────────────────────────────────

# Last successful live read — used to skip write when ASIC is offline.
_last_live_ok_ts: float = 0.0
_last_live_ok_lock = threading.Lock()
_MINER_ONLINE_MAX_AGE_SEC = 45.0


def _mark_miner_live_ok() -> None:
    global _last_live_ok_ts
    with _last_live_ok_lock:
        _last_live_ok_ts = time.time()


def miner_is_online(*, max_age_sec: float | None = None, probe: bool = False) -> bool:
    """
    True if we recently read live data successfully.
    probe=True does a cheap summary ping when cache is stale.
    """
    age = float(max_age_sec if max_age_sec is not None else _MINER_ONLINE_MAX_AGE_SEC)
    with _last_live_ok_lock:
        last = float(_last_live_ok_ts or 0)
    if last > 0 and (time.time() - last) <= age:
        return True
    if not probe:
        return False
    try:
        miner_cmd({"cmd": "summary"}, timeout=4.0)
        _mark_miner_live_ok()
        return True
    except Exception:
        return False


def _parse_miner_json(text: str) -> dict:
    """
    Parse Whatsminer TCP JSON robustly.
    Handles: trailing junk, multiple objects, partial reads already completed,
    and common firmware quirks — never surface raw JSONDecodeError as «offline».
    """
    s = (text or "").replace("\x00", "").strip()
    if not s:
        raise TimeoutError("empty response from miner")
    dec = json.JSONDecoder()
    # Prefer first complete object starting at first '{'
    i = s.find("{")
    if i < 0:
        raise RuntimeError(f"miner response not JSON: {s[:80]!r}")
    try:
        obj, _end = dec.raw_decode(s, i)
        if isinstance(obj, dict):
            return obj
        raise RuntimeError(f"miner JSON root not object: {type(obj).__name__}")
    except json.JSONDecodeError:
        pass
    # Sanitize common garbage then retry
    cleaned = s[i:]
    cleaned = re.sub(r",\s*}", "}", cleaned)
    cleaned = re.sub(r",\s*]", "]", cleaned)
    cleaned = cleaned.replace("NaN", "null").replace("Infinity", "null")
    try:
        obj, _end = dec.raw_decode(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError as e:
        # Brace-match first object if truncated tail after valid body
        depth = 0
        in_str = False
        esc = False
        end_idx = -1
        for idx, ch in enumerate(cleaned):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx > 0:
            try:
                obj = json.loads(cleaned[:end_idx])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
        raise RuntimeError(
            f"bad JSON from miner (len={len(s)}, col={getattr(e, 'colno', '?')})"
        ) from e
    raise RuntimeError(f"miner JSON root not object after sanitize")


def _recv_json(sock: socket.socket, timeout: float = 5.0) -> dict:
    """Read one JSON object from miner TCP socket (may span multiple recv)."""
    deadline = time.time() + max(0.5, float(timeout))
    chunks: list[bytes] = []
    while time.time() < deadline:
        remain = max(0.05, deadline - time.time())
        sock.settimeout(remain)
        try:
            chunk = sock.recv(16384)
            if not chunk:
                break
            chunks.append(chunk)
            text = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace")
            # Need at least one complete object
            if "{" not in text:
                continue
            try:
                return _parse_miner_json(text)
            except RuntimeError as e:
                # incomplete — keep reading until timeout
                if "bad JSON" in str(e) or "not JSON" in str(e):
                    # if looks incomplete (unbalanced braces), continue
                    if text.count("{") > text.count("}"):
                        continue
                    raise
                raise
        except socket.timeout:
            break
    raw = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace")
    if not raw.strip():
        raise TimeoutError("empty response from miner")
    try:
        return _parse_miner_json(raw)
    except RuntimeError:
        # incomplete buffer after timeout
        if raw.count("{") > raw.count("}"):
            raise TimeoutError(
                f"incomplete JSON from miner (len={len(raw)})"
            ) from None
        raise


def _miner_cmd_unlocked(cmd: dict, timeout: float = 5.0) -> dict:
    """Send one JSON cmd to miner API. Caller must hold _miner_io_lock if needed."""
    payload = (json.dumps(cmd, separators=(",", ":")) + "\n").encode()
    with socket.create_connection((HOST_MINER, PORT_MINER), timeout=timeout) as sock:
        sock.sendall(payload)
        return _recv_json(sock, timeout=timeout)


def miner_cmd(cmd: dict, timeout: float = 5.0) -> dict:
    with _miner_io_lock:
        return _miner_cmd_unlocked(cmd, timeout=timeout)


def _read_text_file(path: str, max_len: int = 4096) -> str | None:
    try:
        p = Path(path)
        if not p.is_file():
            return None
        raw = p.read_bytes()[:max_len]
        # device-tree model is often null-terminated
        return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip() or None
    except Exception:
        return None


def _parse_ndmc_kv(text: str) -> dict[str, str]:
    """Parse simple `key: value` lines from `ndmc -c 'show …'` output."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if v:
            out[k] = v
    return out


def _disk_usage_entry(path: str | Path, label: str | None = None) -> dict | None:
    try:
        p = Path(path)
        if not p.exists():
            return None
        u = shutil.disk_usage(str(p))
        return {
            "path": str(p),
            "label": label or str(p),
            "total_b": int(u.total),
            "used_b": int(u.used),
            "free_b": int(u.free),
            "used_pct": round(100.0 * u.used / u.total, 1) if u.total else None,
        }
    except Exception:
        return None


def _host_memory() -> dict | None:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                key = parts[0].rstrip(":")
                try:
                    # values in kB
                    info[key] = int(parts[1]) * 1024
                except ValueError:
                    pass
        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        free = info.get("MemFree")
        if total is None:
            return None
        used = total - (available if available is not None else (free or 0))
        return {
            "total_b": total,
            "available_b": available,
            "free_b": free,
            "used_b": used,
            "used_pct": round(100.0 * used / total, 1) if total else None,
            "swap_total_b": info.get("SwapTotal"),
            "swap_free_b": info.get("SwapFree"),
        }
    except Exception:
        return None


def _host_uptime_sec() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def _collect_router_info() -> dict:
    router: dict = {
        "ok": False,
        "vendor": None,
        "model": None,
        "model_code": None,
        "title": None,
        "hw_version": None,
        "os_title": None,
        "os_release": None,
        "arch": None,
        "hostname": None,
        "device_tree_model": None,
        "source": None,
    }
    dt = _read_text_file("/proc/device-tree/model")
    if dt:
        router["device_tree_model"] = dt
        # e.g. "Keenetic KN-2710"
        m = re.search(r"(KN-\d+)", dt, re.I)
        if m:
            router["model_code"] = m.group(1).upper()
        if "keenetic" in dt.lower():
            router["vendor"] = "Keenetic"
        router["model"] = dt
        router["ok"] = True
        router["source"] = "device-tree"

    ndmc_bin = None
    for cand in ("/bin/ndmc", "/usr/bin/ndmc", "ndmc"):
        if cand == "ndmc" or Path(cand).is_file():
            ndmc_bin = cand
            break
    if ndmc_bin:
        try:
            proc = subprocess.run(
                [ndmc_bin, "-c", "show version"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            kv = _parse_ndmc_kv(text)
            if kv:
                router["ok"] = True
                router["source"] = "ndmc"
                router["vendor"] = kv.get("vendor") or kv.get("manufacturer") or router.get("vendor")
                router["model"] = kv.get("model") or router.get("model")
                router["hw_version"] = kv.get("hw_version")
                router["os_title"] = kv.get("title")
                router["os_release"] = kv.get("release")
                router["arch"] = kv.get("arch")
                # model often "Peak (KN-2710)"
                if router.get("model"):
                    m = re.search(r"(KN-\d+)", str(router["model"]), re.I)
                    if m:
                        router["model_code"] = m.group(1).upper()
            # hostname from show system (optional)
            try:
                proc2 = subprocess.run(
                    [ndmc_bin, "-c", "show system"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                kv2 = _parse_ndmc_kv((proc2.stdout or "") + "\n" + (proc2.stderr or ""))
                if kv2.get("hostname"):
                    router["hostname"] = kv2["hostname"]
                if kv2.get("model") and not router.get("model"):
                    router["model"] = kv2["model"]
                if kv2.get("hw_version") and not router.get("hw_version"):
                    router["hw_version"] = kv2["hw_version"]
            except Exception:
                pass
        except Exception as e:
            router["ndmc_error"] = str(e)

    if not router.get("hostname"):
        try:
            router["hostname"] = socket.gethostname()
        except Exception:
            pass
    if not router.get("arch"):
        try:
            router["arch"] = os.uname().machine
        except Exception:
            pass
    return router


def _parse_detect_hash_rate(raw) -> list[float | None]:
    """
    API v3 miner.detect-hash-rate — EEPROM Tagged Hashrate per slot (GHS).
    Format: \"91153:91153:94080:94080\" (works in Suspend; no board power needed).
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        out: list[float | None] = []
        for x in raw:
            v = _f(x)
            out.append(v if v is not None and v > 0 else None)
        return out
    s = str(raw).strip()
    if not s:
        return []
    out = []
    for part in s.replace(",", ":").split(":"):
        part = part.strip()
        if not part:
            out.append(None)
            continue
        v = _f(part)
        out.append(v if v is not None and v > 0 else None)
    return out


def _ghs_to_th(ghs: float | None) -> float | None:
    if ghs is None:
        return None
    try:
        g = float(ghs)
    except (TypeError, ValueError):
        return None
    if g <= 0:
        return None
    return g / 1000.0


# Whatsminer hashboard layout by model family.
# M63-class: 4 logical slots (2 physical PCBs × 2 virtual halves → paired temps).
# M60S-class: 3 physical hashboards, 3 sensors → chart all three board0/1/2.
HASHBOARD_LAYOUT: dict[str, dict] = {
    "M66": {"boards": 4, "chart": [0, 2], "note": "4 slots · paired sensors"},
    "M63": {"boards": 4, "chart": [0, 2], "note": "2 physical × 2 virtual slots"},
    "M60S": {"boards": 3, "chart": [0, 1, 2], "note": "3 hashboards"},
    "M60": {"boards": 3, "chart": [0, 1, 2]},
    "M56": {"boards": 3, "chart": [0, 1, 2]},
    "M53": {"boards": 3, "chart": [0, 1, 2]},
    "M50S": {"boards": 3, "chart": [0, 1, 2]},
    "M50": {"boards": 3, "chart": [0, 1, 2]},
    "M33S": {"boards": 3, "chart": [0, 1, 2]},
    "M30S": {"boards": 3, "chart": [0, 1, 2]},
    "M30": {"boards": 3, "chart": [0, 1, 2]},
    "M21S": {"boards": 3, "chart": [0, 1, 2]},
    "M20S": {"boards": 3, "chart": [0, 1, 2]},
}


def resolve_hashboard_layout(
    miner_type: str | None = None,
    *,
    n_devs: int | None = None,
    board_num: int | None = None,
) -> dict:
    """
    How many PCB/hashboard slots to show and which indices for charts.

    Priority:
      1) live DEVS count (when > 0) — source of truth on M60S (3) / M63 (4)
      2) v3 miner.board-num
      3) model map (M63→4, M60S→3, …)
      4) default 4
    """
    mt = str(miner_type or "").strip().upper().replace(" ", "").replace("-", "")
    base = mt.split("_")[0] if mt else ""
    layout: dict | None = None
    for key in sorted(HASHBOARD_LAYOUT.keys(), key=len, reverse=True):
        if base.startswith(key) or mt.startswith(key):
            layout = dict(HASHBOARD_LAYOUT[key])
            layout["model_key"] = key
            break
    if layout is None:
        n_guess = 4
        if board_num and int(board_num) > 0:
            n_guess = int(board_num)
        elif n_devs and int(n_devs) > 0:
            n_guess = int(n_devs)
        n_guess = max(1, min(8, n_guess))
        if n_guess >= 4:
            chart = [0, 2]
        elif n_guess >= 3:
            chart = [0, 1, 2]
        elif n_guess >= 2:
            chart = [0, 1]
        else:
            chart = [0]
        layout = {
            "boards": n_guess,
            "chart": chart,
            "model_key": "auto",
            "note": "auto from DEVS/board-num",
        }
    # Live DEVS wins when present — also fix chart slots for board count
    if n_devs is not None and int(n_devs) > 0:
        n = max(1, min(8, int(n_devs)))
        layout["boards"] = n
        layout["chart"] = _chart_slots_for_boards(n, layout.get("chart"))
    elif board_num is not None and int(board_num) > 0:
        n = max(1, min(8, int(board_num)))
        layout["boards"] = n
        layout["chart"] = _chart_slots_for_boards(n, layout.get("chart"))
    return layout


def _chart_slots_for_boards(n: int, preferred: list | None = None) -> list[int]:
    """Pick board indices for temp chart. 3-board ASICs → all three sensors."""
    n = max(1, min(8, int(n)))
    pref = [int(x) for x in (preferred or []) if isinstance(x, (int, float))]
    pref = [x for x in pref if 0 <= x < n]
    if n >= 4:
        # M63-class: keep paired physical sensors if still valid
        if pref == [0, 2] or (len(pref) == 2 and pref[0] == 0 and pref[-1] == 2):
            return [0, 2]
        if len(pref) >= 2:
            return pref[:3]
        return [0, 2]
    if n >= 3:
        return [0, 1, 2]
    if n >= 2:
        return [0, 1]
    return [0]


def _boards_from_v3_device_msg(msg: dict | None) -> list[dict]:
    """
    PCB SN + Tagged Hashrate (detect-hash-rate) + chipdata from API v3 get.device.info.
    Available in Suspend — data lives in hashboard EEPROM.
    """
    if not isinstance(msg, dict):
        return []
    miner = msg.get("miner") if isinstance(msg.get("miner"), dict) else {}
    if not miner:
        return []
    rates = _parse_detect_hash_rate(miner.get("detect-hash-rate"))
    try:
        n = int(miner.get("board-num") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        # infer from highest present pcbsn / rate
        n = max(len(rates), 4)
        for i in range(8):
            if miner.get(f"pcbsn{i}") or miner.get(f"chipdata{i}"):
                n = max(n, i + 1)
    n = max(1, min(8, n))
    boards: list[dict] = []
    for i in range(n):
        pcb = miner.get(f"pcbsn{i}") or miner.get(f"pcb_sn{i}") or miner.get(f"PCB SN{i}")
        if isinstance(pcb, str):
            pcb = pcb.strip() or None
        chip = miner.get(f"chipdata{i}") or miner.get(f"chip_data{i}")
        if isinstance(chip, str):
            chip = chip.strip() or None
        ghs = rates[i] if i < len(rates) else None
        boards.append(
            {
                "slot": i,
                "pcb_sn": pcb,
                "chip_data": chip,
                "tagged_ghs": ghs,
                "tagged_th": _ghs_to_th(ghs),
                "temp_c": None,
                "effective_chips": None,
                "source": "v3",
            }
        )
    return boards


def _merge_board_rows(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Merge board identity rows by slot — fill empty fields from secondary."""
    by_slot: dict[int, dict] = {}
    order: list[int] = []

    def _slot_of(b: dict, fallback: int) -> int:
        try:
            s = b.get("slot")
            if s is None:
                return fallback
            return int(s)
        except (TypeError, ValueError):
            return fallback

    # primary first (prefer EEPROM/v3 for SN + tagged)
    for i, b in enumerate(primary or []):
        if not isinstance(b, dict):
            continue
        sl = _slot_of(b, i)
        row = dict(b)
        row["slot"] = sl
        by_slot[sl] = row
        if sl not in order:
            order.append(sl)
    for i, b in enumerate(secondary or []):
        if not isinstance(b, dict):
            continue
        sl = _slot_of(b, i)
        if sl not in by_slot:
            row = dict(b)
            row["slot"] = sl
            by_slot[sl] = row
            order.append(sl)
            continue
        cur = by_slot[sl]
        for k, v in b.items():
            if k == "slot":
                continue
            if v in (None, "", []):
                continue
            if cur.get(k) in (None, "", []):
                cur[k] = v
    # stable slot order
    order_sorted = sorted(order)
    return [by_slot[s] for s in order_sorted if s in by_slot]


def _collect_miner_identity() -> dict:
    """
    ASIC identity for Info tab.
    PCB SN + Tagged Hashrate come from hashboard EEPROM — available in Suspend
    via API v3 get.device.info (devs often fails while mineroff).
    """
    out: dict = {
        "ok": False,
        "host": f"{HOST_MINER}:{PORT_MINER}",
        "miner_type": None,
        "fw_ver": None,
        "api_ver": None,
        "platform": None,
        "chip": None,
        "mac": None,
        "minersn": None,
        "powersn": None,
        "psu_model": None,
        "psu_hw_version": None,
        "psu_sw_version": None,
        "hostname": None,
        "boards": [],
        # EEPROM factory / tagged hashrate (GHS + TH) — not live mining rate
        "factory_ghs": None,
        "factory_th": None,
        "tagged_ghs": None,  # sum of board tagged (alias factory when present)
        "tagged_th": None,
        "board_num": None,
        "hash_board": None,
        "error": None,
    }
    try:
        ver = miner_cmd({"cmd": "get_version"}, timeout=3).get("Msg") or {}
        if isinstance(ver, dict):
            out["miner_type"] = ver.get("miner_type")
            out["fw_ver"] = ver.get("fw_ver")
            out["api_ver"] = ver.get("api_ver")
            out["platform"] = ver.get("platform")
            out["chip"] = ver.get("chip")
        info = miner_cmd({"cmd": "get_miner_info"}, timeout=3).get("Msg") or {}
        if isinstance(info, dict):
            out["mac"] = info.get("mac")
            out["minersn"] = info.get("minersn") or None
            out["powersn"] = info.get("powersn") or None
            out["hostname"] = info.get("hostname")
            if not out["powersn"]:
                out["powersn"] = None
            # empty string → None for cleaner UI
            if out["minersn"] == "":
                out["minersn"] = None
        try:
            psu = miner_cmd({"cmd": "get_psu"}, timeout=3).get("Msg") or {}
            if isinstance(psu, dict):
                out["psu_model"] = psu.get("model") or psu.get("name")
                out["psu_hw_version"] = psu.get("hw_version")
                out["psu_sw_version"] = psu.get("sw_version")
                if not out["powersn"]:
                    sn = psu.get("serial_no")
                    out["powersn"] = sn if sn not in (None, "") else None
        except Exception:
            pass

        # summary.Factory GHS — EEPROM total; works in Suspend
        try:
            sm = miner_cmd({"cmd": "summary"}, timeout=3).get("Msg") or {}
            if isinstance(sm, dict):
                fg = _f(sm.get("Factory GHS") or sm.get("factory_ghs"))
                if fg is not None and fg > 0:
                    out["factory_ghs"] = fg
                    out["factory_th"] = _ghs_to_th(fg)
        except Exception:
            pass

        boards_devs: list[dict] = []
        try:
            devs_raw = miner_cmd({"cmd": "devs"}, timeout=3)
            devs = (
                (devs_raw.get("DEVS") if isinstance(devs_raw, dict) else None) or []
            )
            for d in devs if isinstance(devs, list) else []:
                if not isinstance(d, dict):
                    continue
                # per-board Factory GHS = Tagged Hashrate (EEPROM)
                ghs = _f(d.get("Factory GHS") or d.get("factory_ghs"))
                if ghs is not None and ghs <= 0:
                    ghs = None
                boards_devs.append(
                    {
                        "slot": d.get("Slot"),
                        "pcb_sn": d.get("PCB SN") or d.get("pcb_sn"),
                        "chip_data": d.get("Chip Data") or d.get("chip_data"),
                        "temp_c": _f(d.get("Temperature")),
                        "effective_chips": d.get("Effective Chips"),
                        "tagged_ghs": ghs,
                        "tagged_th": _ghs_to_th(ghs),
                        "source": "devs",
                    }
                )
        except Exception:
            pass

        # API v3: PCB SN + detect-hash-rate even when Suspend (devs fails)
        boards_v3: list[dict] = []
        try:
            v3_msg = _fetch_v3_device_msg()
            if isinstance(v3_msg, dict):
                boards_v3 = _boards_from_v3_device_msg(v3_msg)
                miner = (
                    v3_msg.get("miner")
                    if isinstance(v3_msg.get("miner"), dict)
                    else {}
                )
                if miner:
                    if not out.get("miner_type") and miner.get("type"):
                        out["miner_type"] = miner.get("type")
                    if not out.get("minersn"):
                        msn = miner.get("miner-sn") or miner.get("minersn")
                        if isinstance(msn, str) and msn.strip():
                            out["minersn"] = msn.strip()
                        elif msn not in (None, ""):
                            out["minersn"] = str(msn)
                    try:
                        bn = int(miner.get("board-num") or 0)
                        if bn > 0:
                            out["board_num"] = bn
                    except (TypeError, ValueError):
                        pass
                    hb = miner.get("hash-board") or miner.get("hash_board")
                    if hb:
                        out["hash_board"] = hb
                    if not out.get("chip"):
                        cd0 = miner.get("chipdata0")
                        if cd0:
                            out["chip"] = cd0
                net = (
                    v3_msg.get("network")
                    if isinstance(v3_msg.get("network"), dict)
                    else {}
                )
                if net and not out.get("mac") and net.get("mac"):
                    out["mac"] = net.get("mac")
                pwr = (
                    v3_msg.get("power")
                    if isinstance(v3_msg.get("power"), dict)
                    else {}
                )
                if isinstance(pwr, dict):
                    if not out.get("powersn") and pwr.get("sn"):
                        out["powersn"] = pwr.get("sn")
                    if not out.get("psu_model"):
                        out["psu_model"] = pwr.get("model") or pwr.get("type")
                sys_ = (
                    v3_msg.get("system")
                    if isinstance(v3_msg.get("system"), dict)
                    else {}
                )
                if isinstance(sys_, dict):
                    if not out.get("fw_ver") and sys_.get("fwversion"):
                        out["fw_ver"] = sys_.get("fwversion")
                    if not out.get("platform") and sys_.get("platform"):
                        out["platform"] = sys_.get("platform")
                    if not out.get("api_ver") and sys_.get("api"):
                        out["api_ver"] = str(sys_.get("api"))
        except Exception as e:
            print(f"[ident] v3 device: {e}")

        # Prefer v3 for EEPROM fields (works in Suspend); merge live temp/chips from devs
        if boards_v3:
            out["boards"] = _merge_board_rows(boards_v3, boards_devs)
        else:
            out["boards"] = boards_devs

        # Sum tagged from boards if factory total missing
        sum_ghs = 0.0
        any_tagged = False
        for b in out["boards"]:
            g = _f(b.get("tagged_ghs"))
            if g is not None and g > 0:
                sum_ghs += g
                any_tagged = True
        if any_tagged:
            out["tagged_ghs"] = sum_ghs
            out["tagged_th"] = _ghs_to_th(sum_ghs)
            if out.get("factory_ghs") is None:
                out["factory_ghs"] = sum_ghs
                out["factory_th"] = out["tagged_th"]
        elif out.get("factory_ghs") is not None:
            out["tagged_ghs"] = out["factory_ghs"]
            out["tagged_th"] = out.get("factory_th")

        if out.get("board_num") is None and out["boards"]:
            out["board_num"] = len(out["boards"])

        out["ok"] = bool(
            out.get("miner_type")
            or out.get("mac")
            or out.get("boards")
            or out.get("factory_ghs")
        )
    except Exception as e:
        out["error"] = str(e)
    return out


def collect_system_info() -> dict:
    """Router + miner identity + host resources for Info tab."""
    disks: list[dict] = []
    seen: set[str] = set()
    # skip rootfs (always full) and poolheat data (same FS as /opt on Peak)
    for path, label in (
        ("/opt", "/opt (Entware)"),
        ("/storage", "/storage"),
        ("/tmp", "/tmp"),
    ):
        ent = _disk_usage_entry(path, label)
        if not ent:
            continue
        # de-dupe by total+free fingerprint (bind-mounts)
        key = f"{ent['total_b']}:{ent['free_b']}:{ent.get('used_b')}"
        if key in seen:
            continue
        seen.add(key)
        disks.append(ent)

    db_size = None
    try:
        if DB_FILE.is_file():
            db_size = DB_FILE.stat().st_size
    except Exception:
        pass

    miner = get_miner_identity_cached(force=True)
    router = _collect_router_info()
    result = {
        "ok": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "router": router,
        "miner": miner,
        "resources": {
            "disks": disks,
            "memory": _host_memory(),
            "uptime_sec": _host_uptime_sec(),
            "history_db_b": db_size,
            "history_db_path": str(DB_FILE),
            "data_path": str(DATA),
            "www_path": str(ROOT),
        },
        "poolheat": {
            **get_version_info(),
            "http_port": HTTP_PORT,
            "poll_interval_sec": int(POLL_INTERVAL_SEC),
            "dry_run": bool(DRY_RUN),
            "miner_host": f"{HOST_MINER}:{PORT_MINER}",
        },
    }
    # full Info snapshot for instant UI (models/versions/boards/resources)
    try:
        cache_blob = dict(result)
        cache_blob["cached_at"] = result["ts"]
        INFO_CACHE_FILE.write_text(
            json.dumps(cache_blob, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass
    return result


def load_info_cache() -> dict | None:
    """Last successful Info snapshot (router/miner) for fast UI paint."""
    try:
        if not INFO_CACHE_FILE.is_file():
            return None
        data = json.loads(INFO_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_version_file(path: Path) -> str | None:
    try:
        if path.is_file():
            v = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            return v or None
    except Exception:
        pass
    return None


def get_app_version() -> str:
    """Installed software version (semver-ish)."""
    env = (os.environ.get("POOLHEAT_VERSION") or "").strip()
    if env:
        return env
    here = Path(__file__).resolve().parent
    for p in (
        here / "VERSION",
        ROOT / "VERSION",
        Path("/opt/lib/poolheat/VERSION"),
        Path("/opt/share/poolheat/VERSION"),
        here.parent / "VERSION",  # repo root when running from ui-demo/
    ):
        v = _read_version_file(p)
        if v:
            return v
    return _DEFAULT_APP_VERSION


def parse_version_tuple(s: str) -> tuple:
    """Parse 'v0.2.0-1' → (0, 2, 0). Non-numeric → (0,)."""
    if not s:
        return (0,)
    core = str(s).strip().lstrip("vV").split("+")[0].split("-")[0]
    parts: list[int] = []
    for p in core.split("."):
        try:
            parts.append(int(re.sub(r"[^0-9].*", "", p) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def version_cmp(a: str, b: str) -> int:
    """-1 if a<b, 0 if equal, 1 if a>b (numeric semver core)."""
    ta, tb = parse_version_tuple(a), parse_version_tuple(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def get_version_info() -> dict:
    return {
        "version": get_app_version(),
        "github_repo": GITHUB_REPO,
        "github_branch": GITHUB_BRANCH,
        "github_url": f"https://github.com/{GITHUB_REPO}",
    }


def _http_json(url: str, timeout: float = 12.0) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"poolheat/{get_app_version()}",
            "Accept": "application/vnd.github+json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _http_text(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"poolheat/{get_app_version()}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_bytes(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"poolheat/{get_app_version()}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def check_github_update() -> dict:
    """
    Compare installed VERSION with GitHub:
      1) latest release tag (if any)
      2) latest git tag
      3) VERSION file on default branch
    Also returns latest commit on branch for reference.
    """
    current = get_app_version()
    out: dict = {
        "ok": True,
        "current_version": current,
        "latest_version": None,
        "update_available": False,
        "status": "unknown",  # up_to_date | update_available | local_ahead | unknown
        "source": None,
        "release_name": None,
        "release_url": None,
        "tag": None,
        "commit_sha": None,
        "commit_url": None,
        "published_at": None,
        "github_repo": GITHUB_REPO,
        "github_branch": GITHUB_BRANCH,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "error": None,
        "notes": None,
    }
    latest: str | None = None
    try:
        # 1) releases
        try:
            rel = _http_json(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            )
            if isinstance(rel, dict) and rel.get("tag_name") and not rel.get("message"):
                latest = str(rel["tag_name"]).lstrip("vV")
                out["source"] = "release"
                out["tag"] = rel.get("tag_name")
                out["release_name"] = rel.get("name") or rel.get("tag_name")
                out["release_url"] = rel.get("html_url")
                out["published_at"] = rel.get("published_at")
                # keep newlines for UI pre-wrap; soft cap length
                body_notes = (rel.get("body") or "").replace("\r\n", "\n").strip()
                out["notes"] = body_notes[:4000] if body_notes else None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        except Exception:
            pass

        # 2) tags
        if not latest:
            try:
                tags = _http_json(
                    f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=5"
                )
                if isinstance(tags, list) and tags:
                    t0 = tags[0]
                    if isinstance(t0, dict) and t0.get("name"):
                        latest = str(t0["name"]).lstrip("vV")
                        out["source"] = "tag"
                        out["tag"] = t0.get("name")
                        out["release_url"] = (
                            f"https://github.com/{GITHUB_REPO}/releases/tag/{t0['name']}"
                        )
                        if isinstance(t0.get("commit"), dict):
                            out["commit_sha"] = (t0["commit"].get("sha") or "")[:12] or None
            except Exception:
                pass

        # 3) VERSION on branch (GitHub Contents API — no CDN cache lag)
        if not latest:
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{GITHUB_REPO}/contents/VERSION"
                    f"?ref={urllib.parse.quote(GITHUB_BRANCH)}",
                    headers={
                        "User-Agent": f"poolheat/{get_app_version()}",
                        "Accept": "application/vnd.github.raw",
                    },
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
                if line and "404" not in line.lower() and len(line) < 40:
                    # accept "0.2.0" or "v0.2.0"
                    if re.match(r"^v?\d+(\.\d+)*", line, re.I):
                        latest = line.lstrip("vV")
                        out["source"] = f"VERSION@{GITHUB_BRANCH}"
                        out["release_url"] = (
                            f"https://github.com/{GITHUB_REPO}/blob/"
                            f"{GITHUB_BRANCH}/VERSION"
                        )
            except urllib.error.HTTPError as e:
                if e.code not in (404, 403):
                    # 403 rate-limit etc. — surface later if nothing else works
                    out["_version_http"] = e.code
            except Exception as e:
                out["_version_err"] = str(e)

        # always try commit tip for branch (metadata)
        try:
            commit = _http_json(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
            )
            if isinstance(commit, dict) and commit.get("sha"):
                out["commit_sha"] = commit["sha"][:12]
                out["commit_url"] = commit.get("html_url")
                if not out.get("published_at"):
                    try:
                        out["published_at"] = (
                            (commit.get("commit") or {})
                            .get("committer", {})
                            .get("date")
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        out["latest_version"] = latest
        if latest:
            cmp = version_cmp(current, latest)
            if cmp < 0:
                out["update_available"] = True
                out["status"] = "update_available"
            elif cmp == 0:
                out["update_available"] = False
                out["status"] = "up_to_date"
            else:
                out["update_available"] = False
                out["status"] = "local_ahead"
        else:
            # No published version channel yet — still allow install from branch
            out["status"] = "branch_only"
            out["update_available"] = False
            out["latest_version"] = None
            out["error"] = None
            out["notes"] = (
                f"На GitHub нет release/tag/VERSION. "
                f"Можно поставить код с ветки {GITHUB_BRANCH}"
                + (f" ({out['commit_sha']})" if out.get("commit_sha") else "")
                + ". После push VERSION или Release vX.Y.Z проверка станет точной."
            )
        # drop internal diagnostics from public payload
        out.pop("_version_http", None)
        out.pop("_version_err", None)
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
        out["status"] = "error"

    _update_state["last_check"] = out
    return out


def _is_entware_layout() -> bool:
    return Path("/opt/lib/poolheat").is_dir() and Path("/opt/share/poolheat/www").is_dir()


def _restart_poolheat_later() -> None:
    """
    Restart after GitHub apply. Must outlive this process: killing serve.py
    would otherwise abort an in-process restart (or a timed-out foreground
    poolheatd) and leave the service dead.
    """
    def _run() -> None:
        time.sleep(1.2)
        log = "/opt/var/poolheat/poolheat.log"
        # Detached shell in a new session — survives kill of current serve.py
        script = f"""
exec >>{log} 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] poolheat update: restart begin"
sleep 1
# stop any serve.py / tracked pid
if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
  /opt/etc/init.d/S99poolheat-standalone stop || true
elif [ -x /opt/etc/init.d/S99poolheat ]; then
  /opt/etc/init.d/S99poolheat stop || true
fi
for p in $(ps w 2>/dev/null | grep '[s]erve.py' | awk '{{print $1}}'); do
  kill "$p" 2>/dev/null || true
done
sleep 1
# start (prefer standalone — no rc.func / PROCS=poolheatd mismatch)
ok=0
if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
  if /opt/etc/init.d/S99poolheat-standalone start; then ok=1; fi
fi
if [ "$ok" -eq 0 ] && [ -x /opt/etc/init.d/S99poolheat ]; then
  if /opt/etc/init.d/S99poolheat start; then ok=1; fi
fi
if [ "$ok" -eq 0 ]; then
  mkdir -p /opt/var/poolheat /opt/var/run
  if [ -x /opt/bin/poolheatd ]; then
    /opt/bin/poolheatd >>{log} 2>&1 &
    echo $! > /opt/var/run/poolheatd.pid
  else
    /opt/bin/python3 /opt/lib/poolheat/serve.py >>{log} 2>&1 &
    echo $! > /opt/var/run/poolheatd.pid
  fi
  sleep 1
fi
if ps w 2>/dev/null | grep -q '[s]erve.py'; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] poolheat update: restart OK"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] poolheat update: restart FAILED — no serve.py"
fi
"""
        try:
            subprocess.Popen(
                ["sh", "-c", script],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception as e:
            try:
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"poolheat update: spawn restart failed: {e}\n")
            except Exception:
                pass

    threading.Thread(target=_run, name="poolheat-restart", daemon=True).start()


def _queue_update_restart_notify(
    chat_id,
    lang: str = "ru",
    *,
    from_version: str | None = None,
    to_version: str | None = None,
    source: str = "telegram",
) -> None:
    """
    Persist a one-shot TG message for after service restart.
    Written to DATA so it survives kill of serve.py.
    """
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        payload = {
            "chat_id": chat_id,
            "lang": "en" if str(lang or "").lower().startswith("en") else "ru",
            "from_version": from_version,
            "to_version": to_version or get_app_version(),
            "source": source,
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        }
        UPDATE_NOTIFY_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[update] restart notify queued → chat {chat_id}")
    except Exception as e:
        print(f"[update] restart notify queue fail: {e}")


def _flush_update_restart_notify() -> None:
    """
    After boot: if an OTA left a pending notify, send success to that chat.
    Safe to call multiple times; file is removed first to avoid loops.
    """
    path = UPDATE_NOTIFY_FILE
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[update] restart notify read fail: {e}")
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    if not isinstance(raw, dict):
        return
    chat_id = raw.get("chat_id")
    if chat_id is None:
        return
    en = str(raw.get("lang") or "ru").lower().startswith("en")
    ver = str(raw.get("to_version") or get_app_version() or "?")
    from_v = raw.get("from_version")
    try:
        # ensure TG config loaded
        if not _tg_cfg.get("bot_token") or not _tg_cfg.get("enabled"):
            print("[update] restart notify skipped: telegram disabled")
            return
        if en:
            if from_v:
                msg = (
                    f"✅ Restart OK after update\n"
                    f"{from_v} → {ver}\n"
                    f"Service is online again."
                )
            else:
                msg = f"✅ Restart OK · {ver}\nService is online again."
        else:
            if from_v:
                msg = (
                    f"✅ Перезапуск после обновления OK\n"
                    f"{from_v} → {ver}\n"
                    f"Сервис снова онлайн."
                )
            else:
                msg = f"✅ Перезапуск OK · {ver}\nСервис снова онлайн."
        tg_send_message(
            chat_id,
            msg,
            reply_markup=_tg_main_keyboard(
                "en" if en else "ru", chat_id
            ),
        )
        print(f"[update] restart notify sent → chat {chat_id}")
    except Exception as e:
        print(f"[update] restart notify send fail: {e}")
        # re-queue once if send failed (network blip right after start)
        try:
            raw["retry"] = int(raw.get("retry") or 0) + 1
            if int(raw["retry"]) <= 2:
                path.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        except Exception:
            pass


def apply_github_update(ref: str | None = None) -> dict:
    """
    Download GitHub archive (tag/branch) and install serve.py + UI + VERSION.
    Does not overwrite /opt/etc/poolheat/config.json.
    """
    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "error": "update already in progress"}
    _update_state["busy"] = True
    from_ver = get_app_version()
    try:
        if not _is_entware_layout():
            # allow local demo install into ROOT / next to serve.py
            lib_dir = Path(__file__).resolve().parent
            www_dir = ROOT if ROOT.is_dir() else lib_dir
            version_targets = [lib_dir / "VERSION"]
            if ROOT != lib_dir:
                version_targets.append(ROOT / "VERSION")
            entware = False
        else:
            lib_dir = Path("/opt/lib/poolheat")
            www_dir = Path("/opt/share/poolheat/www")
            version_targets = [
                Path("/opt/lib/poolheat/VERSION"),
                Path("/opt/share/poolheat/VERSION"),
            ]
            entware = True

        target_ref = (ref or "").strip()
        # Prefer latest known from check, else branch
        if not target_ref:
            last = _update_state.get("last_check") or {}
            if isinstance(last, dict):
                target_ref = last.get("tag") or ""
                # if only VERSION@branch, install from branch
                if not target_ref and last.get("source", "").startswith("VERSION@"):
                    target_ref = GITHUB_BRANCH
            target_ref = target_ref or GITHUB_BRANCH
        target_ref = str(target_ref).strip() or GITHUB_BRANCH

        # Build archive URL
        # tags/branches both work as refs/heads/X or refs/tags/X; GitHub also accepts
        # https://github.com/owner/repo/archive/refs/heads/main.tar.gz
        if re.match(r"^[0-9a-f]{7,40}$", target_ref):
            archive_url = (
                f"https://codeload.github.com/{GITHUB_REPO}/tar.gz/{target_ref}"
            )
        elif target_ref == GITHUB_BRANCH or not target_ref.startswith("v"):
            # try heads first; for version tags user may pass v0.2.0
            if re.match(r"^v?\d+\.\d+", target_ref):
                tag = target_ref if target_ref.startswith("v") else f"v{target_ref}"
                archive_url = (
                    f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.tar.gz"
                )
                # also keep plain tag without v as fallback later
            else:
                archive_url = (
                    f"https://github.com/{GITHUB_REPO}/archive/refs/heads/"
                    f"{target_ref}.tar.gz"
                )
        else:
            archive_url = (
                f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{target_ref}.tar.gz"
            )

        import tempfile
        import tarfile

        errors: list[str] = []
        blob: bytes | None = None
        used_url = archive_url
        urls_try = [archive_url]
        # fallbacks
        if "refs/tags/" in archive_url:
            bare = target_ref.lstrip("v")
            urls_try.append(
                f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.tar.gz"
            )
            urls_try.append(
                f"https://github.com/{GITHUB_REPO}/archive/refs/tags/v{bare}.tar.gz"
            )
            urls_try.append(
                f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{bare}.tar.gz"
            )
        elif "refs/heads/" in archive_url:
            pass
        for u in urls_try:
            try:
                blob = _http_bytes(u, timeout=90)
                used_url = u
                break
            except Exception as e:
                errors.append(f"{u}: {e}")
                blob = None
        if not blob:
            return {
                "ok": False,
                "error": "download failed: " + "; ".join(errors[:3]),
                "tried": urls_try,
            }

        installed: list[str] = []
        new_version = None
        with tempfile.TemporaryDirectory(prefix="poolheat-upd-") as td:
            tdir = Path(td)
            tgz_path = tdir / "src.tar.gz"
            tgz_path.write_bytes(blob)
            with tarfile.open(tgz_path, "r:gz") as tar:
                # safe extract: only known relative paths
                def _safe_members(members):
                    for m in members:
                        name = m.name.replace("\\", "/")
                        if name.startswith("/") or ".." in name.split("/"):
                            continue
                        # strip first path component (repo-branch/)
                        parts = name.split("/", 1)
                        if len(parts) < 2:
                            continue
                        rel = parts[1]
                        if rel in (
                            "ui-demo/serve.py",
                            "ui-demo/index.html",
                            "VERSION",
                            "packaging/entware/opt/bin/poolheatd",
                            "packaging/entware/opt/etc/init.d/S99poolheat",
                            "packaging/entware/opt/etc/init.d/S99poolheat-standalone",
                        ) or rel.startswith("ui-demo/") and rel.endswith(
                            (".py", ".html")
                        ):
                            yield m

                tar.extractall(path=tdir, members=_safe_members(tar))

            # find extracted root
            roots = [p for p in tdir.iterdir() if p.is_dir()]
            src_root = roots[0] if roots else tdir

            mapping = [
                (src_root / "ui-demo" / "serve.py", lib_dir / "serve.py"),
                (
                    src_root / "ui-demo" / "whatsminer_driver.py",
                    lib_dir / "whatsminer_driver.py",
                ),
                (
                    src_root / "ui-demo" / "luci_proxy.py",
                    lib_dir / "luci_proxy.py",
                ),
                (src_root / "ui-demo" / "index.html", www_dir / "index.html"),
            ]
            ver_src = src_root / "VERSION"
            if ver_src.is_file():
                new_version = _read_version_file(ver_src)
                for vt in version_targets:
                    mapping.append((ver_src, vt))

            if entware:
                mapping.extend(
                    [
                        (
                            src_root
                            / "packaging/entware/opt/bin/poolheatd",
                            Path("/opt/bin/poolheatd"),
                        ),
                        (
                            src_root
                            / "packaging/entware/opt/etc/init.d/S99poolheat",
                            Path("/opt/etc/init.d/S99poolheat"),
                        ),
                        (
                            src_root
                            / "packaging/entware/opt/etc/init.d/S99poolheat-standalone",
                            Path("/opt/etc/init.d/S99poolheat-standalone"),
                        ),
                    ]
                )

            for src, dst in mapping:
                if not src.is_file():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                data = src.read_bytes()
                tmp = dst.with_suffix(dst.suffix + ".new")
                tmp.write_bytes(data)
                tmp.replace(dst)
                if dst.name in ("poolheatd", "S99poolheat", "S99poolheat-standalone", "serve.py"):
                    try:
                        os.chmod(dst, 0o755)
                    except Exception:
                        pass
                installed.append(str(dst))

        if not installed:
            return {
                "ok": False,
                "error": "archive extracted but no installable files found",
                "url": used_url,
            }

        result = {
            "ok": True,
            "installed": installed,
            "from_version": from_ver,
            "to_version": get_app_version(),
            "ref": target_ref,
            "url": used_url,
            "restart": entware,
            "message": (
                "Файлы обновлены"
                + ("; сервис перезапускается…" if entware else "")
            ),
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        }
        _update_state["last_apply"] = result
        if entware:
            _restart_poolheat_later()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _update_state["busy"] = False
        _update_lock.release()


def _add_to_16(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) % 16:
        b += b"\x00" * (16 - len(b) % 16)
    return b


# Whatsminer: max ~100 tokens, default lifetime ~30 min (API manual Code 136).
# Each successful get_token burns a slot — cache & reuse; do not spam get_token.
_OVER_MAX_CONNECT_RU = (
    "лимит токенов API майнера (over max connect, до 100 / ~30 мин). "
    "Не жмите повторно: подождите до 30 мин или перезагрузите ASIC (питание / web). "
    "Read-команды (summary) не при чём — только get_token."
)

_token_cache_lock = threading.Lock()
# {host, port, pwd_fp, host_sign, host_passwd_md5, expires_at}
_token_cache: dict | None = None

# After over-max: freeze TCP privileged writes so policy cannot burn more slots.
# LuCI-first ops (mode/pools/reboot/restart) stay available.
_tcp_write_backoff_lock = threading.Lock()
_tcp_write_backoff_until: float = 0.0
_TCP_WRITE_BACKOFF_SEC = 1200.0  # 20 min


def _msg_is_over_max_connect(msg) -> bool:
    if not isinstance(msg, str):
        return False
    low = msg.strip().lower()
    return (
        low == "over max connect"
        or "over max connect" in low
        or "лимит токенов api" in low
    )


def _note_tcp_write_exhausted(sec: float | None = None) -> None:
    global _tcp_write_backoff_until
    wait = float(sec if sec is not None else _TCP_WRITE_BACKOFF_SEC)
    with _tcp_write_backoff_lock:
        _tcp_write_backoff_until = max(
            _tcp_write_backoff_until, time.time() + max(60.0, wait)
        )


def _tcp_write_blocked() -> bool:
    with _tcp_write_backoff_lock:
        return time.time() < _tcp_write_backoff_until


def _tcp_write_blocked_msg() -> str:
    with _tcp_write_backoff_lock:
        rem = max(0, int(_tcp_write_backoff_until - time.time()))
    return f"{_OVER_MAX_CONNECT_RU} · backoff {rem}s (LuCI mode/reboot всё ещё работают)"


def _password_fingerprint(password: str) -> str:
    return hashlib.sha256((password or "").encode("utf-8")).hexdigest()[:16]


def _token_cache_get(password: str) -> dict | None:
    global _token_cache
    with _token_cache_lock:
        c = _token_cache
        if not c:
            return None
        if c.get("host") != HOST_MINER or int(c.get("port") or 0) != int(PORT_MINER):
            return None
        if c.get("pwd_fp") != _password_fingerprint(password):
            return None
        if float(c.get("expires_at") or 0) <= time.time():
            _token_cache = None
            return None
        return {
            "host_sign": c["host_sign"],
            "host_passwd_md5": c["host_passwd_md5"],
        }


def _token_cache_put(password: str, token: dict, *, ttl_sec: float) -> None:
    global _token_cache
    with _token_cache_lock:
        _token_cache = {
            "host": HOST_MINER,
            "port": int(PORT_MINER),
            "pwd_fp": _password_fingerprint(password),
            "host_sign": token["host_sign"],
            "host_passwd_md5": token["host_passwd_md5"],
            "expires_at": time.time() + max(30.0, float(ttl_sec)),
        }


def _token_cache_clear() -> None:
    global _token_cache
    with _token_cache_lock:
        _token_cache = None


def _decrypt_privileged_response(cipher, raw: dict) -> dict:
    """Miner may return ciphertext in «data» or «enc» (base64 string)."""
    if not isinstance(raw, dict):
        return raw  # type: ignore[return-value]
    blob = None
    for k in ("data", "enc"):
        v = raw.get(k)
        # request uses enc:1 (int); response uses enc:"<base64>"
        if isinstance(v, str) and len(v) > 16:
            blob = v
            break
    if not blob:
        return raw
    try:
        pt = cipher.decrypt(base64.b64decode(blob)).split(b"\x00")[0].decode()
        return json.loads(pt)
    except Exception:
        return raw


def get_token_data(
    password: str,
    *,
    max_attempts: int = 3,
    unlocked: bool = False,
    force_refresh: bool = False,
) -> dict:
    """
    Privileged Whatsminer token. Cached ~25 min (API default ~30).

    When unlocked=True (caller holds _miner_io_lock): NEVER sleep — only one
    attempt. Sleeping under the I/O lock freezes live poll + Telegram status.

    After over max connect: global TCP-write backoff (no further get_token
    attempts) so policy cannot burn remaining slots. LuCI control still works.
    """
    if not force_refresh:
        cached = _token_cache_get(password)
        if cached:
            return cached
    # No cache and already exhausted — fail fast (do not open more sessions)
    if _tcp_write_blocked() and not force_refresh:
        raise RuntimeError(_tcp_write_blocked_msg())

    last_err: Exception | None = None
    # Under exclusive lock: single shot only (caller retries outside lock).
    attempts = 1 if unlocked else max(1, int(max_attempts))
    for attempt in range(attempts):
        try:
            if unlocked:
                data = _miner_cmd_unlocked({"cmd": "get_token"}, timeout=6.0)
            else:
                data = miner_cmd({"cmd": "get_token"}, timeout=6.0)
        except Exception as e:
            last_err = e
            if unlocked:
                break
            time.sleep(min(4.0, 1.0 + attempt * 1.0))
            continue
        msg = data.get("Msg") if isinstance(data, dict) else None
        if _msg_is_over_max_connect(msg):
            last_err = RuntimeError("over max connect")
            _note_tcp_write_exhausted()
            if unlocked:
                # Do not sleep while holding _miner_io_lock.
                break
            # Outside lock path: brief wait between free-standing retries
            time.sleep(min(30.0, 8.0 + attempt * 8.0))
            continue
        if not isinstance(msg, dict) or "salt" not in msg or "time" not in msg:
            raise RuntimeError(f"get_token failed: {msg!r}")
        pwd_hash = md5_crypt.hash(password, salt=msg["salt"])
        host_passwd_md5 = pwd_hash.split("$")[3]
        tmp = md5_crypt.hash(host_passwd_md5 + msg["time"], salt=msg["newsalt"])
        host_sign = tmp.split("$")[3]
        token = {"host_sign": host_sign, "host_passwd_md5": host_passwd_md5}
        # timeout field: "0" = no expire; missing = 30 min (Whatsminer manual)
        ttl = 25 * 60.0
        try:
            to = msg.get("timeout")
            if to is not None and str(to).strip() not in ("", "0"):
                ttl = max(60.0, min(30 * 60.0, float(to)))
        except (TypeError, ValueError):
            pass
        _token_cache_put(password, token, ttl_sec=ttl)
        return token
    if last_err and "over max" in str(last_err).lower():
        raise RuntimeError(_OVER_MAX_CONNECT_RU) from last_err
    if last_err:
        raise RuntimeError(f"get_token: {last_err}") from last_err
    raise RuntimeError(_OVER_MAX_CONNECT_RU)


def _miner_write_ports() -> list[int]:
    """
    TCP write ports to try (order matters).
    4028 = classic BTMiner API (get_version api_ver 2.x).
    4029 = temporary «API-v2 / IP access mode» seen during restarts on some FW.
    """
    ports: list[int] = []
    for p in (int(PORT_MINER), 4029, 4028):
        if p not in ports:
            ports.append(p)
    return ports


def privileged_cmd(
    cmd: dict,
    password: str,
    *,
    token_attempts: int = 3,
    ports: list[int] | None = None,
) -> dict:
    """
    Encrypted privileged write. Reuses cached token.
    Tries several TCP ports (4028 then 4029) — not a different crypto,
    just alternate API listeners on newer firmwares.
    Retries over max connect with sleep OUTSIDE _miner_io_lock so TG/live stay responsive.
    """
    last_err: Exception | None = None
    attempts = max(1, int(token_attempts))
    write_ports = list(ports) if ports else _miner_write_ports()
    for port in write_ports:
        for attempt in range(attempts):
            try:
                with _miner_io_lock:
                    token = get_token_data(
                        password, max_attempts=1, unlocked=True
                    )
                    out_cmd = dict(cmd)
                    out_cmd["token"] = token["host_sign"]
                    aeskey = binascii.unhexlify(
                        hashlib.sha256(token["host_passwd_md5"].encode()).hexdigest()
                    )
                    cipher = AES.new(aeskey, AES.MODE_ECB)
                    api_str = json.dumps(out_cmd, separators=(",", ":"))
                    enc = base64.b64encode(
                        cipher.encrypt(_add_to_16(api_str))
                    ).decode()
                    payload = (
                        json.dumps({"enc": 1, "data": enc}, separators=(",", ":"))
                        + "\n"
                    ).encode()
                    try:
                        with socket.create_connection(
                            (HOST_MINER, int(port)), timeout=8
                        ) as sock:
                            sock.sendall(payload)
                            raw = _recv_json(sock, timeout=25)
                    except (OSError, TimeoutError, socket.timeout) as e:
                        cname = str(out_cmd.get("cmd") or "")
                        if cname in ("factory_reset", "reboot", "net_config"):
                            return {
                                "STATUS": "S",
                                "Msg": f"{cname} sent (link dropped: {e})",
                                "Code": 131,
                                "port": int(port),
                            }
                        # try next port
                        last_err = e
                        break
                    if isinstance(raw, dict):
                        dec = _decrypt_privileged_response(cipher, raw)
                        try:
                            st = str(dec.get("STATUS") or "").upper()
                            msg = str(dec.get("Msg") or "").lower()
                            code = dec.get("Code")
                            if code == 135 or (
                                "token" in msg
                                and (
                                    "error" in msg
                                    or "invalid" in msg
                                    or "check" in msg
                                )
                            ):
                                _token_cache_clear()
                            elif st in ("E", "F") and "token" in msg:
                                _token_cache_clear()
                        except Exception:
                            pass
                        if isinstance(dec, dict):
                            dec = dict(dec)
                            dec["_write_port"] = int(port)
                            # Code 45: try next TCP port before giving up
                            msg_l = str(dec.get("Msg") or "").lower()
                            code_i = dec.get("Code")
                            if code_i == 45 or "can't access write" in msg_l or (
                                "cant access write" in msg_l
                            ):
                                last_err = RuntimeError(
                                    str(dec.get("Msg") or "can't access write cmd")
                                )
                                break  # next port
                        return dec
                    return raw
            except RuntimeError as e:
                last_err = e
                if "over max" not in str(e).lower():
                    # can't access write / other: try next port then fall through
                    if "can't access write" in str(e).lower() or "cant access write" in str(
                        e
                    ).lower():
                        break
                    # non-port-related auth errors should not spam all ports forever
                    if "get_token" in str(e).lower() and "over max" not in str(e).lower():
                        raise
                    break
                # Sleep outside lock — free live poll / Telegram
                if attempt + 1 < attempts:
                    time.sleep(min(45.0, 10.0 + attempt * 12.0))
                    continue
                # over max on this port — try next port without long wait
                break
    # all ports exhausted — return synthetic Code 45 so miner_write_cmd can LuCI-fallback
    if last_err and "over max" in str(last_err).lower():
        raise RuntimeError(_OVER_MAX_CONNECT_RU) from last_err
    if last_err and (
        "can't access write" in str(last_err).lower()
        or "cant access write" in str(last_err).lower()
    ):
        return {
            "STATUS": "E",
            "Code": 45,
            "Msg": "can't access write cmd",
            "Description": str(last_err),
        }
    if last_err:
        raise last_err if isinstance(last_err, Exception) else RuntimeError(str(last_err))
    raise RuntimeError(_OVER_MAX_CONNECT_RU)


# ─── Whatsminer driver (LuCI-first, btccom/libbtctools parity) ───────────────
# Control with API off: HTTPS LuCI (pools / power mode / restart / reboot).
# TCP :4028 / :4433 only when needed (limit, suspend, factory) — no get_token spam.
# See ui-demo/whatsminer_driver.py and github.com/btccom/libbtctools
#   src/lua/scripts/{configurator,rebooter}/WhatsMinerHttpsLuci.lua

try:
    from whatsminer_driver import LuciClient, WhatsminerDriver  # type: ignore
except ImportError:
    # Entware layout: same dir as serve.py
    import sys as _sys

    _lib = Path(__file__).resolve().parent
    if str(_lib) not in _sys.path:
        _sys.path.insert(0, str(_lib))
    from whatsminer_driver import LuciClient, WhatsminerDriver  # type: ignore

_wm_driver_lock = threading.Lock()
_wm_driver: "WhatsminerDriver | None" = None
_wm_driver_pw_fp: str = ""


def get_whatsminer_driver(password: str | None = None) -> "WhatsminerDriver":
    """
    Shared WhatsminerDriver for poolheat writes.
    LuCI first for mode/pools/reboot/restart — never burns get_token slots.
    """
    global _wm_driver, _wm_driver_pw_fp
    pw = (password or DEFAULT_API_PASSWORD or "admin").strip() or "admin"
    fp = _password_fingerprint(pw)

    def _tcp_write(cmd: dict, p: str) -> dict:
        # Few token attempts: each get_token burns a 30‑min slot (max ~100).
        return privileged_cmd(cmd, p, token_attempts=1)

    def _v3_write(cmd: dict, p: str) -> dict:
        return v3_write_legacy(cmd, p)

    with _wm_driver_lock:
        if (
            _wm_driver is not None
            and _wm_driver_pw_fp == fp
            and str(_wm_driver.host) == str(HOST_MINER)
        ):
            _wm_driver.api_password = pw
            _wm_driver.luci_password = pw
            _wm_driver.luci.password = pw
            return _wm_driver
        d = WhatsminerDriver(
            HOST_MINER,
            api_password=pw,
            luci_username="admin",
            luci_password=pw,
            port_v2=int(PORT_MINER) if PORT_MINER else 4028,
            port_v3=4433,
            tcp_write=_tcp_write,
            v3_write=_v3_write,
            is_online=lambda: miner_is_online(
                max_age_sec=_MINER_ONLINE_MAX_AGE_SEC, probe=True
            ),
        )
        _wm_driver = d
        _wm_driver_pw_fp = fp
        return d


def _luci_clear_session() -> None:
    with _wm_driver_lock:
        if _wm_driver is not None:
            try:
                _wm_driver.luci.clear()
            except Exception:
                pass


def _luci_request(
    path: str,
    password: str,
    *,
    data: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Compat for chipmap / enable-write helpers — routes through LuciClient."""
    d = get_whatsminer_driver(password)
    # LuciClient.request timeout is per-call
    old_to = d.luci.timeout
    try:
        if timeout and timeout != old_to:
            d.luci.timeout = float(timeout)
        return d.luci.request(path, data=data, timeout=timeout)
    finally:
        d.luci.timeout = old_to


def _luci_extract_token(html: str) -> str:
    return LuciClient.extract_token(html)


def luci_set_power_mode(mode: str, password: str) -> dict:
    """Power Mode via LuCI (btccom path) — no TCP write unlock."""
    return get_whatsminer_driver(password).set_power_mode(mode)


def luci_reboot_asic(password: str) -> dict:
    """Full reboot via LuCI /system/reboot/call (libbtctools)."""
    return get_whatsminer_driver(password).reboot()


def luci_restart_btminer(password: str) -> dict:
    """Restart mining process via LuCI status/{prog}status/restart."""
    return get_whatsminer_driver(password).restart_btminer()


def luci_set_pools(pools: list, password: str) -> dict:
    """Set up to 3 pools via LuCI (btccom setMinerConf)."""
    return get_whatsminer_driver(password).set_pools(pools)


def luci_enable_api_switch(password: str, *, enable: bool = True) -> dict:
    """
    Enable Miner API Switch (open_by_api / apiswitch) via LuCI form POST.
    Used only when TCP privileged write is required (suspend, power_limit, …).
    """
    password = password or DEFAULT_API_PASSWORD
    out = get_whatsminer_driver(password).enable_api_switch(enable)
    _token_cache_clear()
    return out


def _is_write_access_denied(resp_or_err) -> bool:
    s = ""
    if isinstance(resp_or_err, dict):
        s = str(resp_or_err.get("Msg") or resp_or_err.get("msg") or "")
        try:
            if int(resp_or_err.get("Code") or 0) == 45:
                return True
        except Exception:
            pass
    else:
        s = str(resp_or_err or "")
    low = s.lower()
    return "can't access write" in low or "cant access write" in low


# ─── Whatsminer API v3 (TCP :4433, length-prefixed JSON) ─────────────────────
# Present on M63 fw 2025+ as «API-v2 3.0.2». Field system.apiswitch (0/1) is the
# same write gate as Tools «Miner API Switch». Write cmds need apiswitch=1.
PORT_MINER_V3 = 4433


def _v3_send(payload: dict | str, *, timeout: float = 8.0) -> dict:
    """Send one API v3 message: 4-byte LE length + JSON body."""
    if isinstance(payload, dict):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    else:
        raw = str(payload).encode("utf-8")
    with socket.create_connection((HOST_MINER, PORT_MINER_V3), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack("<I", len(raw)) + raw)
        hdr = b""
        while len(hdr) < 4:
            ch = sock.recv(4 - len(hdr))
            if not ch:
                raise TimeoutError("API v3: empty length header")
            hdr += ch
        n = struct.unpack("<I", hdr)[0]
        if n <= 0 or n > 2_000_000:
            raise RuntimeError(f"API v3: bad length {n}")
        buf = b""
        while len(buf) < n:
            ch = sock.recv(min(65536, n - len(buf)))
            if not ch:
                break
            buf += ch
    return json.loads(buf.decode("utf-8", "replace"))


def _v3_token(cmd: str, password: str, salt: str, ts: int) -> str:
    src = f"{cmd}{password}{salt}{ts}"
    digest = hashlib.sha256(src.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")[:8]


def _v3_encrypt_param(param: str, cmd: str, password: str, salt: str, ts: int) -> str:
    src = f"{cmd}{password}{salt}{ts}"
    aes_key = hashlib.sha256(src.encode("utf-8")).digest()
    pad = 16 - (len(param) % 16)
    padded = param + (chr(pad) * pad)
    cipher = AES.new(aes_key, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(padded.encode("utf-8"))).decode("ascii")


def v3_get_device_info() -> dict:
    """Unauthenticated get.device.info (includes salt + system.apiswitch)."""
    return _v3_send({"cmd": "get.device.info", "param": None})


def v3_api_switch_on() -> bool | None:
    """True if Miner API Switch enabled (write allowed). None if probe fail."""
    try:
        info = v3_get_device_info()
        msg = info.get("msg") if isinstance(info, dict) else None
        if not isinstance(msg, dict):
            return None
        sys_ = msg.get("system") if isinstance(msg.get("system"), dict) else {}
        sw = str(sys_.get("apiswitch") or "0").strip()
        return sw in ("1", "true", "on", "enable", "enabled")
    except Exception:
        return None


def v3_call(
    cmd: str,
    param=None,
    *,
    password: str | None = None,
    account: str = "super",
    encrypt_param: bool = False,
) -> dict:
    """Authenticated API v3 call. Tries account super then admin with password."""
    info = v3_get_device_info()
    # note: code 0 is success — do not use `or -1` (0 is falsy)
    try:
        info_code = int(info.get("code")) if isinstance(info, dict) and info.get("code") is not None else -1
    except (TypeError, ValueError):
        info_code = -1
    if not isinstance(info, dict) or info_code != 0:
        raise RuntimeError(f"API v3 get.device.info fail: {info}")
    msg = info.get("msg") or {}
    salt = str(msg.get("salt") or "")
    if not salt:
        raise RuntimeError("API v3: no salt in get.device.info")
    pw_candidates = []
    p0 = (password or DEFAULT_API_PASSWORD or "admin").strip() or "admin"
    for p in (p0, "super", "admin"):
        if p not in pw_candidates:
            pw_candidates.append(p)
    acc_candidates = []
    for a in (account, "super", "admin"):
        if a not in acc_candidates:
            acc_candidates.append(a)
    last: dict | None = None
    for acc in acc_candidates:
        for pw in pw_candidates:
            ts = int(time.time())
            tok = _v3_token(cmd, pw, salt, ts)
            payload: dict = {
                "cmd": cmd,
                "ts": ts,
                "token": tok,
                "account": acc,
            }
            if encrypt_param and param is not None:
                pstr = param if isinstance(param, str) else json.dumps(param)
                payload["param"] = _v3_encrypt_param(pstr, cmd, pw, salt, ts)
            else:
                payload["param"] = param
            try:
                last = _v3_send(payload)
            except Exception as e:
                last = {"code": -99, "msg": str(e)}
                continue
            code = last.get("code")
            # 0 = ok; -4 = no write permission (auth may still be ok)
            # -3 / token errors → try next credential
            if code == 0:
                last["_v3_account"] = acc
                return last
            msg_s = str(last.get("msg") or "").lower()
            if code == -4 or "no permission" in msg_s or "write command" in msg_s:
                last["_v3_account"] = acc
                return last  # switch off / no write — credentials accepted
            if "token" in msg_s or "auth" in msg_s or "password" in msg_s:
                continue
            # invalid command/param — stop credential churn
            if code in (-1, -2):
                return last
    return last or {"code": -99, "msg": "API v3 call failed"}


def _v3_cmd_for_legacy(legacy_cmd: str, cmd: dict) -> tuple[str, object, bool] | None:
    """
    Map classic 4028 privileged cmd → API v3 (cmd, param, encrypt).
    Returns None if no mapping.
    """
    c = str(legacy_cmd or "").strip()
    if c == "set_low_power":
        return "set.miner.power_mode", "low", False
    if c == "set_normal_power":
        return "set.miner.power_mode", "normal", False
    if c == "set_high_power":
        return "set.miner.power_mode", "high", False
    if c == "power_off":
        # stop hashing / suspend equivalent on v3
        return "set.miner.service", "stop", False
    if c == "power_on":
        return "set.miner.service", "start", False
    if c == "reboot":
        return "set.system.reboot", None, False
    if c == "restart_btminer":
        return "set.miner.service", "restart", False
    if c == "adjust_power_limit":
        lim = cmd.get("power_limit")
        return "set.miner.power_limit", lim, False
    if c == "set_power_pct":
        pct = cmd.get("percent")
        return (
            "set.miner.power_percent",
            json.dumps({"percent": str(pct), "mode": "temp"}),
            False,
        )
    if c == "update_pwd":
        return (
            "set.user.change_passwd",
            {
                "account": "admin",
                "old": str(cmd.get("old") or ""),
                "new": str(cmd.get("new") or ""),
            },
            True,
        )
    return None


def v3_write_legacy(cmd: dict, password: str) -> dict:
    """Execute legacy privileged cmd via API v3; raise on failure."""
    cname = str(cmd.get("cmd") or "")
    mapped = _v3_cmd_for_legacy(cname, cmd)
    if not mapped:
        raise RuntimeError(f"API v3: no mapping for «{cname}»")
    v3cmd, param, enc = mapped
    # power_off sometimes exposed as service power_off on some firmwares
    attempts = [(v3cmd, param, enc)]
    if cname == "power_off":
        attempts.extend(
            [
                ("set.miner.service", "power_off", False),
                ("set.miner.service", "suspend", False),
            ]
        )
    if cname == "power_on":
        attempts.extend(
            [
                ("set.miner.service", "power_on", False),
                ("set.miner.service", "resume", False),
            ]
        )
    last_err = "API v3 write fail"
    for vc, p, e in attempts:
        r = v3_call(vc, p, password=password, encrypt_param=e)
        code = r.get("code")
        if code == 0:
            return {
                "STATUS": "S",
                "Code": 131,
                "Msg": r.get("msg") if r.get("msg") is not None else "ok",
                "transport": "api_v3",
                "v3_cmd": vc,
                "response": r,
            }
        last_err = str(r.get("msg") or f"code={code}")
        if code == -2:  # invalid command — try next alias
            continue
        if code == -4 or "no permission" in last_err.lower():
            raise RuntimeError(
                f"can't access write cmd · API v3 apiswitch off ({last_err})"
            )
        # other errors: try next alias only for service variants
        if cname not in ("power_off", "power_on"):
            break
    raise RuntimeError(f"API v3: {last_err}")


def get_write_api_status(password: str | None = None) -> dict:
    """
    Probe write capability: API v3 apiswitch + 4028 privileged test (set_led).
    Does not change miner state when possible (set_led auto is safe).
    """
    password = password or DEFAULT_API_PASSWORD
    out: dict = {
        "ok": True,
        "host": HOST_MINER,
        "port_v2": int(PORT_MINER),
        "port_v3": PORT_MINER_V3,
        "apiswitch": None,
        "apiswitch_on": None,
        "v3_ok": False,
        "v2_write_ok": None,
        "v3_write_ok": None,
        "luci_ok": None,
        "write_ok": False,
        "hint": "",
        "fw": None,
        "api_ver_v3": None,
    }
    # v3 info
    try:
        info = v3_get_device_info()
        try:
            out["v3_ok"] = (
                isinstance(info, dict)
                and info.get("code") is not None
                and int(info.get("code")) == 0
            )
        except (TypeError, ValueError):
            out["v3_ok"] = False
        msg = info.get("msg") if isinstance(info, dict) else {}
        if isinstance(msg, dict):
            sys_ = msg.get("system") if isinstance(msg.get("system"), dict) else {}
            out["apiswitch"] = str(sys_.get("apiswitch") or "")
            out["apiswitch_on"] = out["apiswitch"] in ("1", "true", "on")
            out["api_ver_v3"] = sys_.get("api")
            out["fw"] = sys_.get("fwversion")
    except Exception as e:
        out["v3_error"] = str(e)

    # v2 write probe (set_led auto)
    try:
        r = privileged_cmd({"cmd": "set_led", "param": "auto"}, password, token_attempts=1)
        ok, msg = _miner_cmd_result(r)
        out["v2_write_ok"] = bool(ok)
        if not ok:
            out["v2_write_error"] = msg
    except Exception as e:
        out["v2_write_ok"] = False
        out["v2_write_error"] = str(e)

    # v3 write probe when switch on
    if out.get("apiswitch_on"):
        try:
            r3 = v3_call(
                "set.miner.power_mode",
                "normal",
                password=password,
            )
            try:
                out["v3_write_ok"] = (
                    r3.get("code") is not None and int(r3.get("code")) == 0
                )
            except (TypeError, ValueError):
                out["v3_write_ok"] = False
            if not out["v3_write_ok"]:
                out["v3_write_error"] = r3.get("msg")
        except Exception as e:
            out["v3_write_ok"] = False
            out["v3_write_error"] = str(e)
    else:
        out["v3_write_ok"] = False

    # LuCI reachability (mode/pools/reboot work without TCP write unlock)
    try:
        get_whatsminer_driver(password).luci.login()
        out["luci_ok"] = True
    except Exception as e:
        out["luci_ok"] = False
        out["luci_error"] = str(e)

    out["write_ok"] = bool(
        out.get("v2_write_ok") or out.get("v3_write_ok") or out.get("luci_ok")
    )
    if out.get("v2_write_ok") or out.get("v3_write_ok"):
        out["hint"] = "Write API доступен (TCP)"
    elif out.get("luci_ok"):
        out["hint"] = (
            "LuCI OK · mode/pools/reboot без API Switch; "
            "suspend/limit/factory — TCP (apiswitch) или Tools :8889"
        )
    elif out.get("apiswitch_on") is False:
        out["hint"] = (
            "apiswitch=0 · LuCI offline? · Tools Miner API Switch → Enable"
        )
    elif out.get("v2_write_error") and "over max" in str(out.get("v2_write_error")).lower():
        out["hint"] = "Лимит get_token (over max connect) · ждите ~30 мин или reboot ASIC"
    else:
        out["hint"] = (
            "Write закрыт · WhatsMinerTool: Password + Miner API Switch → Enable"
        )
    return out


def enable_write_api(
    password: str | None = None,
    *,
    new_password: str | None = None,
) -> dict:
    """
    Enable Write API the way Tools does «Miner API Switch», but via LuCI UCI:

      1) Probe
      2) LuCI POST open_by_api=1 (hidden cbi on Power page) — primary unlock
      3) Optional password cycle (new_password)
      4) Verify with set_led / apiswitch

    Live M63: after open_by_api=1, power_off/on, mode, adjust_power_limit work
    on :4028 without WhatsMinerTool.
    """
    password = (password or DEFAULT_API_PASSWORD or "admin").strip() or "admin"
    new_pw = (new_password or password).strip() or password
    steps: list[dict] = []
    before = get_write_api_status(password)
    steps.append({
        "step": "probe_before",
        "ok": True,
        "detail": {
            "apiswitch": before.get("apiswitch"),
            "write_ok": before.get("write_ok"),
            "v2_write_ok": before.get("v2_write_ok"),
            "v3_write_ok": before.get("v3_write_ok"),
        },
    })

    if before.get("write_ok"):
        return {
            "ok": True,
            "enabled": True,
            "already": True,
            "message": "Write API уже доступен (mining control / mode / limit)",
            "status": before,
            "steps": steps,
        }

    # A) Primary unlock — LuCI open_by_api (Tools Remote Ctrl equivalent)
    try:
        r = luci_enable_api_switch(password, enable=True)
        steps.append({
            "step": "luci_open_by_api",
            "ok": True,
            "detail": r.get("Msg"),
        })
    except Exception as e:
        steps.append({"step": "luci_open_by_api", "ok": False, "detail": str(e)})

    # B) Optional password set via LuCI (if caller asked for new password)
    if new_pw != password:
        try:
            path = "/cgi-bin/luci/admin/system/admin"
            st, html = _luci_request(path, password)
            tok = _luci_extract_token(html)
            st2, _ = _luci_request(
                path,
                password,
                data={
                    "token": tok,
                    "cbi.submit": "1",
                    "cbid.system._pass.pw1": new_pw,
                    "cbid.system._pass.pw2": new_pw,
                    "cbi.apply": "1",
                },
                timeout=20.0,
            )
            _luci_clear_session()
            apply_miner_settings(password=new_pw, persist=True)
            password = new_pw
            steps.append({
                "step": "luci_password",
                "ok": st2 in (200, 302, 500, 502),
                "detail": f"HTTP {st2}",
            })
        except Exception as e:
            steps.append({"step": "luci_password", "ok": False, "detail": str(e)})

    # C) Verify write (set_led is harmless)
    time.sleep(1.0)
    _token_cache_clear()
    write_probe_ok = False
    write_probe_detail = ""
    try:
        resp = privileged_cmd(
            {"cmd": "set_led", "param": "auto"}, password, token_attempts=1
        )
        ok, msg = _miner_cmd_result(resp)
        write_probe_ok = bool(ok)
        write_probe_detail = msg or str(resp.get("Msg") if isinstance(resp, dict) else resp)
    except Exception as e:
        write_probe_detail = str(e)
    steps.append({
        "step": "write_probe_set_led",
        "ok": write_probe_ok,
        "detail": write_probe_detail,
    })

    after = get_write_api_status(password)
    steps.append({
        "step": "probe_after",
        "ok": True,
        "detail": {
            "apiswitch": after.get("apiswitch"),
            "write_ok": after.get("write_ok"),
            "v2_write_ok": after.get("v2_write_ok"),
            "v3_write_ok": after.get("v3_write_ok"),
        },
    })

    enabled = bool(after.get("write_ok") or write_probe_ok)
    if enabled:
        msg = "Write API включён · Suspend/Resume, Power Mode, Power Limit доступны"
    else:
        msg = (
            "Не удалось открыть write через LuCI open_by_api. "
            "Проверьте web-пароль admin и доступ к https://miner/cgi-bin/luci. "
            "Запасной путь: WhatsMinerTool → Remote Ctrl → Miner API Switch → Enable."
        )
    return {
        "ok": True,
        "enabled": enabled,
        "already": False,
        "message": msg,
        "status": after,
        "steps": steps,
        "tools_steps": [
            "poolheat → Enable Write (LuCI open_by_api)",
            "или WhatsMinerTool → Remote Ctrl → Miner API Switch → Enable",
        ],
    }


def miner_write_cmd(cmd: dict, password: str) -> dict:
    """
    Unified write path via WhatsminerDriver (libbtctools-aligned):

      0) Refuse if ASIC offline
      A) LuCI FIRST for mode / reboot / restart_btminer / pools
         — no get_token, works with apiswitch=0 (btccom path)
      B) TCP v2 privileged (limit, suspend, factory, …) — 1 token attempt
      C) If write locked → auto LuCI open_by_api once, retry TCP
      D) API v3 :4433 when available
    Concurrent WMT + poolheat used to exhaust get_token («over max connect»);
    LuCI-first avoids that for everyday mode/reboot control.
    """
    password = password or DEFAULT_API_PASSWORD
    cname = str(cmd.get("cmd") or "").strip()

    # Offline short-circuit — no write, no auto-enable spam
    if not miner_is_online(max_age_sec=_MINER_ONLINE_MAX_AGE_SEC, probe=True):
        raise RuntimeError(
            "ASIC offline · write skipped (нет read — команда не отправлялась)"
        )

    d = get_whatsminer_driver(password)

    # ── A) LuCI-native ops first (never touch get_token) ───────────────────
    luci_first = {
        "set_low_power": lambda: d.set_power_mode("low"),
        "set_normal_power": lambda: d.set_power_mode("normal"),
        "set_high_power": lambda: d.set_power_mode("high"),
        "reboot": lambda: d.reboot(),
        "restart_btminer": lambda: d.restart_btminer(),
        "restart_cgminer": lambda: d.restart_btminer(),
    }
    if cname in luci_first:
        errors: list[str] = []
        try:
            return luci_first[cname]()
        except Exception as e:
            errors.append(f"luci: {e}")
        # rare TCP fallback if LuCI down but API open
        try:
            resp = privileged_cmd(cmd, password, token_attempts=1)
            ok, msg = _miner_cmd_result(resp)
            if ok:
                if isinstance(resp, dict):
                    resp = dict(resp)
                    resp.setdefault("transport", "api_v2")
                return (
                    resp
                    if isinstance(resp, dict)
                    else {"STATUS": "S", "Msg": str(resp), "transport": "api_v2"}
                )
            errors.append(f"v2: {msg or resp}")
        except Exception as e2:
            errors.append(f"v2: {e2}")
        raise RuntimeError(" · ".join(errors) if errors else f"write failed: {cname}")

    if cname in ("update_pools", "set_pools"):
        pools = cmd.get("pools")
        if not isinstance(pools, list):
            raise ValueError("update_pools requires pools=[{url,user,pass},…]")
        return d.set_pools(pools)

    # ── B/C/D) TCP / v3 path via driver.write_cmd ──────────────────────────
    try:
        return d.write_cmd(cmd)
    except Exception as e:
        # keep previous RU over-max message when applicable
        err = str(e)
        if "over max" in err.lower() and "лимит" not in err.lower():
            raise RuntimeError(_OVER_MAX_CONNECT_RU) from e
        raise

# Human tooltips (hover) for ErrorCode — not shown as Cause
_MINER_ERROR_HINTS: dict[str, str] = {
    "2320": "Низкий хешрейт vs expected (LOW_HASH)",
    "2000": "Питание / PSU",
    "2010": "Пулы отключены / сеть",
    "2020": "Pool connect failed",
    "2030": "Hashboard / pool reject",
    "2100": "Сеть / пул",
    "2200": "Частота / чипы",
    "2300": "Hashrate",
    "2310": "Hashrate",
    "540": "SM0 chip id",
    "541": "SM1 chip id",
    "542": "SM2 chip id",
    "543": "SM3 chip id",
}

# Cause text as on Whatsminer web UI / official Error Code list (Reason column).
# API get_error_code usually returns only code+time; web builds Cause from this map.
_MINER_ERROR_CAUSES: dict[str, str] = {
    # fans
    "110": "Fanin detect speed error",
    "111": "Fanout detect speed error",
    "130": "Fanin speed error",
    "131": "Fanout speed error",
    "140": "Fan speed is too high",
    # power
    "200": "Power probing error, no power found",
    "201": "Power supply and configuration file mismatch",
    "203": "Power protecting",
    "204": "Power current protecting",
    "205": "Power current error",
    "206": "Power input voltage is low",
    "207": "Power input current protecting",
    "210": "Power error status",
    "213": "Power input voltage and current do not match the power",
    "233": "Power output over temperature protection",
    "234": "Power output over temperature protection",
    "235": "Power output over temperature protection",
    "236": "Overcurrent Protection of Power Output",
    "237": "Overcurrent Protection of Power Output",
    "238": "Overcurrent Protection of Power Output",
    "239": "Overvoltage Protection of Power Output",
    "240": "Low Voltage Protection for Power Output",
    "241": "Power output current imbalance",
    "243": "Over-temperature Protection for Power Input",
    "244": "Over-temperature Protection for Power Input",
    "245": "Over-temperature Protection for Power Input",
    "246": "Overcurrent Protection for Power Input",
    "247": "Overcurrent Protection for Power Input",
    "248": "Overvoltage Protection for Power Input",
    "249": "Overvoltage Protection for Power Input",
    "250": "Undervoltage Protection for Power Input",
    "251": "Undervoltage Protection for Power Input",
    "253": "Power Fan Error",
    "254": "Power Fan Error",
    "255": "Protection of over power output",
    "256": "Protection of over power output",
    "257": "Input over current protection of power supply primary side",
    "263": "Power communication warning",
    "264": "Power communication error",
    "267": "Power watchdog protection",
    "268": "Power output over-current protection",
    "269": "Power input over-current protection",
    "270": "Power input over-voltage protection",
    "271": "Power input under-voltage protection",
    "272": "Warning of excessive power output of power supply",
    "273": "Power input power too high warning",
    "274": "Power fan warning",
    "275": "Power over temperature warning",
    # temp / boards
    "300": "SM0 temperature sensor detection error",
    "301": "SM1 temperature sensor detection error",
    "302": "SM2 temperature sensor detection error",
    "303": "SM3 temperature sensor detection error",
    "320": "SM0 temperature reading error",
    "321": "SM1 temperature reading error",
    "322": "SM2 temperature reading error",
    "323": "SM3 temperature reading error",
    "329": "Control board temperature sensor communication error",
    "350": "SM0 temperature protecting",
    "351": "SM1 temperature protecting",
    "352": "SM2 temperature protecting",
    "353": "SM3 temperature protecting",
    "410": "SM0 detect eeprom error",
    "411": "SM1 detect eeprom error",
    "412": "SM2 detect eeprom error",
    "413": "SM3 detect eeprom error",
    "420": "SM0 parser eeprom error",
    "421": "SM1 parser eeprom error",
    "422": "SM2 parser eeprom error",
    "430": "SM0 chip bin type error",
    "431": "SM1 chip bin type error",
    "432": "SM2 chip bin type error",
    "440": "SM0 eeprom chip num X error",
    "441": "SM1 eeprom chip num X error",
    "442": "SM2 eeprom chip num X error",
    "510": "SM0 miner type error",
    "511": "SM1 miner type error",
    "512": "SM2 miner type error",
    "530": "SM0 not found",
    "531": "SM1 not found",
    "532": "SM2 not found",
    "533": "SM3 not found",
    # chip id — exact web Cause strings
    "540": "SM0 reading chip id error",
    "541": "SM1 reading chip id error",
    "542": "SM2 reading chip id error",
    "543": "SM3 reading chip id error",
    "550": "SM0 have bad chips",
    "551": "SM1 have bad chips",
    "552": "SM2 have bad chips",
    "553": "SM3 have bad chips",
    "560": "SM0 loss balance",
    "561": "SM1 loss balance",
    "562": "SM2 loss balance",
    "600": "Environment temperature is high",
    "610": "If the ambient temperature is too high in high performance mode, return to normal mode",
    "620": "Liquid cooling liquid temperature protection",
    "701": "Control board no support chip",
    "702": "Control board version unknown",
    "710": "Control board rebooted as exception",
    "712": "Control board rebooted as exception",
    "714": "The network connection is seriously unstable",
    "800": "cgminer checksum error",
    "801": "system-monitor checksum error",
    "802": "remote-daemon checksum error",
    # pools / network — web Cause
    "2010": "All pools are disable",
    "2020": "Pool0 connect failed",
    "2021": "Pool1 connect failed",
    "2022": "Pool2 connect failed",
    "2030": "High rejection rate of pool",
    "2040": "The pool does not support the asicboost mode",
    "5110": "SM0 Frequency Up Timeout",
    "5111": "SM1 Frequency Up Timeout",
    "5112": "SM2 Frequency Up Timeout",
    "8410": "Software version error (M2x miner with M3x firmware, or M3x with M2x firmware).",
    # hashrate — base string; 2320 may be expanded with live numbers
    "2320": "Hashrate too low",
}

# Typical J/TH when Factory GHS unavailable (~2990 W / ~172.7 TH on this M63)
_DEFAULT_J_PER_TH = 17.31


def _official_cause(code: str) -> str | None:
    """Whatsminer web UI Cause (Reason) for error code, if known."""
    c = str(code).strip()
    if c in _MINER_ERROR_CAUSES:
        return _MINER_ERROR_CAUSES[c]
    # some FW report with leading zeros
    c2 = c.lstrip("0") or "0"
    if c2 in _MINER_ERROR_CAUSES:
        return _MINER_ERROR_CAUSES[c2]
    return None


def _parse_error_code_item(item) -> list[dict]:
    """
    Normalize one entry from get_error_code Msg.error_code.
    Supports:
      {"2320": "2026-08-03 01:26:57"}
      {"error_code":"2320","time":"...","cause":"...","error_message":"..."}
      {"Code":2320,"Cause":"Hashrate too low, ...","Time":"..."}
    """
    if not isinstance(item, dict):
        return []
    # structured form with explicit cause
    code = (
        item.get("error_code")
        or item.get("ErrorCode")
        or item.get("code")
        or item.get("Code")
    )
    cause = (
        item.get("cause")
        or item.get("Cause")
        or item.get("error_message")
        or item.get("ErrorMessage")
        or item.get("msg")
        or item.get("Msg")
        or item.get("message")
        or item.get("Description")
    )
    ts = (
        item.get("time")
        or item.get("Time")
        or item.get("timestamp")
        or item.get("When")
        or item.get("ts")
    )
    out: list[dict] = []
    if code is not None and str(code).strip().isdigit():
        out.append(
            {
                "code": str(code).strip(),
                "ts": str(ts) if ts is not None else None,
                "cause": str(cause).strip() if cause not in (None, "") else None,
            }
        )
        return out
    # classic map form: { "2320": "2026-08-03 01:26:57" }
    for k, v in item.items():
        ks = str(k).strip()
        if not ks or not ks.replace(".", "").isdigit():
            continue
        # value may be time string or nested dict
        if isinstance(v, dict):
            c2 = (
                v.get("cause")
                or v.get("Cause")
                or v.get("error_message")
                or v.get("msg")
            )
            t2 = v.get("time") or v.get("Time") or v.get("timestamp")
            out.append(
                {
                    "code": ks,
                    "ts": str(t2) if t2 is not None else None,
                    "cause": str(c2).strip() if c2 not in (None, "") else None,
                }
            )
        else:
            out.append(
                {
                    "code": ks,
                    "ts": str(v) if v is not None else None,
                    "cause": None,
                }
            )
    return out


def _fetch_miner_errors_raw() -> list[dict]:
    """get_error_code → list of {code, ts, cause?} (cause if firmware provides it)."""
    out: list[dict] = []
    try:
        raw = miner_cmd({"cmd": "get_error_code"}, timeout=4)
    except Exception:
        return out
    msg = raw.get("Msg") if isinstance(raw, dict) else None
    codes = None
    if isinstance(msg, dict):
        codes = msg.get("error_code")
    elif isinstance(msg, list):
        codes = msg
    if not isinstance(codes, list):
        return out
    for item in codes:
        out.extend(_parse_error_code_item(item))
    return out


def _snapshot_cause_2320(
    *,
    hashrate_th: float | None,
    power_limit: float | None,
    factory_ghs: float | None,
) -> str:
    """
    Whatsminer web Cause for 2320 (snapshot at first detection).
    API usually only gives code+time; stock UI builds this string once.
    """
    cur = hashrate_th
    exp: float | None = None
    if factory_ghs is not None and factory_ghs > 0:
        exp = float(factory_ghs) / 1000.0
    elif power_limit is not None and power_limit > 0:
        exp = float(power_limit) / _DEFAULT_J_PER_TH
    if cur is not None and exp is not None and exp > 0:
        pct = 100.0 * float(cur) / exp
        return (
            f"Hashrate too low, current: {float(cur):.2f}T, "
            f"expected: {exp:.2f}T, percent: {pct:.2f}%"
        )
    if cur is not None:
        return f"Hashrate too low, current: {float(cur):.2f}T"
    return "Hashrate too low"


def _resolve_miner_errors(
    raw_errors: list[dict],
    *,
    hashrate_th: float | None,
    power_limit: float | None,
    factory_ghs: float | None,
) -> list[dict]:
    """
    Active firmware errors for UI table: ErrorCode | Cause | Time.
    Cause is frozen on first sight (cache) so live hashrate does not rewrite it.
    """
    with _state_lock:
        cache = dict(_state.get("miner_error_cache") or {})

    active: list[dict] = []
    active_keys: set[str] = set()

    for e in raw_errors:
        code = str(e.get("code") or "").strip()
        if not code:
            continue
        ts = e.get("ts")
        key = f"{code}@{ts or ''}"
        active_keys.add(key)

        # prefer native Cause from miner API if present
        cause = e.get("cause")
        # ignore placeholder "Error code NNN" (our old fallback / empty FW)
        if cause and str(cause).strip().lower() in (
            f"error code {code}".lower(),
            f"error {code}".lower(),
        ):
            cause = None

        hint = (
            _MINER_ERROR_HINTS.get(code)
            or _MINER_ERROR_HINTS.get(code[:3] + "0")
            or f"Error {code}"
        )

        if key in cache and cache[key].get("cause"):
            cached = cache[key]["cause"]
            # refresh if cache still holds useless placeholder
            if cached and str(cached).strip().lower() not in (
                f"error code {code}".lower(),
                f"error {code}".lower(),
            ):
                cause = cached
            elif not cause:
                cause = None

        if not cause:
            if code == "2320" or code.startswith("232"):
                cause = _snapshot_cause_2320(
                    hashrate_th=hashrate_th,
                    power_limit=power_limit,
                    factory_ghs=factory_ghs,
                )
            else:
                # match Whatsminer web Cause column (official Reason text)
                cause = _official_cause(code) or f"Error code {code}"

        cache[key] = {"code": code, "ts": ts, "cause": cause, "hint": hint}
        active.append(
            {
                "code": code,
                "cause": cause,
                "ts": ts,
                "hint": hint,
                # aliases for older UI
                "message": cause,
            }
        )

    # journal: log active errors (dedupe by code+miner_ts in DB)
    try:
        log_miner_errors(active, miner_ctx=get_miner_identity_cached())
    except Exception as e:
        print(f"[errors] log failed: {e}")

    # drop cleared errors from cache
    cache = {k: v for k, v in cache.items() if k in active_keys}

    with _state_lock:
        _state["miner_error_cache"] = cache
        _save_state()

    return active


def _extract_liquid_temp(
    status: dict | None,
    summary: dict | None,
    psu: dict | None = None,
    *,
    v3_msg: dict | None = None,
) -> float | None:
    """
    Liquid / coolant temperature across firmware families.
    Classic air API: often missing. Liquid M63+: API v3 power.liquid-temperature
    (same field WhatsMinerTool shows as Liquid Temp). Also try status/summary/psu.
    """
    status = status if isinstance(status, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    psu = psu if isinstance(psu, dict) else {}
    candidates = [
        status.get("liquid_temp"),
        status.get("Liquid Temp"),
        status.get("liquid_temperature"),
        status.get("coolant_temp"),
        summary.get("Liquid Temp"),
        summary.get("liquid_temp"),
        summary.get("Coolant Temp"),
        psu.get("liquid_temp"),
        psu.get("liquid-temperature"),
        psu.get("temp_liquid"),
    ]
    if isinstance(v3_msg, dict):
        power = v3_msg.get("power") if isinstance(v3_msg.get("power"), dict) else {}
        candidates.extend(
            [
                power.get("liquid-temperature"),
                power.get("liquid_temperature"),
                power.get("liquid_temp"),
                power.get("coolant"),
                v3_msg.get("liquid_temp"),
                v3_msg.get("liquid-temperature"),
            ]
        )
    for c in candidates:
        v = _f(c)
        if v is not None:
            return v
    return None


_v3_device_msg_cache: dict | None = None
_v3_device_msg_ts = 0.0
_V3_DEVICE_MSG_TTL_SEC = 30.0


def _fetch_v3_device_msg(*, force: bool = False) -> dict | None:
    """Best-effort API v3 get.device.info msg (for liquid temp / model). Cached ~30s."""
    global _v3_device_msg_cache, _v3_device_msg_ts
    now = time.time()
    if (
        not force
        and isinstance(_v3_device_msg_cache, dict)
        and (now - float(_v3_device_msg_ts or 0)) < _V3_DEVICE_MSG_TTL_SEC
    ):
        return _v3_device_msg_cache
    try:
        info = v3_get_device_info()
        if isinstance(info, dict) and info.get("code") in (0, "0"):
            msg = info.get("msg")
            if isinstance(msg, dict):
                _v3_device_msg_cache = msg
                _v3_device_msg_ts = now
                return msg
    except Exception as e:
        print(f"[v3] get.device.info: {e}")
    return _v3_device_msg_cache if isinstance(_v3_device_msg_cache, dict) else None


def _normalize_psu_vin(raw) -> float | None:
    """
    PowerVin (input voltage, V).
    get_psu: often ×100 (e.g. 39200 → 392.0 V); API v3: already volts (392).
    """
    v = _f(raw)
    if v is None or v < 0:
        return None
    if v > 1000:
        # centivolts / 0.01 V units
        return round(v / 100.0, 2)
    return round(v, 2)


def _normalize_psu_iin(raw) -> float | None:
    """
    PowerIin (input current, A).
    get_psu: milliamps as integer string (\"96\", \"12515\") → A.
    API v3: already amps as float (0.09, 12.48) — keep as-is.
    """
    i = _f(raw)
    if i is None or i < 0:
        return None
    s = str(raw).strip().replace(",", ".")
    # integer / no decimal point → get_psu mA
    if s and "." not in s and i >= 1:
        return round(i / 1000.0, 3)
    return round(i, 3)


def fetch_live() -> dict:
    summary = miner_cmd({"cmd": "summary"})["Msg"]
    if not isinstance(summary, dict):
        summary = {}
    status = miner_cmd({"cmd": "status"})["Msg"]
    if not isinstance(status, dict):
        status = {}
    # devs may return error object while miner is suspended — non-fatal
    devs: list = []
    try:
        devs_raw = miner_cmd({"cmd": "devs"})
        if isinstance(devs_raw, dict):
            d = devs_raw.get("DEVS")
            if isinstance(d, list):
                devs = d
    except Exception as e:
        print(f"[live] devs: {e}")
    raw_errors = _fetch_miner_errors_raw()

    # PSU: temp0 (°C), fan_speed (rpm), Vin/Iin/Pin — never fail whole live poll
    psu_temp = None
    psu_fan = None
    psu_pin = None
    psu_vin = None  # PowerVin (input V) — Tools name
    psu_iin = None  # PowerIin (input A)
    psu_model = None
    psu: dict = {}
    try:
        psu_raw = miner_cmd({"cmd": "get_psu"}, timeout=3).get("Msg") or {}
        if isinstance(psu_raw, dict):
            psu = psu_raw
            psu_temp = _f(psu.get("temp0"))
            psu_fan = _f(psu.get("fan_speed"))
            psu_pin = _f(psu.get("pin"))  # often watts as string
            psu_vin = _normalize_psu_vin(psu.get("vin") or psu.get("Vin"))
            psu_iin = _normalize_psu_iin(psu.get("iin") or psu.get("Iin"))
            psu_model = psu.get("model") or psu.get("name")
    except Exception as e:
        print(f"[psu] get_psu failed: {e}")

    # Liquid temp: status.liquid_temp missing on many liquid FW → API v3
    v3_msg = None
    liquid = _extract_liquid_temp(status, summary, psu)
    if liquid is None:
        v3_msg = _fetch_v3_device_msg()
        liquid = _extract_liquid_temp(status, summary, psu, v3_msg=v3_msg)

    # Fill Vin/Iin from v3 power{} if get_psu missing / odd units
    if psu_vin is None or psu_iin is None:
        try:
            if v3_msg is None:
                v3_msg = _fetch_v3_device_msg()
            pwr = (
                v3_msg.get("power")
                if isinstance(v3_msg, dict) and isinstance(v3_msg.get("power"), dict)
                else None
            )
            if isinstance(pwr, dict):
                if psu_vin is None:
                    psu_vin = _normalize_psu_vin(pwr.get("vin") or pwr.get("Vin"))
                if psu_iin is None:
                    psu_iin = _normalize_psu_iin(pwr.get("iin") or pwr.get("Iin"))
                if psu_pin is None:
                    psu_pin = _f(pwr.get("pin") or pwr.get("Pin"))
                if psu_temp is None:
                    psu_temp = _f(pwr.get("temp0"))
                if psu_fan is None:
                    psu_fan = _f(pwr.get("fanspeed") or pwr.get("fan_speed"))
        except Exception:
            pass

    # Hashboard count: live DEVS / v3 board-num / model map (M63=4, M60S=3, …)
    miner_type_s: str | None = None
    board_num_hint: int | None = None
    try:
        ident = get_miner_identity_cached(force=False)
        if isinstance(ident, dict):
            miner_type_s = (
                str(ident.get("miner_type") or "").strip() or None
            )
            try:
                bn = int(ident.get("board_num") or 0)
                if bn > 0:
                    board_num_hint = bn
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    hb_layout = resolve_hashboard_layout(
        miner_type_s,
        n_devs=len(devs) if isinstance(devs, list) else 0,
        board_num=board_num_hint,
    )
    n_boards = int(hb_layout.get("boards") or 4)

    boards: list[float | None] = []
    upfreq: list[int] = []
    factory_parts: list[float] = []
    for i in range(n_boards):
        if i < len(devs) and isinstance(devs[i], dict):
            try:
                t = float(devs[i].get("Temperature", 0) or 0)
                # 0.0 often means empty/missing slot — keep as 0 for live API
                boards.append(t)
            except (TypeError, ValueError):
                boards.append(None)
            try:
                upfreq.append(int(devs[i].get("Upfreq Complete", 0) or 0))
            except (TypeError, ValueError):
                upfreq.append(0)
            try:
                fg = float(devs[i].get("Factory GHS") or 0)
                if fg > 0:
                    factory_parts.append(fg)
            except (TypeError, ValueError):
                pass
        else:
            # layout slot without DEVS row (suspend) — null, not fake 0 pad
            boards.append(None)
            upfreq.append(0)

    mode = summary.get("Power Mode") or status.get("power_mode")
    mode_norm = mode.strip().lower() if isinstance(mode, str) else str(mode)

    with _state_lock:
        pct_cmd = _state.get("power_pct_cmd")
        lim_cmd = _state.get("power_limit_cmd")
        mode_cmd = _state.get("mode_cmd")
        work_cmd = _state.get("work_cmd")
        last_write = _state.get("last_write")

    hash_pct = status.get("hash_percent")
    try:
        hash_pct_num = float(hash_pct) if hash_pct not in (None, "") else None
    except (TypeError, ValueError):
        hash_pct_num = None

    hs = summary.get("Hash Stable")
    hs_int = 1 if str(hs).lower() in ("true", "1") else 0

    # Whatsminer reports MHS* as large numbers; TH/s ≈ MHS / 1e6
    mhs = summary.get("HS RT") or summary.get("MHS 1m") or summary.get("MHS av")
    hashrate_th = None
    try:
        if mhs is not None:
            hashrate_th = float(mhs) / 1_000_000.0
    except (TypeError, ValueError):
        hashrate_th = None

    power_limit = _f(summary.get("Power Limit"))
    factory_ghs = None
    try:
        fg = summary.get("Factory GHS")
        if fg is not None and float(fg) > 0:
            factory_ghs = float(fg)
    except (TypeError, ValueError):
        factory_ghs = None
    if factory_ghs is None and factory_parts:
        factory_ghs = sum(factory_parts)

    # Only real get_error_code entries — no Hash Stable soft-alerts
    miner_errors = _resolve_miner_errors(
        raw_errors,
        hashrate_th=hashrate_th,
        power_limit=power_limit,
        factory_ghs=factory_ghs,
    )

    body = {
        "ok": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "host": f"{HOST_MINER}:{PORT_MINER}",
        "liquid": liquid,
        "liquid_source": (
            "v3"
            if liquid is not None
            and status.get("liquid_temp") in (None, "")
            and summary.get("Liquid Temp") in (None, "")
            else ("status" if status.get("liquid_temp") not in (None, "") else None)
        ),
        "env": summary.get("Env Temp"),
        "chip_min": summary.get("Chip Temp Min"),
        "chip_avg": summary.get("Chip Temp Avg"),
        "chip_max": summary.get("Chip Temp Max"),
        "boards": boards,
        "upfreq": upfreq,
        "board_count": n_boards,
        "board_chart_slots": list(hb_layout.get("chart") or [0, 2]),
        "board_layout_key": hb_layout.get("model_key"),
        "board_layout_note": hb_layout.get("note"),
        "miner_type": miner_type_s,
        "power": summary.get("Power"),
        "mode": mode,
        "mode_norm": mode_norm,
        "mineroff": status.get("mineroff"),
        "mineroff_reason": status.get("mineroff_reason"),
        "power_limit": summary.get("Power Limit"),
        "power_limit_set": status.get("power_limit_set"),
        "power_pct_reported": hash_pct_num,
        "power_pct_cmd": pct_cmd,
        "power_limit_cmd": lim_cmd,
        "mode_cmd": mode_cmd,
        "work_cmd": work_cmd,
        # measured from miner API (for UI); work_cmd remains last commanded
        "work_measured": (
            "suspend"
            if _measured_work_state(
                {
                    "mineroff": status.get("mineroff"),
                    "mode": mode,
                    "mode_norm": mode_norm,
                    "power": summary.get("Power"),
                    "hashrate_th": hashrate_th,
                }
            )
            == "sleep"
            else "resume"
        ),
        "mode_measured": mode_norm,
        "power_limit_measured": (
            _f(status.get("power_limit_set"))
            if _f(status.get("power_limit_set")) is not None
            else _f(summary.get("Power Limit"))
        ),
        "last_write": last_write,
        "freq_avg": summary.get("freq_avg"),
        "hashrate_th": hashrate_th,
        "mhs_rt": summary.get("HS RT"),
        "mhs_1m": summary.get("MHS 1m"),
        "mhs_av": summary.get("MHS av"),
        "hash_stable": summary.get("Hash Stable"),
        "hash_stable_i": hs_int,
        "elapsed": summary.get("Elapsed"),
        "uptime": summary.get("Uptime"),
        "psu_temp": psu_temp,
        "psu_fan": psu_fan,
        "psu_pin": psu_pin,
        "psu_vin": psu_vin,  # PowerVin V (input)
        "psu_iin": psu_iin,  # PowerIin A (input)
        "psu_model": psu_model,
        "miner_errors": miner_errors,
        "dry_run": bool(DRY_RUN),
        "policy": get_policy_status(),
    }
    # lifecycle status (starting / stopping / tuning / running / stopped)
    try:
        rs = mining_run_status(body)
        body["run_status"] = rs.get("key")
        body["run_status_ru"] = rs.get("label_ru")
        body["run_status_en"] = rs.get("label_en")
    except Exception:
        body["run_status"] = None
    _mark_miner_live_ok()
    return body


def _measured_work_state(live: dict) -> str:
    """
    Actual mining state from miner API (NOT last work_cmd).
    Primary signals: status.mineroff + hashrate. Residual board power after
    power_off is normal and must NOT look like Resume (avoids Suspend thrash).
    sleep/suspend · resume/mining
    """
    mo = live.get("mineroff")
    mo_s = str(mo).strip().lower() if mo is not None else ""
    p = _f(live.get("power"))
    h = _f(live.get("hashrate_th"))
    hashing = h is not None and h >= 1.0  # TH/s

    # API says miner off → Suspend, unless it is actually hashing
    if mo_s in ("true", "1", "yes"):
        return "resume" if hashing else "sleep"

    mode = str(live.get("mode_norm") or live.get("mode") or "").lower()
    if "sleep" in mode or mode in ("off", "power_off"):
        return "resume" if hashing else "sleep"

    # mineroff false / unknown: hashing or sustained power = mining
    if hashing:
        return "resume"
    if mo_s in ("false", "0", "no"):
        # claimed on — residual <100 W after stop still common; use higher bar
        if p is not None and p >= 200:
            return "resume"
        if p is not None and p < 80 and not hashing:
            return "sleep"
        return "resume"  # mineroff false default = mining path
    # no mineroff field
    if p is not None and p >= 200:
        return "resume"
    if p is not None and p < 50 and not hashing:
        return "sleep"
    return "resume" if hashing else "sleep"


def _infer_work_state(live: dict) -> str:
    """resume | sleep — measured state for history bands / UI badge."""
    return _measured_work_state(live)


def _fmt_last_share_time(v) -> str:
    """Whatsminer Last Share Time: 0 → Never; else unix or elapsed."""
    if v is None or v == "" or v == 0 or v == "0":
        return "Never"
    try:
        n = float(v)
    except (TypeError, ValueError):
        s = str(v).strip()
        return s if s else "Never"
    if n <= 0:
        return "Never"
    # absolute unix seconds
    if n > 1_000_000_000:
        try:
            return datetime.fromtimestamp(n).strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            return str(int(n))
    # relative seconds since last share
    sec = int(n)
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def _normalize_pool_row(p: dict, idx: int = 0) -> dict:
    """Map Whatsminer POOLS[] entry → UI row (hash2cash-style columns)."""
    active = p.get("Stratum Active")
    if active is None:
        active = p.get("stratum_active")
    if isinstance(active, str):
        active = active.strip().lower() in ("true", "1", "yes")
    else:
        active = bool(active)

    lst_raw = p.get("Last Share Time", p.get("last_share_time"))
    diff = p.get("Stratum Difficulty", p.get("Difficulty", p.get("difficulty")))
    try:
        diff_f = float(diff) if diff not in (None, "") else None
    except (TypeError, ValueError):
        diff_f = None

    def _i(key_a, key_b=None, default=0):
        v = p.get(key_a)
        if v is None and key_b:
            v = p.get(key_b)
        try:
            return int(float(v)) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    pool_n = p.get("POOL", p.get("pool", idx + 1))
    try:
        pool_n = int(pool_n)
    except (TypeError, ValueError):
        pool_n = idx + 1

    return {
        "pool": pool_n,
        "url": p.get("URL") or p.get("url") or "—",
        "active": active,
        "user": p.get("User") or p.get("user") or "—",
        "status": p.get("Status") or p.get("status") or "—",
        "difficulty": diff_f,
        "getworks": _i("Getworks", "getworks"),
        "accepted": _i("Accepted", "accepted"),
        "rejected": _i("Rejected", "rejected"),
        "stale": _i("Stale", "stale"),
        "discarded": _i("Discarded", "discarded"),
        "lst": lst_raw,
        "lst_label": _fmt_last_share_time(lst_raw),
        "priority": _i("Priority", "priority", default=idx),
        "rejected_pct": _f(
            p["Pool Rejected%"]
            if "Pool Rejected%" in p
            else p.get("rejected_pct")
        ),
        "stale_pct": _f(
            p["Pool Stale%"] if "Pool Stale%" in p else p.get("stale_pct")
        ),
    }


_pools_cache_lock = threading.Lock()
_pools_cache: dict | None = None
_pools_cache_ts = 0.0
_POOLS_CACHE_TTL = 15.0


def fetch_mining_pools(*, force: bool = False) -> dict:
    """
    Mining pool list + share stats from Whatsminer `pools` command.
    Cached briefly to avoid hammering :4028.
    """
    global _pools_cache, _pools_cache_ts
    now = time.time()
    with _pools_cache_lock:
        if (
            not force
            and _pools_cache is not None
            and (now - _pools_cache_ts) < _POOLS_CACHE_TTL
        ):
            out = dict(_pools_cache)
            out["cached"] = True
            return out

    try:
        raw = miner_cmd({"cmd": "pools"}, timeout=6)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "pools": [],
            "host": f"{HOST_MINER}:{PORT_MINER}",
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    pools_raw = None
    if isinstance(raw, dict):
        pools_raw = raw.get("POOLS") or raw.get("pools")
        if pools_raw is None:
            msg = raw.get("Msg")
            if isinstance(msg, list):
                pools_raw = msg
            elif isinstance(msg, dict) and isinstance(msg.get("POOLS"), list):
                pools_raw = msg["POOLS"]
    if not isinstance(pools_raw, list):
        pools_raw = []

    pools = []
    for i, p in enumerate(pools_raw):
        if isinstance(p, dict):
            pools.append(_normalize_pool_row(p, i))

    # active first, then pool number
    pools.sort(key=lambda r: (0 if r.get("active") else 1, r.get("pool") or 99))

    body = {
        "ok": True,
        "pools": pools,
        "count": len(pools),
        "host": f"{HOST_MINER}:{PORT_MINER}",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cached": False,
    }
    with _pools_cache_lock:
        _pools_cache = dict(body)
        _pools_cache_ts = now
    return body


def _infer_upfreq_ok(live: dict) -> int | None:
    """1 = all boards Upfreq Complete, 0 = tuning (any board still 0)."""
    up = live.get("upfreq")
    if not isinstance(up, (list, tuple)) or not up:
        return None
    try:
        vals = [int(x) for x in up]
    except (TypeError, ValueError):
        return None
    # any board not complete → still tuning
    return 1 if all(v == 1 for v in vals) else 0


def _sample_outdoor_c() -> float | None:
    """Outdoor °C from Open-Meteo cache (refresh if TTL expired)."""
    try:
        w = fetch_weather_current(force=False)
        if not w or w.get("enabled") is False:
            return None
        if w.get("ok") or w.get("stale"):
            return _f(w.get("temp_c"))
    except Exception:
        pass
    return None


def _sample_eff_jt(power, hashrate_th) -> float | None:
    """
    J/TH = W / (TH/s).
    Undefined (no hash, div-by-zero, offline ramp) → 0 for clean charts.
    """
    p = _f(power)
    h = _f(hashrate_th)
    if p is None and h is None:
        return 0.0
    if p is None:
        p = 0.0
    if h is None or h <= 0.05:
        return 0.0
    if p <= 0:
        return 0.0
    try:
        return round(p / h, 3)
    except Exception:
        return 0.0


def live_to_sample(live: dict) -> dict:
    boards = live.get("boards") or [None, None, None, None]
    lim_set = live.get("power_limit_set")
    try:
        lim_set_f = float(lim_set) if lim_set not in (None, "") else None
    except (TypeError, ValueError):
        lim_set_f = None
    now = time.time()
    online = 1 if live.get("ok") is not False else 0
    power = _f(live.get("power"))
    hashrate_th = _f(live.get("hashrate_th"))
    return {
        "ts": now,
        "ts_iso": datetime.now().isoformat(timespec="seconds"),
        "liquid": _f(live.get("liquid")),
        "env": _f(live.get("env")),
        "chip_min": _f(live.get("chip_min")),
        "chip_avg": _f(live.get("chip_avg")),
        "chip_max": _f(live.get("chip_max")),
        "board0": _f(boards[0] if len(boards) > 0 else None),
        "board1": _f(boards[1] if len(boards) > 1 else None),
        "board2": _f(boards[2] if len(boards) > 2 else None),
        "board3": _f(boards[3] if len(boards) > 3 else None),
        "power": power,
        "power_limit": _f(live.get("power_limit")),
        "power_limit_set": lim_set_f,
        "power_pct_cmd": _f(live.get("power_pct_cmd")),
        "freq": _f(live.get("freq_avg")),
        "hashrate_th": hashrate_th,
        "mode": live.get("mode"),
        "hash_stable": live.get("hash_stable_i", 0),
        "online": online,
        "work_state": _infer_work_state(live) if online else None,
        "upfreq_ok": _infer_upfreq_ok(live) if online else None,
        "outdoor_c": _sample_outdoor_c(),
        # always store a number for chart (0 = undefined / no hash)
        "eff_jt": _sample_eff_jt(power, hashrate_th) if online else 0.0,
    }


def offline_sample(err: str | None = None) -> dict:
    """Marker sample when miner is unreachable from monitor."""
    now = time.time()
    return {
        "ts": now,
        "ts_iso": datetime.now().isoformat(timespec="seconds"),
        "liquid": None,
        "env": None,
        "chip_min": None,
        "chip_avg": None,
        "chip_max": None,
        "board0": None,
        "board1": None,
        "board2": None,
        "board3": None,
        "power": None,
        "power_limit": None,
        "power_limit_set": None,
        "power_pct_cmd": None,
        "freq": None,
        "hashrate_th": None,
        "mode": None,
        "hash_stable": None,
        "online": 0,
        "work_state": None,
        "upfreq_ok": None,
        # weather is independent of miner link
        "outdoor_c": _sample_outdoor_c(),
        "eff_jt": None,
    }


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _miner_cmd_result(resp) -> tuple[bool, str]:
    """Parse Whatsminer write response → (ok, message)."""
    if resp is None:
        return False, "empty response from miner"
    if not isinstance(resp, dict):
        return True, str(resp)

    # Encrypted / opaque blob without STATUS — treat as ok (can't decode further)
    if "STATUS" not in resp and "status" not in resp and "Msg" not in resp and "msg" not in resp:
        if "enc" in resp or "data" in resp:
            return True, ""
        return True, ""

    status = resp.get("STATUS", resp.get("status"))
    msg = resp.get("Msg", resp.get("msg", ""))
    desc = resp.get("Description", resp.get("description", ""))

    # STATUS can be list of dicts: [{"STATUS":"E","Msg":"..."}]
    if isinstance(status, list) and status:
        first = status[0]
        if isinstance(first, dict):
            s = str(first.get("STATUS") or first.get("status") or "").upper()
            msg = first.get("Msg") or first.get("msg") or msg
            desc = first.get("Description") or first.get("description") or desc
            status = s
        else:
            status = str(status[0]).upper()
    elif status is not None:
        status = str(status).upper()

    if isinstance(msg, dict):
        # rare: Msg is payload object on read cmds
        msg_s = json.dumps(msg, ensure_ascii=False)[:200]
    else:
        msg_s = str(msg or "").strip()

    desc_s = str(desc or "").strip()
    text = msg_s or desc_s or ""

    if status in ("E", "F", "N", "ERROR", "FAIL", "FAILED"):
        return False, text or f"miner STATUS={status}"

    # Some firmwares put errors only in Msg while STATUS is S
    low = text.lower()
    err_tokens = (
        "error",
        "fail",
        "invalid",
        "denied",
        "reject",
        "low_hash",
        "low hash",
        "not support",
        "permission",
        "over max",
    )
    if any(t in low for t in err_tokens) and low not in ("ok", "success"):
        return False, text

    return True, text if text.lower() not in ("ok", "success") else ""


def _record_write(action: str, value, resp, *, warning: str | None = None) -> dict:
    """Persist last write + raise if miner rejected command."""
    ok, msg = _miner_cmd_result(resp)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "value": value,
        "ok": ok,
        "error": None if ok else (msg or "miner rejected command"),
        "msg": msg or None,
    }
    with _state_lock:
        _state["last_write"] = entry
        _state["last_write_result"] = resp
        if action == "mode":
            _state["mode_cmd"] = value
        elif action == "working":
            _state["work_cmd"] = value
        elif action == "power_pct":
            _state["power_pct_cmd"] = value
        elif action == "power_limit":
            _state["power_limit_cmd"] = value
        _save_state()
    # Soft invalidate: keep last-good for instant TG /status; also patch
    # commanded fields so miner card reflects the write before next poll.
    with _cache_lock:
        if isinstance(_cache, dict) and _cache.get("ok"):
            if action == "mode" and value is not None:
                _cache["mode"] = value
                _cache["mode_norm"] = str(value).strip().lower()
                _cache["mode_measured"] = _cache["mode_norm"]
                _cache["mode_cmd"] = value
            elif action == "working" and value is not None:
                v = str(value).strip().lower()
                if v in ("sleep", "suspend"):
                    _cache["work_measured"] = "suspend"
                    _cache["mineroff"] = "true"
                elif v in ("resume", "mining", "on"):
                    _cache["work_measured"] = "resume"
                    _cache["mineroff"] = "false"
                _cache["work_cmd"] = value
            elif action == "power_pct" and value is not None:
                _cache["power_pct_cmd"] = value
            elif action == "power_limit" and value is not None:
                try:
                    w = float(value)
                    _cache["power_limit_cmd"] = value
                    _cache["power_limit_set"] = w
                    _cache["power_limit_measured"] = w
                except (TypeError, ValueError):
                    pass
            _cache["last_write"] = entry
    _invalidate_cache(hard=False)
    if not ok:
        # Expand laconic Whatsminer msgs so UI/TG get actionable text
        err = entry["error"] or "miner rejected command"
        low = str(err).lower()
        if "can't access write" in low or "cant access write" in low:
            err = (
                "can't access write cmd — API записи закрыт. "
                "WhatsMinerTool/web могут работать, а TCP :4028 write — нет, "
                "пока не смените API-пароль в WhatsMinerTool (можно вернуть admin) "
                "и укажете тот же пароль в poolheat Settings → Miner."
            )
        elif "enc json load" in low:
            err = (
                "enc json load err — неверный API-пароль "
                "(майнер не расшифровал privileged-команду)."
            )
        entry["error"] = err
        with _state_lock:
            _state["last_write"] = entry
            _save_state()
        raise RuntimeError(err)
    out = {
        "ok": True,
        "action": action,
        "value": value,
        "response": resp,
        "msg": msg or None,
    }
    if warning:
        out["warning"] = warning
    return out


def _live_power_limit_w(live: dict | None = None) -> float | None:
    """Configured power limit (prefer power_limit_set; summary Power Limit is 0 in Suspend)."""
    try:
        live = live if isinstance(live, dict) else fetch_live()
    except Exception:
        return None
    lim = _f(live.get("power_limit_measured"))
    if lim is None:
        lim = _f(live.get("power_limit_set"))
    if lim is None:
        lim = _f(live.get("power_limit"))
    return lim


def apply_set(action: str, value, password: str) -> dict:
    action = (action or "").strip().lower()
    password = password or DEFAULT_API_PASSWORD

    # No write if ASIC is offline (recent live read failed)
    if not miner_is_online(max_age_sec=_MINER_ONLINE_MAX_AGE_SEC, probe=True):
        raise RuntimeError(
            "ASIC offline · write skipped (read недоступен — команда не отправлялась)"
        )

    if action == "mode":
        v = str(value).strip().lower()
        # Power Mode only: low / normal / high
        cmd_map = {
            "low": "set_low_power",
            "normal": "set_normal_power",
            "high": "set_high_power",
        }
        if v not in cmd_map:
            raise ValueError("Power Mode must be low|normal|high (use action=working for suspend|resume)")
        # skip if already on this mode (mode change restarts mining)
        try:
            live = fetch_live()
            cur = str(live.get("mode_norm") or live.get("mode") or "").strip().lower()
            if cur == v:
                return {
                    "ok": True,
                    "skipped": True,
                    "action": "mode",
                    "value": v,
                    "msg": f"mode already {v}",
                }
        except Exception:
            pass
        resp = miner_write_cmd({"cmd": cmd_map[v]}, password)
        out = _record_write("mode", v, resp)
        out["cmd"] = cmd_map[v]
        if isinstance(resp, dict) and resp.get("transport"):
            out["transport"] = resp.get("transport")
        return out

    if action in ("working", "working_mode", "work", "mining"):
        v = str(value).strip().lower().replace(" ", "_")
        # Mining Control: Suspend Mining / Resume Mining (Whatsminer power_off / power_on)
        aliases = {
            "sleep": "sleep",
            "suspend": "sleep",
            "suspend_mining": "sleep",
            "power_off": "sleep",
            "resume": "resume",
            "resume_mining": "resume",
            "power_on": "resume",
        }
        if v not in aliases:
            raise ValueError("Mining Control must be sleep|suspend|resume")
        stored = aliases[v]
        cmd_map = {
            "sleep": "power_off",
            "resume": "power_on",
        }
        miner_cmd_name = cmd_map[stored]
        # skip if already suspend/resume
        try:
            live = fetch_live()
            have = str(live.get("work_measured") or _live_work(live) or "").lower()
            want = "suspend" if stored == "sleep" else "resume"
            if have in ("sleep", "suspend") and want == "suspend":
                return {
                    "ok": True,
                    "skipped": True,
                    "action": "working",
                    "value": stored,
                    "msg": "already suspend",
                }
            if have in ("resume", "mining") and want == "resume":
                return {
                    "ok": True,
                    "skipped": True,
                    "action": "working",
                    "value": stored,
                    "msg": "already resume",
                }
        except Exception:
            pass
        resp = miner_write_cmd({"cmd": miner_cmd_name}, password)
        out = _record_write("working", stored, resp)
        out["cmd"] = miner_cmd_name
        if isinstance(resp, dict) and resp.get("transport"):
            out["transport"] = resp.get("transport")
        return out

    if action == "power_pct":
        pct = int(value)
        if not 0 <= pct <= 100:
            raise ValueError("power_pct must be 0..100")
        # skip if already at this pct — set_power_pct can disturb / restart hashing
        try:
            live = fetch_live()
            have_cmd = _f(live.get("power_pct_cmd"))
            if have_cmd is None:
                have_cmd = _f(_state.get("power_pct_cmd"))
            same = False
            if have_cmd is not None and abs(float(have_cmd) - float(pct)) < 0.5:
                same = True
            if not same:
                # estimate from power / limit (ASIC may not read back pct)
                pw = _f(live.get("power"))
                lim = _live_power_limit_w(live)
                if pw is not None and lim is not None and lim > 0 and float(pw) > 0:
                    est = 100.0 * float(pw) / float(lim)
                    if abs(est - float(pct)) <= 3.0:
                        same = True
            if same:
                return {
                    "ok": True,
                    "skipped": True,
                    "action": "power_pct",
                    "value": pct,
                    "msg": f"power_pct already {pct}%",
                }
        except Exception:
            pass
        resp = miner_write_cmd(
            {"cmd": "set_power_pct", "percent": str(pct)}, password
        )
        return _record_write("power_pct", pct, resp)

    if action in ("power_limit", "set_power_limit", "adjust_power_limit"):
        watts = int(value)
        if watts < 0 or watts > 20000:
            raise ValueError("power_limit out of range")
        # skip if already at this limit — adjust_power_limit restarts mining
        try:
            candidates: list[float] = []
            cur = _live_power_limit_w()
            if cur is not None:
                candidates.append(float(cur))
            cmd_lim = _f(_state.get("power_limit_cmd"))
            if cmd_lim is not None:
                candidates.append(float(cmd_lim))
            for c in candidates:
                if abs(int(round(float(c))) - int(watts)) <= 1:
                    return {
                        "ok": True,
                        "skipped": True,
                        "action": "power_limit",
                        "value": watts,
                        "msg": f"power_limit already {watts} W",
                    }
        except Exception:
            pass
        resp = miner_write_cmd(
            {"cmd": "adjust_power_limit", "power_limit": str(watts)}, password
        )
        return _record_write(
            "power_limit",
            watts,
            resp,
            warning="adjust_power_limit may reboot / restart mining",
        )

    # Full device reboot (Whatsminer privileged "reboot" · LuCI fallback)
    if action in ("reboot", "reboot_asic", "system_reboot"):
        resp = miner_write_cmd({"cmd": "reboot"}, password)
        out = _record_write(
            "reboot",
            "asic",
            resp,
            warning="ASIC rebooting — offline for several minutes",
        )
        if isinstance(resp, dict) and resp.get("transport"):
            out["transport"] = resp.get("transport")
        return out

    # Restart mining process only (btminer), not full OS reboot
    if action in ("restart", "restart_miner", "restart_btminer", "btminer_restart"):
        resp = miner_write_cmd({"cmd": "restart_btminer"}, password)
        out = _record_write(
            "restart_miner",
            "btminer",
            resp,
            warning="btminer restarting — hash rate rebuilds after upfreq",
        )
        if isinstance(resp, dict) and resp.get("transport"):
            out["transport"] = resp.get("transport")
        return out

    # Set mining pools via LuCI (btccom/libbtctools WhatsMinerHttpsLuci path)
    # value: list of {url,user,pass} or {"pools":[...]} — up to 3
    if action in ("pools", "set_pools", "update_pools"):
        pools_in = value
        if isinstance(pools_in, dict) and "pools" in pools_in:
            pools_in = pools_in.get("pools")
        if isinstance(pools_in, str):
            try:
                pools_in = json.loads(pools_in)
            except Exception as e:
                raise ValueError(f"pools JSON: {e}") from e
        if not isinstance(pools_in, list) or not pools_in:
            raise ValueError(
                "pools requires list [{url,user,pass},…] (1–3 entries)"
            )
        norm: list[dict] = []
        for p in pools_in[:3]:
            if not isinstance(p, dict):
                raise ValueError("each pool must be an object")
            norm.append(
                {
                    "url": str(p.get("url") or p.get("URL") or "").strip(),
                    "user": str(
                        p.get("user") or p.get("User") or p.get("worker") or ""
                    ).strip(),
                    "pass": str(
                        p.get("pass") or p.get("password") or p.get("Pass") or "x"
                    ),
                }
            )
        if not any(x.get("url") for x in norm):
            raise ValueError("at least one pool url required")
        resp = miner_write_cmd({"cmd": "update_pools", "pools": norm}, password)
        out = _record_write(
            "pools",
            norm,
            resp,
            warning="pools updated · btminer restart may follow",
        )
        if isinstance(resp, dict) and resp.get("transport"):
            out["transport"] = resp.get("transport")
        # invalidate pools cache
        try:
            global _pools_cache, _pools_cache_ts
            with _pools_cache_lock:
                _pools_cache = None
                _pools_cache_ts = 0.0
        except Exception:
            pass
        return out

    # Factory reset (Whatsminer privileged "factory_reset")
    # Restores network, admin password, power mode/limit, pools-related settings.
    # API does NOT guarantee log wipe — documented as settings restore only.
    if action in (
        "factory_reset",
        "factory",
        "restore_factory",
        "reset_factory",
    ):
        # require explicit confirm value from UI
        conf = str(value or "").strip().lower()
        if conf not in ("yes", "confirm", "factory", "1", "go"):
            raise ValueError(
                "factory_reset requires value=yes (double confirm in UI)"
            )
        try:
            _policy_log("warn", "FACTORY_RESET sent · settings → defaults")
        except Exception:
            pass
        try:
            # Few attempts: each get_token burns a 30‑min slot (max ~100).
            resp = privileged_cmd(
                {"cmd": "factory_reset"}, password, token_attempts=3
            )
        except RuntimeError as e:
            err = str(e)
            if "over max" in err.lower() and "лимит" not in err.lower():
                raise RuntimeError(_OVER_MAX_CONNECT_RU) from e
            raise
        try:
            _policy_log("ok", "FACTORY_RESET · ASIC will reboot / reconfigure")
        except Exception:
            pass
        # drop token cache — password/network may change after factory
        try:
            _token_cache_clear()
        except Exception:
            pass
        return _record_write(
            "factory_reset",
            "asic",
            resp,
            warning=(
                "Factory reset sent · ASIC reboot / defaults. "
                "IP may change (DHCP)."
            ),
        )

    raise ValueError(f"unknown action: {action}")


def _invalidate_cache(*, hard: bool = False) -> None:
    """
    Soft (default): drop freshness so /api/live re-polls, but keep last-good
    snapshot for Telegram (status/miner cards stay instant).
    hard=True: wipe snapshot (host change, etc.).
    """
    global _cache, _cache_ts
    with _cache_lock:
        if hard:
            _cache = None
            _cache_ts = 0.0
        else:
            # keep _cache body; force next HTTP live to refresh
            _cache_ts = 0.0


# ─── Telegram bot (getUpdates long-poll) ──────────────────────────────────────

# Default prefs for each chat_id (copied when chat is first seen)
# Notifications OFF by default — user enables in Profile.
DEFAULT_CHAT_PREFS = {
    "lang": "ru",  # ru | en
    "notify_events": False,
    "notify_offline": False,
    "notify_safety": False,
    "notify_zone": False,
    "commands_en": True,
    # confirm main-menu Force Stop / Continue (avoid accidental taps)
    "confirm_force_stop": True,
    # main reply-keyboard sections (Status + Profile always shown)
    "show_miner": True,
    "show_policy": True,  # Events section visibility
    "show_force_stop": True,
    "show_filtration": True,
    "show_settings": True,
    "show_help": True,
}

# bool prefs stored per chat (besides lang)
_TG_BOOL_PREF_KEYS = (
    "notify_events",
    "notify_offline",
    "notify_safety",
    "notify_zone",
    "commands_en",
    "confirm_force_stop",
    "show_miner",
    "show_policy",
    "show_force_stop",
    "show_filtration",
    "show_settings",
    "show_help",
)

# callback id → pref key for menu section visibility
_TG_SECTION_TOG = {
    "miner": "show_miner",
    "policy": "show_policy",
    "force_stop": "show_force_stop",
    "filtration": "show_filtration",
    "settings": "show_settings",
    "help": "show_help",
}

DEFAULT_TELEGRAM_CFG = {
    "enabled": False,
    "bot_token": "",
    # chat ids allowed for commands + receive notifications (ints or str digits)
    "chat_ids": [],
    # per-chat prefs: { "123456": { lang, notify_* , commands_en } }
    "chats": {},
    # global defaults for NEW chats (and fallback) — all notify OFF until user opts in
    "notify_events": False,  # zone/policy event log · APPLY
    "notify_offline": False,  # ASIC offline (after N consecutive poll fails)
    "notify_offline_streak": 3,  # in a row timeouts before TG (reset on any ok poll)
    "notify_safety": False,  # Safety Critical
    "notify_zone": False,  # zone change apply
    "commands_en": True,  # /status /suspend …
    "default_lang": "ru",
    "offset": 0,  # last processed update_id + 1
    # seen chats from bot traffic: [{id, username, first_name, last_name, title, type, nick, seen_ts}]
    "chat_history": [],
}
_TG_HISTORY_MAX = 40

_tg_cfg_lock = threading.Lock()
_tg_cfg: dict = dict(DEFAULT_TELEGRAM_CFG)
_tg_stop = threading.Event()
_tg_state_lock = threading.Lock()
_tg_state: dict = {
    "ok": False,
    "last_error": None,
    "last_update_ts": None,
    "last_send_ts": None,
    "me": None,  # bot username
    "polls": 0,
}
_tg_notify_lock = threading.Lock()
_tg_last_msg_sig: dict[str, float] = {}  # debounce identical messages
# Throttle telegram_config.json writes (Entware flash is slow; was on every msg)
_tg_save_ts = 0.0
_tg_save_dirty = False
_TG_SAVE_MIN_INTERVAL_SEC = 3.0
# Per-command latency: receive → first reply / handler end (ring buffer)
_TG_TIMING_MAX = 100
_TG_TIMING_SLOW_MS = 1500.0  # print [tg] slow when first/total exceeds
_tg_timing_lock = threading.Lock()
_tg_timing_log: list[dict] = []  # newest last; max _TG_TIMING_MAX
_tg_req = threading.local()  # active handler timing context
# ASIC offline streak: only TG after N consecutive fails; reset on success
_tg_offline_streak = 0
_tg_offline_notified = False
# Hidden tool: /emoji — wait for custom emoji and print custom_emoji_id
_tg_emoji_wait_lock = threading.Lock()
_tg_emoji_wait: set[str] = set()  # chat id keys in capture mode

# Custom emoji for miner host line (🖥 → pack glyph)
# HTML: <tg-emoji emoji-id="5399965542633200318">📦</tg-emoji>
_TG_MINER_HOST_EMOJI_ID = "5399965542633200318"
_TG_MINER_HOST_EMOJI_FB = "📦"  # unicode fallback inside custom_emoji entity
# Control / write FAIL in Miner + Status
# <tg-emoji emoji-id="5278578973595427038">🚫</tg-emoji>
# Fallback inside <tg-emoji> must NOT be 🚫 — some clients render custom+alt = two 🚫.
_TG_CTRL_ERR_EMOJI_ID = "5278578973595427038"
_TG_CTRL_ERR_EMOJI_FB = "⛔"
# Inline button «Пулы» — icon_custom_emoji_id (Bot API)
# <tg-emoji emoji-id="5267040075803274242">💲</tg-emoji>
_TG_POOLS_BTN_EMOJI_ID = "5267040075803274242"


def _load_telegram_cfg() -> None:
    global _tg_cfg
    with _tg_cfg_lock:
        raw = _load_json(TELEGRAM_CFG_FILE, DEFAULT_TELEGRAM_CFG)
        cfg = dict(DEFAULT_TELEGRAM_CFG)
        cfg.update(raw if isinstance(raw, dict) else {})
        cfg["enabled"] = bool(cfg.get("enabled", False))
        cfg["bot_token"] = str(cfg.get("bot_token") or "").strip()
        ids = cfg.get("chat_ids") or []
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.replace(";", ",").split(",") if x.strip()]
        out_ids: list = []
        for x in ids if isinstance(ids, list) else []:
            try:
                out_ids.append(int(str(x).strip()))
            except (TypeError, ValueError):
                s = str(x).strip()
                if s:
                    out_ids.append(s)
        cfg["chat_ids"] = out_ids
        # legacy global key notify_policy → notify_events
        if isinstance(raw, dict) and "notify_policy" in raw and "notify_events" not in raw:
            cfg["notify_events"] = bool(raw.get("notify_policy"))
        cfg.pop("notify_policy", None)
        if isinstance(raw, dict):
            raw.pop("notify_policy", None)  # no-op on file; keep cfg clean
        for k in _TG_BOOL_PREF_KEYS:
            if k.startswith("show_"):
                cfg[k] = bool(cfg.get(k, True))
            else:
                # notify_* default OFF; commands_en default ON (DEFAULT_TELEGRAM_CFG)
                cfg[k] = bool(cfg.get(k, DEFAULT_TELEGRAM_CFG.get(k, False)))
        lang0 = str(cfg.get("default_lang") or "ru").lower()
        cfg["default_lang"] = "en" if lang0.startswith("en") else "ru"
        # per-chat map
        chats_in = cfg.get("chats") if isinstance(cfg.get("chats"), dict) else {}
        chats_out: dict = {}
        for cid in out_ids:
            key = str(cid)
            base = dict(DEFAULT_CHAT_PREFS)
            base["lang"] = cfg["default_lang"]
            for nk in _TG_BOOL_PREF_KEYS:
                # global defaults for notify_*/commands_en; show_* always default True
                if nk.startswith("show_"):
                    base[nk] = True
                else:
                    base[nk] = bool(
                        cfg.get(nk, DEFAULT_CHAT_PREFS.get(nk, False))
                    )
            prev = chats_in.get(key) if isinstance(chats_in.get(key), dict) else {}
            # also try int key as string already
            merged = dict(base)
            for pk, pv in prev.items():
                if pk == "lang":
                    merged["lang"] = "en" if str(pv).lower().startswith("en") else "ru"
                elif pk == "notify_policy":
                    # legacy → notify_events
                    merged["notify_events"] = bool(pv)
                elif pk in _TG_BOOL_PREF_KEYS or str(pk).startswith("show_"):
                    merged[pk] = bool(pv)
            merged.pop("notify_policy", None)
            chats_out[key] = merged
        # One-shot: silence all notifies (user opts in via Profile). Existing
        # chats had True from old defaults — reset once then set flag.
        notify_migrated = False
        if not cfg.get("notify_opt_in_v1"):
            for k in (
                "notify_events",
                "notify_offline",
                "notify_safety",
                "notify_zone",
            ):
                cfg[k] = False
            for ch in chats_out.values():
                if isinstance(ch, dict):
                    for k in (
                        "notify_events",
                        "notify_offline",
                        "notify_safety",
                        "notify_zone",
                    ):
                        ch[k] = False
            cfg["notify_opt_in_v1"] = True
            notify_migrated = True
        # keep orphan prefs? drop — only allowlisted chats
        cfg["chats"] = chats_out
        try:
            cfg["offset"] = max(0, int(cfg.get("offset") or 0))
        except (TypeError, ValueError):
            cfg["offset"] = 0
        try:
            cfg["notify_offline_streak"] = max(
                1, min(30, int(cfg.get("notify_offline_streak") or 3))
            )
        except (TypeError, ValueError):
            cfg["notify_offline_streak"] = 3
        # chat history (id + nick) for UI helper
        hist_in = cfg.get("chat_history") if isinstance(cfg.get("chat_history"), list) else []
        hist_out: list = []
        seen_h: set = set()
        for h in hist_in:
            if not isinstance(h, dict):
                continue
            try:
                hid = int(h.get("id"))
            except (TypeError, ValueError):
                continue
            if hid in seen_h:
                continue
            seen_h.add(hid)
            nick = str(h.get("nick") or "").strip()
            uname = str(h.get("username") or "").strip().lstrip("@")
            fn = str(h.get("first_name") or "").strip()
            ln = str(h.get("last_name") or "").strip()
            title = str(h.get("title") or "").strip()
            if not nick:
                if uname:
                    nick = "@" + uname
                elif title:
                    nick = title
                elif fn or ln:
                    nick = (fn + " " + ln).strip()
                else:
                    nick = str(hid)
            hist_out.append(
                {
                    "id": hid,
                    "username": uname,
                    "first_name": fn,
                    "last_name": ln,
                    "title": title,
                    "type": str(h.get("type") or "private"),
                    "nick": nick,
                    "seen_ts": str(h.get("seen_ts") or ""),
                }
            )
        # also seed from allowlisted chat_ids if not in history
        for cid in out_ids:
            try:
                hid = int(cid)
            except (TypeError, ValueError):
                continue
            if hid in seen_h:
                continue
            seen_h.add(hid)
            hist_out.append(
                {
                    "id": hid,
                    "username": "",
                    "first_name": "",
                    "last_name": "",
                    "title": "",
                    "type": "private",
                    "nick": str(hid),
                    "seen_ts": "",
                }
            )
        cfg["chat_history"] = hist_out[:_TG_HISTORY_MAX]
        _tg_cfg = cfg
        if notify_migrated:
            # hold lock; write directly (avoid re-entrant _save_telegram_cfg)
            try:
                _save_json(TELEGRAM_CFG_FILE, _tg_cfg)
            except Exception as e:
                print(f"[tg] notify_opt_in_v1 save: {e}")


def _save_telegram_cfg(*, force: bool = False) -> None:
    """
    Persist telegram_config.json. Throttled by default — every inbound message
    used to rewrite flash and delay getUpdates handlers on Peak.
    force=True for explicit API / offset-critical paths when needed.
    """
    global _tg_save_ts, _tg_save_dirty
    now = time.time()
    with _tg_cfg_lock:
        if (
            not force
            and _tg_save_ts
            and (now - float(_tg_save_ts)) < _TG_SAVE_MIN_INTERVAL_SEC
        ):
            _tg_save_dirty = True
            return
        _save_json(TELEGRAM_CFG_FILE, _tg_cfg)
        _tg_save_ts = now
        _tg_save_dirty = False


def get_telegram_cfg(*, redact: bool = True) -> dict:
    with _tg_cfg_lock:
        cfg = dict(_tg_cfg)
        # deep-ish copy chats
        if isinstance(cfg.get("chats"), dict):
            cfg["chats"] = {
                k: dict(v) if isinstance(v, dict) else v
                for k, v in cfg["chats"].items()
            }
    if redact and cfg.get("bot_token"):
        t = str(cfg["bot_token"])
        cfg["bot_token_set"] = True
        cfg["bot_token"] = (t[:6] + "…" + t[-4:]) if len(t) > 12 else "••••"
    else:
        cfg["bot_token_set"] = bool(cfg.get("bot_token"))
    with _tg_state_lock:
        cfg["status"] = dict(_tg_state)
    return cfg


def _tg_remember_chat(chat: dict | None, user: dict | None = None) -> None:
    """
    Record chat id + nick into chat_history (for UI picker).
    Called on every inbound message / callback.
    """
    if not isinstance(chat, dict):
        return
    try:
        hid = int(chat.get("id"))
    except (TypeError, ValueError):
        return
    ctype = str(chat.get("type") or "private")
    uname = ""
    fn = ""
    ln = ""
    title = str(chat.get("title") or "").strip()
    # prefer user for private chats
    src = user if isinstance(user, dict) else None
    if ctype == "private" and src:
        uname = str(src.get("username") or "").strip().lstrip("@")
        fn = str(src.get("first_name") or "").strip()
        ln = str(src.get("last_name") or "").strip()
    elif src:
        uname = str(src.get("username") or chat.get("username") or "").strip().lstrip("@")
        fn = str(src.get("first_name") or "").strip()
        ln = str(src.get("last_name") or "").strip()
    else:
        uname = str(chat.get("username") or "").strip().lstrip("@")
    if uname:
        nick = "@" + uname
    elif title:
        nick = title
    elif fn or ln:
        nick = (fn + " " + ln).strip()
    else:
        nick = str(hid)
    entry = {
        "id": hid,
        "username": uname,
        "first_name": fn,
        "last_name": ln,
        "title": title,
        "type": ctype,
        "nick": nick,
        "seen_ts": datetime.now().isoformat(timespec="seconds"),
    }
    with _tg_cfg_lock:
        hist = _tg_cfg.get("chat_history")
        if not isinstance(hist, list):
            hist = []
        # move to front / update
        hist = [h for h in hist if not (isinstance(h, dict) and h.get("id") == hid)]
        hist.insert(0, entry)
        _tg_cfg["chat_history"] = hist[:_TG_HISTORY_MAX]
    # persist lightly (ok to write often — small file)
    try:
        _save_telegram_cfg()
    except Exception:
        pass


def _tg_cid_key(chat_id) -> str:
    try:
        return str(int(chat_id))
    except (TypeError, ValueError):
        return str(chat_id)


def _tg_ensure_chat_prefs(chat_id) -> dict:
    """Create default prefs for chat_id if missing. Caller holds no lock required."""
    key = _tg_cid_key(chat_id)
    with _tg_cfg_lock:
        chats = _tg_cfg.setdefault("chats", {})
        if not isinstance(chats, dict):
            chats = {}
            _tg_cfg["chats"] = chats
        if key not in chats or not isinstance(chats.get(key), dict):
            prefs = dict(DEFAULT_CHAT_PREFS)
            prefs["lang"] = str(_tg_cfg.get("default_lang") or "ru")
            for nk in _TG_BOOL_PREF_KEYS:
                if nk.startswith("show_"):
                    prefs[nk] = True
                else:
                    prefs[nk] = bool(_tg_cfg.get(nk, True))
            chats[key] = prefs
        # ensure chat_id in allowlist when we have prefs from active user
        ids = list(_tg_cfg.get("chat_ids") or [])
        try:
            cid_i = int(key)
        except ValueError:
            cid_i = key
        if cid_i not in ids and key not in {str(x) for x in ids}:
            # do not auto-allow; only ensure structure for allowed chats
            pass
        return dict(chats[key])


def _tg_get_chat_prefs(chat_id) -> dict:
    key = _tg_cid_key(chat_id)
    with _tg_cfg_lock:
        chats = _tg_cfg.get("chats") if isinstance(_tg_cfg.get("chats"), dict) else {}
        p = chats.get(key)
        if isinstance(p, dict):
            out = dict(DEFAULT_CHAT_PREFS)
            out.update(p)
            # legacy cleanup for callers
            if "notify_policy" in out:
                if "notify_events" not in p:
                    out["notify_events"] = bool(out.get("notify_policy"))
                out.pop("notify_policy", None)
            return out
        # fallback global defaults
        out = dict(DEFAULT_CHAT_PREFS)
        out["lang"] = str(_tg_cfg.get("default_lang") or "ru")
        for nk in _TG_BOOL_PREF_KEYS:
            if nk.startswith("show_"):
                out[nk] = True
            else:
                out[nk] = bool(_tg_cfg.get(nk, True))
        return out


def _tg_remove_chat(chat_id, *, history: bool = False) -> dict:
    """
    Remove chat from allowlist + per-chat prefs.
    history=True also drops entry from chat_history picker.
    """
    key = _tg_cid_key(chat_id)
    with _tg_cfg_lock:
        ids = list(_tg_cfg.get("chat_ids") or [])
        _tg_cfg["chat_ids"] = [
            x
            for x in ids
            if str(x) != key and str(x) != str(chat_id)
        ]
        chats = _tg_cfg.get("chats")
        if isinstance(chats, dict):
            chats.pop(key, None)
            chats.pop(str(chat_id), None)
        if history:
            hist = _tg_cfg.get("chat_history")
            if isinstance(hist, list):
                try:
                    hid = int(key)
                except ValueError:
                    hid = None
                _tg_cfg["chat_history"] = [
                    h
                    for h in hist
                    if not (
                        isinstance(h, dict)
                        and (
                            str(h.get("id")) == key
                            or (hid is not None and h.get("id") == hid)
                        )
                    )
                ]
    try:
        _save_telegram_cfg()
    except Exception:
        pass
    _load_telegram_cfg()
    return get_telegram_cfg(redact=True)


def _tg_set_chat_prefs(chat_id, **updates) -> dict:
    key = _tg_cid_key(chat_id)
    with _tg_cfg_lock:
        chats = _tg_cfg.setdefault("chats", {})
        if not isinstance(chats, dict):
            chats = {}
            _tg_cfg["chats"] = chats
        cur = dict(DEFAULT_CHAT_PREFS)
        if isinstance(chats.get(key), dict):
            cur.update(chats[key])
        for k, v in updates.items():
            if k == "lang":
                cur["lang"] = "en" if str(v).lower().startswith("en") else "ru"
            elif k == "notify_policy":
                cur["notify_events"] = bool(v)
            elif k in _TG_BOOL_PREF_KEYS or str(k).startswith("show_"):
                cur[k] = bool(v)
        cur.pop("notify_policy", None)
        chats[key] = cur
        # ensure allowlisted
        ids = list(_tg_cfg.get("chat_ids") or [])
        try:
            cid_i = int(key)
        except ValueError:
            cid_i = key
        if cid_i not in ids and str(cid_i) not in {str(x) for x in ids}:
            ids.append(cid_i)
            _tg_cfg["chat_ids"] = ids
        out = dict(cur)
    try:
        _save_telegram_cfg()
    except Exception:
        pass
    return out


def _tg_chat_wants(chat_id, notify_kind: str) -> bool:
    """notify_kind: events | offline | safety | zone"""
    prefs = _tg_get_chat_prefs(chat_id)
    key = {
        "events": "notify_events",
        "offline": "notify_offline",
        "safety": "notify_safety",
        "zone": "notify_zone",
    }.get(str(notify_kind or "").strip().lower())
    if not key:
        return True
    return bool(prefs.get(key, True))


def _tg_api(method: str, payload: dict | None = None, *, timeout: float = 35.0) -> dict:
    with _tg_cfg_lock:
        token = str(_tg_cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("bot_token empty")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {"User-Agent": f"poolheat/{get_app_version()} telegram"}
    if payload is not None:
        raw = json.dumps(payload).encode("utf-8")
        data = raw
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(body, dict):
        raise RuntimeError("bad telegram response")
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or "telegram api error")
    return body


def _tg_chat_allowed(chat_id) -> bool:
    with _tg_cfg_lock:
        ids = list(_tg_cfg.get("chat_ids") or [])
    if not ids:
        return False
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = str(chat_id)
    return cid in ids or str(cid) in {str(x) for x in ids}


def _tg_target_chats() -> list:
    with _tg_cfg_lock:
        return list(_tg_cfg.get("chat_ids") or [])


def _tg_utf16_len(s: str) -> int:
    """Length in UTF-16 code units (Telegram entity offsets)."""
    return len((s or "").encode("utf-16-le")) // 2


def _tg_miner_host_prefix() -> str:
    """📦  — fallback glyph for custom miner-host emoji."""
    return f"{_TG_MINER_HOST_EMOJI_FB}  "


def _tg_html_esc(s) -> str:
    """Escape for Telegram parse_mode=HTML."""
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _tg_miner_host_line_html(host: str) -> str:
    """Host line with custom emoji via HTML (works with parse_mode=HTML)."""
    return (
        f'<tg-emoji emoji-id="{_TG_MINER_HOST_EMOJI_ID}">'
        f"{_TG_MINER_HOST_EMOJI_FB}</tg-emoji>  {_tg_html_esc(host)}"
    )


def _tg_attach_miner_host_emoji(text: str) -> tuple[str, list[dict]]:
    """
    Replace 🖥 with pack emoji 📦 and attach custom_emoji entities.
    Returns (text, entities) for sendMessage/editMessageText.
    """
    t = text or ""
    # normalize any computer emoji to our fallback box
    t = t.replace("🖥  ", _tg_miner_host_prefix()).replace("🖥 ", _tg_miner_host_prefix())
    t = t.replace("🖥", _TG_MINER_HOST_EMOJI_FB)
    entities: list[dict] = []
    fb = _TG_MINER_HOST_EMOJI_FB
    fb_u16 = _tg_utf16_len(fb)
    # scan codepoints; track UTF-16 offset
    u16 = 0
    i = 0
    while i < len(t):
        if t.startswith(fb, i):
            prev = t[i - 1] if i > 0 else "\n"
            # only host lines: start of message or after newline, then "  "
            nxt = t[i + len(fb) : i + len(fb) + 2]
            if (i == 0 or prev == "\n") and nxt == "  ":
                entities.append(
                    {
                        "type": "custom_emoji",
                        "offset": u16,
                        "length": fb_u16,
                        "custom_emoji_id": _TG_MINER_HOST_EMOJI_ID,
                    }
                )
            u16 += fb_u16
            i += len(fb)
            continue
        # advance one unicode char
        ch = t[i]
        u16 += _tg_utf16_len(ch)
        i += 1
    return t, entities


def _tg_req_begin(
    *,
    kind: str,
    cmd: str,
    chat_id=None,
    update_id=None,
) -> None:
    """Start timing for one inbound update (command or callback)."""
    _tg_req.active = True
    _tg_req.t0 = time.monotonic()
    _tg_req.kind = str(kind or "msg")[:24]
    _tg_req.cmd = str(cmd or "")[:100]
    _tg_req.chat_id = chat_id
    _tg_req.update_id = update_id
    _tg_req.n_out = 0
    _tg_req.first_ms = None  # any outbound (answer/send/edit)
    _tg_req.reply_ms = None  # first sendMessage / editMessageText
    _tg_req.answer_ms = None
    _tg_req.last_via = None
    _tg_req.outs = []


def _tg_req_note_out(via: str) -> None:
    """Mark one Telegram outbound API call after success."""
    if not getattr(_tg_req, "active", False):
        return
    t0 = getattr(_tg_req, "t0", None)
    if t0 is None:
        return
    ms = round((time.monotonic() - float(t0)) * 1000.0, 1)
    via_s = str(via or "out")[:24]
    _tg_req.n_out = int(getattr(_tg_req, "n_out", 0) or 0) + 1
    if _tg_req.first_ms is None:
        _tg_req.first_ms = ms
    if via_s == "answer":
        if _tg_req.answer_ms is None:
            _tg_req.answer_ms = ms
    elif via_s in ("send", "edit", "edit_markup") and _tg_req.reply_ms is None:
        _tg_req.reply_ms = ms
    _tg_req.last_via = via_s
    outs = getattr(_tg_req, "outs", None)
    if not isinstance(outs, list):
        outs = []
        _tg_req.outs = outs
    outs.append({"via": via_s, "ms": ms})
    if len(outs) > 8:
        del outs[:-8]


def _tg_req_end(*, error: str | None = None) -> None:
    """Close timing context and append ring-buffer entry."""
    if not getattr(_tg_req, "active", False):
        return
    t0 = getattr(_tg_req, "t0", None)
    total_ms = (
        round((time.monotonic() - float(t0)) * 1000.0, 1) if t0 is not None else None
    )
    first_ms = getattr(_tg_req, "first_ms", None)
    reply_ms = getattr(_tg_req, "reply_ms", None)
    answer_ms = getattr(_tg_req, "answer_ms", None)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": getattr(_tg_req, "kind", None),
        "cmd": getattr(_tg_req, "cmd", None),
        "chat_id": getattr(_tg_req, "chat_id", None),
        "update_id": getattr(_tg_req, "update_id", None),
        "first_ms": first_ms,
        "answer_ms": answer_ms,
        "reply_ms": reply_ms,
        "total_ms": total_ms,
        "n_out": int(getattr(_tg_req, "n_out", 0) or 0),
        "last_via": getattr(_tg_req, "last_via", None),
        "outs": list(getattr(_tg_req, "outs", None) or []),
        "error": (str(error)[:160] if error else None),
    }
    with _tg_timing_lock:
        _tg_timing_log.append(entry)
        overflow = len(_tg_timing_log) - _TG_TIMING_MAX
        if overflow > 0:
            del _tg_timing_log[:overflow]
    # console hint for slow handlers
    slow_ref = reply_ms if reply_ms is not None else first_ms
    try:
        slow = (
            (slow_ref is not None and float(slow_ref) >= _TG_TIMING_SLOW_MS)
            or (total_ms is not None and float(total_ms) >= _TG_TIMING_SLOW_MS)
        )
    except (TypeError, ValueError):
        slow = False
    if slow or error:
        print(
            f"[tg] timing {'SLOW ' if slow else ''}"
            f"cmd={entry.get('cmd')!r} first={first_ms} "
            f"reply={reply_ms} total={total_ms} ms n_out={entry.get('n_out')}"
            + (f" err={error}" if error else "")
        )
    _tg_req.active = False


def get_tg_timing(*, limit: int = 100, newest_first: bool = True) -> dict:
    """Ring buffer of recent command→reply latencies (in-memory)."""
    lim = max(1, min(int(limit or _TG_TIMING_MAX), _TG_TIMING_MAX))
    with _tg_timing_lock:
        items = list(_tg_timing_log[-lim:])
    if newest_first:
        items.reverse()
    return {
        "ok": True,
        "max": _TG_TIMING_MAX,
        "count": len(items),
        "slow_ms": _TG_TIMING_SLOW_MS,
        "items": items,
    }


def tg_send_message(
    chat_id,
    text: str,
    *,
    silent: bool = False,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
    entities: list | None = None,
    miner_host_emoji: bool = False,
) -> bool:
    text = (text or "")[:3900]
    if not text:
        return False
    try:
        ent = list(entities) if entities else None
        if miner_host_emoji and not parse_mode:
            # custom emoji entities conflict with parse_mode HTML
            text, ent2 = _tg_attach_miner_host_emoji(text)
            ent = (ent or []) + ent2
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": bool(silent),
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        elif ent:
            payload["entities"] = ent
        _tg_api("sendMessage", payload, timeout=20)
        _tg_req_note_out("send")
        with _tg_state_lock:
            _tg_state["last_send_ts"] = datetime.now().isoformat(timespec="seconds")
            _tg_state["ok"] = True
            _tg_state["last_error"] = None
        return True
    except Exception as e:
        with _tg_state_lock:
            _tg_state["ok"] = False
            _tg_state["last_error"] = str(e)
        print(f"[tg] send fail chat={chat_id}: {e}")
        return False


def tg_edit_message(
    chat_id,
    message_id: int,
    text: str,
    *,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
    entities: list | None = None,
    miner_host_emoji: bool = False,
) -> bool:
    text = (text or "")[:3900]
    try:
        ent = list(entities) if entities else None
        if miner_host_emoji and not parse_mode:
            text, ent2 = _tg_attach_miner_host_emoji(text)
            ent = (ent or []) + ent2
        payload: dict = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        elif ent:
            payload["entities"] = ent
        _tg_api("editMessageText", payload, timeout=15)
        _tg_req_note_out("edit")
        return True
    except Exception as e:
        err = str(e).lower()
        # text identical — still try to swap keyboard only
        if "not modified" in err and reply_markup is not None:
            return tg_edit_reply_markup(chat_id, message_id, reply_markup)
        if "not modified" in err:
            _tg_req_note_out("edit")
            return True
        print(f"[tg] edit fail: {e}")
        # last resort: keyboard-only edit
        if reply_markup is not None:
            return tg_edit_reply_markup(chat_id, message_id, reply_markup)
        return False


def tg_edit_reply_markup(
    chat_id, message_id: int, reply_markup: dict | None
) -> bool:
    """Update inline keyboard without changing message text."""
    try:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": int(message_id),
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        _tg_api("editMessageReplyMarkup", payload, timeout=15)
        _tg_req_note_out("edit_markup")
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            _tg_req_note_out("edit_markup")
            return True
        print(f"[tg] edit markup fail: {e}")
        return False


def tg_answer_callback(callback_query_id: str, text: str = "", *, alert: bool = False) -> None:
    try:
        _tg_api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": (text or "")[:180],
                "show_alert": bool(alert),
            },
            timeout=10,
        )
        _tg_req_note_out("answer")
    except Exception:
        pass


def _tg_dry_run_on() -> bool:
    try:
        return bool(get_miner_settings().get("dry_run"))
    except Exception:
        return bool(DRY_RUN)


def _tg_who(from_user: dict | None, chat_id=None) -> str:
    """
    Laconic actor for action log (kind already shows [tg]):
      @username   if Telegram login is set
      chat 123    otherwise (no first-name noise)
    """
    cid = chat_id if chat_id is not None else "?"
    uname = ""
    if isinstance(from_user, dict):
        uname = str(from_user.get("username") or "").strip().lstrip("@")
    if uname:
        return f"@{uname}"
    return f"chat {cid}"


def _rewrite_tg_event_msg(msg: str) -> str:
    """
    Normalize legacy TG action log lines:
      chat 843750212 · @chogdekogo · Filtration ON  →  @chogdekogo · Filtration ON
      chat 843750212 · John · Filtration ON         →  chat 843750212 · Filtration ON
    """
    m = str(msg or "").strip()
    if not m.lower().startswith("chat "):
        return m
    # chat ID · @user · action
    mm = re.match(
        r"^chat\s+(\S+)\s+·\s+(@[A-Za-z0-9_]{1,64})\s+·\s+(.+)$",
        m,
    )
    if mm:
        return f"{mm.group(2)} · {mm.group(3).strip()}"
    # chat ID · something · action  (no @login → keep chat id only)
    mm = re.match(r"^chat\s+(\S+)\s+·\s+([^·]+?)\s+·\s+(.+)$", m)
    if mm:
        return f"chat {mm.group(1)} · {mm.group(3).strip()}"
    # chat ID · @user   (no action tail)
    mm = re.match(r"^chat\s+(\S+)\s+·\s+(@[A-Za-z0-9_]{1,64})\s*$", m)
    if mm:
        return mm.group(2)
    return m


def _tg_log_control(
    chat_id,
    from_user: dict | None,
    action: str,
) -> None:
    """
    Action log after a real control change only (not confirm dialogs).
    Format: @user · Force Stop OFF   or   chat 123 · Force Stop OFF
    (kind [tg] is added by the Events / Action log UI)
    """
    who = _tg_who(from_user, chat_id)
    act = str(action or "?").strip()
    msg = f"{who} · {act}" if act else who
    try:
        _policy_log(
            "tg",
            msg,
            source="telegram",
            chat_id=str(chat_id),
        )
    except Exception as e:
        print(f"[tg] action log fail: {e}")


def _tg_apply_force_stop_result(
    chat_id,
    onoff: bool,
    lang: str = "ru",
    *,
    from_user: dict | None = None,
    log: bool = True,
) -> None:
    """Apply Force Stop and send result + main keyboard."""
    en = str(lang or "ru").lower().startswith("en")
    if log:
        _tg_log_control(
            chat_id,
            from_user,
            "Force Stop ON" if onoff else "Force Stop OFF")
    try:
        set_force_stop(bool(onoff), apply_now=True)
        if onoff:
            msg = (
                "🛑 Force Stop ON · mining suspended\n"
                "Zones & Dry Run ignored until Continue mining"
                if en
                else "🛑 Force Stop ВКЛ · майнинг остановлен\n"
                "Зоны и Dry Run игнорируются до «Продолжить майнинг»"
            )
        else:
            msg = (
                "▶️ Force Stop OFF · Continue mining\n"
                "Zone auto / Dry Run rules apply again"
                if en
                else "▶️ Force Stop ВЫКЛ · Продолжить майнинг\n"
                "Снова действуют зоны / Dry Run"
            )
        tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
    except Exception as e:
        tg_send_message(
            chat_id,
            f"❌ Force Stop: {e}",
            reply_markup=_tg_main_keyboard(lang, chat_id),
        )


def _tg_offer_force_stop_confirm(chat_id, want: bool, lang: str = "ru") -> None:
    """Inline Yes/No before Force Stop ON/OFF (profile confirm_force_stop)."""
    en = str(lang or "ru").lower().startswith("en")
    if want:
        text = (
            "⚠️ Confirm <b>Force Stop ON</b>?\n"
            "Mining will suspend · zones & Dry Run ignored until Continue."
            if en
            else "⚠️ Подтвердите <b>Force Stop ВКЛ</b>?\n"
            "Майнинг остановится · зоны и Dry Run игнорируются до «Продолжить»."
        )
    else:
        text = (
            "⚠️ Confirm <b>Force Stop OFF</b>?\n"
            "Zone auto / Dry Run rules apply again."
            if en
            else "⚠️ Подтвердите <b>Force Stop ВЫКЛ</b>?\n"
            "Снова действуют зоны / Dry Run."
        )
    markup = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Yes" if en else "✅ Да",
                    "callback_data": f"fs:yes:{1 if want else 0}",
                },
                {
                    "text": "❌ No" if en else "❌ Нет",
                    "callback_data": "fs:no",
                },
            ]
        ]
    }
    tg_send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )


def _tg_force_stop_btn_label(lang: str = "ru") -> str:
    """Main-menu toggle: Stop when off, Continue when Force Stop is active."""
    en = str(lang or "ru").lower().startswith("en")
    on = get_force_stop()
    # ▶️ continue · ⏹ stop (same “transport” style)
    if en:
        return "▶️ Continue mining" if on else "⏹ Stop mining"
    return "▶️ Продолжить майнинг" if on else "⏹ Остановить майнинг"


def _tg_notify_filtration(on: bool, *, source: str = "") -> None:
    """
    Push on pump state change:
      💦 Насос фильтрации включен
      🚱 Насос фильтрации выключен
    """
    on = bool(on)
    text_ru = (
        "💦 Насос фильтрации включен" if on else "🚱 Насос фильтрации выключен"
    )
    text_en = (
        "💦 Filtration pump is on" if on else "🚱 Filtration pump is off"
    )
    tg_broadcast(
        text_ru,
        debounce_key=f"filtr:{1 if on else 0}",
        notify_kind="events",
        text_by_lang={"ru": text_ru, "en": text_en},
    )


def _tg_filtration_status_lines(lang: str = "ru") -> list[str]:
    """
    Status card lines when filtration control is enabled.
    Uses last known pump state from config (no device/miner poll).
    """
    en = str(lang or "ru").lower().startswith("en")
    try:
        cfg = get_filtration_cfg(redact=True)
    except Exception:
        return []
    if not cfg.get("enabled"):
        return []
    on = cfg.get("last_on")
    if on is True:
        return ["💦 Filtration:  on" if en else "💦 Фильтрация:  вкл"]
    if on is False:
        return ["🚱 Filtration:  off" if en else "🚱 Фильтрация:  выкл"]
    return ["💧 Filtration:  —" if en else "💧 Фильтрация:  —"]


def _tg_filtration_btn_label(lang: str = "ru") -> str:
    """
    Main-menu filtration toggle label: «Фильтрация [вкл]» / «[выкл]».
    OFF is locked while mining (🔒) unless allow_off_while_mining / can_turn_off.
    Telegram cannot disable a reply key, so the lock is on the label and the
    handler refuses OFF when the option is off.
    """
    en = str(lang or "ru").lower().startswith("en")
    try:
        st = get_filtration_status(probe_live=False)
    except Exception:
        st = {}
    if not st.get("enabled"):
        return "💧 Filtration [—]" if en else "💧 Фильтрация [—]"
    on = st.get("on") is True
    mining = st.get("mining") is True
    # Same gate as UI / filtration_set: can_turn_off (allow_off_while_mining)
    locked = bool(on and mining and not st.get("can_turn_off"))
    if en:
        state = "on" if on else "off"
        if locked:
            return f"🔒 Filtration [{state}]"
        return f"💧 Filtration [{state}]"
    state = "вкл" if on else "выкл"
    if locked:
        return f"🔒 Фильтрация [{state}]"
    return f"💧 Фильтрация [{state}]"


def _tg_main_keyboard(lang: str = "ru", chat_id=None) -> dict:
    """
    Persistent reply keyboard — main navigation.
    Per-chat show_* prefs hide optional sections (Status + Profile always on).
    Fast path: no miner I/O (filtration label uses cache only).
    """
    prefs: dict = {}
    if chat_id is not None:
        try:
            prefs = _tg_get_chat_prefs(chat_id)
            if not lang:
                lang = str(prefs.get("lang") or "ru")
        except Exception:
            prefs = {}
    en = str(lang or "ru").lower().startswith("en")

    def _show(key: str) -> bool:
        # default visible if pref missing
        return bool(prefs.get(key, True)) if prefs else True

    # Info + Pools live under Miner (inline), not on the main keyboard.
    fs_btn = _tg_force_stop_btn_label(lang)

    # One status read (cache) — avoid double get_filtration_status → was 2× fetch_live
    filt_st: dict = {}
    try:
        filt_st = get_filtration_status(probe_live=False)
    except Exception:
        filt_st = {}
    filt_enabled = bool(filt_st.get("enabled"))
    fl_btn = None
    if filt_enabled:
        # inline label from same snapshot (no second call)
        on = filt_st.get("on") is True
        mining = filt_st.get("mining") is True
        locked = bool(on and mining and not filt_st.get("can_turn_off"))
        if en:
            state = "on" if on else "off"
            fl_btn = (
                f"🔒 Filtration [{state}]"
                if locked
                else f"💧 Filtration [{state}]"
            )
        else:
            state = "вкл" if on else "выкл"
            fl_btn = (
                f"🔒 Фильтрация [{state}]"
                if locked
                else f"💧 Фильтрация [{state}]"
            )

    # Row 1: Status always · Miner optional
    row1 = [{"text": "📊 Status" if en else "📊 Статус"}]
    if _show("show_miner"):
        row1.append({"text": "⛏ Miner" if en else "⛏ Майнер"})

    # Row 2: Profile always · Events (policy log) optional
    row2 = [{"text": "👤 Profile" if en else "👤 Профайл"}]
    if _show("show_policy"):
        row2.append({"text": "📋 Events" if en else "📋 События"})

    rows: list[list[dict]] = [row1, row2]
    if _show("show_force_stop"):
        rows.append([{"text": fs_btn}])
    if filt_enabled and _show("show_filtration") and fl_btn:
        rows.append([{"text": fl_btn}])

    # Settings / Help
    tail: list[dict] = []
    if _show("show_settings"):
        tail.append({"text": "⚙️ Settings" if en else "⚙️ Настройки"})
    if _show("show_help"):
        tail.append({"text": "❓ Help" if en else "❓ Справка"})
    if tail:
        rows.append(tail)

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _tg_on_mark(v: bool, en: bool) -> str:
    if en:
        return "✅ ON" if v else "⬜ OFF"
    return "✅ ВКЛ" if v else "⬜ ВЫКЛ"


def _tg_settings_inline(
    chat_id, prefs: dict | None = None, *, view: str = "root"
) -> dict:
    """
    Inline keyboard for personal settings.
    view:
      root     — language · Notifications · Menu sections · Control · nav
      notify   — expand notification toggles + Back
      sections — expand main-menu section visibility + Back
    """
    p = prefs or _tg_get_chat_prefs(chat_id)
    en = str(p.get("lang") or "ru").lower().startswith("en")
    v = str(view or "root").lower()
    if v.startswith("notif"):
        view = "notify"
    elif v.startswith("sec") or v in ("menu", "sections", "menu_sec"):
        view = "sections"
    else:
        view = "root"

    def on_mark(key: str, default: bool = True) -> str:
        return _tg_on_mark(bool(p.get(key, default)), en)

    if view == "notify":
        # Expanded: each notification toggle + back to profile root
        rows = [
            [
                {
                    "text": (
                        f"Zone {on_mark('notify_zone')}"
                        if en
                        else f"Зоны {on_mark('notify_zone')}"
                    ),
                    "callback_data": "s:tog:zone",
                }
            ],
            [
                {
                    "text": f"Safety {on_mark('notify_safety')}",
                    "callback_data": "s:tog:safety",
                }
            ],
            [
                {
                    "text": f"Offline {on_mark('notify_offline')}",
                    "callback_data": "s:tog:offline",
                }
            ],
            [
                {
                    "text": (
                        f"Events {on_mark('notify_events')}"
                        if en
                        else f"События {on_mark('notify_events')}"
                    ),
                    "callback_data": "s:tog:events",
                }
            ],
            [
                {
                    "text": "◀️ " + ("Back" if en else "Назад"),
                    "callback_data": "s:notify:back",
                }
            ],
        ]
        return {"inline_keyboard": rows}

    if view == "sections":
        # Which main-menu buttons to show (Status + Profile always on)
        def sec_btn(sid: str, label_en: str, label_ru: str) -> dict:
            key = _TG_SECTION_TOG[sid]
            return {
                "text": f"{label_en if en else label_ru} {on_mark(key)}",
                "callback_data": f"s:togsec:{sid}",
            }

        rows = [
            [sec_btn("miner", "⛏ Miner", "⛏ Майнер")],
            [sec_btn("policy", "📋 Events", "📋 События")],
            [sec_btn("force_stop", "⏹ Force Stop", "⏹ Force Stop")],
            [sec_btn("filtration", "💧 Filtration", "💧 Фильтрация")],
            [sec_btn("settings", "⚙️ Settings", "⚙️ Настройки")],
            [sec_btn("help", "❓ Help", "❓ Справка")],
            [
                {
                    "text": "◀️ " + ("Back" if en else "Назад"),
                    "callback_data": "s:sections:back",
                }
            ],
        ]
        return {"inline_keyboard": rows}

    # Root profile keyboard
    lang_ru = "● RU" if not en else "RU"
    lang_en = "● EN" if en else "EN"
    # compact status of all notify flags on the folder button
    n_on = sum(
        1
        for k in (
            "notify_zone",
            "notify_safety",
            "notify_offline",
            "notify_events",
        )
        if bool(p.get(k, True))
    )
    notify_btn = (
        f"🔔 Notifications · {n_on}/4"
        if en
        else f"🔔 Уведомления · {n_on}/4"
    )
    sec_keys = list(_TG_SECTION_TOG.values())
    s_on = sum(1 for k in sec_keys if bool(p.get(k, True)))
    s_tot = len(sec_keys)
    sections_btn = (
        f"📱 Menu sections · {s_on}/{s_tot}"
        if en
        else f"📱 Разделы меню · {s_on}/{s_tot}"
    )
    rows = [
        [
            {"text": f"🌐 {lang_ru}", "callback_data": "s:lang:ru"},
            {"text": f"🌐 {lang_en}", "callback_data": "s:lang:en"},
        ],
        [
            {
                "text": notify_btn,
                "callback_data": "s:notify",
            }
        ],
        [
            {
                "text": sections_btn,
                "callback_data": "s:sections",
            }
        ],
        [
            {
                "text": (
                    f"Control {on_mark('commands_en')}"
                    if en
                    else f"Управление {on_mark('commands_en')}"
                ),
                "callback_data": "s:tog:commands",
            }
        ],
        [
            {
                "text": (
                    f"Force Stop confirm {on_mark('confirm_force_stop')}"
                    if en
                    else f"Подтверждение Force Stop {on_mark('confirm_force_stop')}"
                ),
                "callback_data": "s:tog:confirm_fs",
            }
        ],
        [
            {
                "text": "🔄 " + ("Refresh" if en else "Обновить"),
                "callback_data": "s:refresh",
            },
            {
                "text": "◀️ " + ("Settings" if en else "Настройки"),
                "callback_data": "cfg:home",
            },
            {
                "text": "🏠 " + ("Menu" if en else "Меню"),
                "callback_data": "s:menu",
            },
        ],
    ]
    return {"inline_keyboard": rows}


def _tg_normalize_incoming_text(text: str) -> str:
    """
    Button label → /command; leave real commands as-is.
    Match by keyword (emoji variants differ across clients).
    """
    t = (text or "").strip()
    if not t:
        return t
    if t.startswith("/"):
        return t
    low = t.lower()
    # strip common emoji / symbols for matching
    bare = low
    for ch in (
        "📊", "⚙️", "⚙", "⏸", "⏸️", "▶️", "▶", "⏹", "⏹️", "🛑",
        "📋", "🏊", "ℹ️", "ℹ", "🏠", "⛏", "⛏️", "❓", "🧪", "🔴", "🟢",
        "💧", "🔒",
    ):
        bare = bare.replace(ch, " ")
    bare = " ".join(bare.split())
    if "status" in bare or bare in ("статус",):
        return "/status"
    if "miner" in bare or "майнер" in bare:
        return "/miner"
    if "dry" in bare or "dry_run" in bare or "dryrun" in bare:
        return "/dry_run"
    if bare in ("info", "инфо", "информ", "information") or "инфо" in bare:
        return "/info"
    if "профайл" in bare or "profile" in bare or bare == "prefs":
        return "/profile"
    if "setting" in bare or "настрой" in bare:
        return "/settings"
    if "update" in bare or "обновл" in bare:
        return "/update"
    # Filtration before bare "mining" match
    if (
        "filtr" in bare
        or "фильтр" in bare
        or bare in ("filter", "filtration", "фильтрация")
    ):
        return "/filtration"
    if (
        "stop mining" in bare
        or "stop work" in bare
        or "остановить майнинг" in bare
        or "остановить работу" in bare
        or bare in ("остановить", "stop", "force stop", "forcestop")
        or "force_stop" in bare
        or "останов" in bare
    ):
        return "/force_stop"
    if (
        "continue mining" in bare
        or "continue work" in bare
        or "продолжить майнинг" in bare
        or "продолжить работу" in bare
        or bare in ("продолжить", "continue")
        or "продолж" in bare
    ):
        # Continue mining = clear Force Stop (not plain resume)
        return "/force_stop"
    if "suspend" in bare or "sleep" in bare or "приостанов" in bare:
        return "/force_stop"
    if "resume" in bare or bare in ("mining", "майнинг"):
        return "/force_stop"
    if "events" in bare or "событ" in bare or bare in ("event", "policy"):
        return "/events"
    if "pool" in bare or "пул" in bare:
        return "/pools"
    if "help" in bare or "справк" in bare or "помощ" in bare:
        return "/help"
    if "menu" in bare or "меню" in bare:
        return "/start"
    return t


def tg_broadcast(
    text: str,
    *,
    silent: bool = False,
    debounce_key: str | None = None,
    notify_kind: str | None = None,
    text_by_lang: dict | None = None,
) -> int:
    """
    Send to configured chat_ids.
    notify_kind: policy|offline|safety|zone — filtered by per-chat prefs.
    text_by_lang: optional { "ru": "...", "en": "..." } overrides text per lang.
    """
    if debounce_key:
        now = time.time()
        with _tg_notify_lock:
            last = float(_tg_last_msg_sig.get(debounce_key) or 0)
            if now - last < 45.0:
                return 0
            _tg_last_msg_sig[debounce_key] = now
    n = 0
    for cid in _tg_target_chats():
        if notify_kind and not _tg_chat_wants(cid, notify_kind):
            continue
        prefs = _tg_get_chat_prefs(cid)
        lang = prefs.get("lang") or "ru"
        msg = text
        if text_by_lang and isinstance(text_by_lang, dict):
            msg = text_by_lang.get(lang) or text_by_lang.get("ru") or text_by_lang.get("en") or text
        # HTML status cards (<b>, <tg-emoji>) vs entity-based host emoji
        use_html = ("<b>" in msg) or ("<tg-emoji" in msg)
        use_icon = (not use_html) and (("📦  " in msg) or ("🖥" in msg))
        if tg_send_message(
            cid,
            msg,
            silent=silent,
            miner_host_emoji=use_icon,
            parse_mode="HTML" if use_html else None,
        ):
            n += 1
    return n


def _fmt_asic_offline_msg(err, lang: str = "ru") -> str:
    """Laconic ASIC offline line for Telegram."""
    s = str(err or "").strip()
    low = s.lower()
    en = str(lang or "ru").lower().startswith("en")
    if "timed out" in low or "timeout" in low or "incomplete json" in low:
        return "⚠️ ASIC offline · timeout" if en else "⚠️ ASIC offline · timeout"
    if "refused" in low or "reset" in low:
        return (
            "⚠️ ASIC offline · connection refused"
            if en
            else "⚠️ ASIC offline · нет соединения"
        )
    if "unreachable" in low or "no route" in low:
        return (
            "⚠️ ASIC offline · unreachable"
            if en
            else "⚠️ ASIC offline · недоступен"
        )
    if "name or service" in low or "nodename" in low or "resolve" in low:
        return (
            "⚠️ ASIC offline · host unresolved"
            if en
            else "⚠️ ASIC offline · хост не найден"
        )
    # JSON parse bugs / firmware quirks — not a true offline event
    if (
        "bad json" in low
        or "expecting" in low
        or "delimiter" in low
        or "json" in low
        and ("parse" in low or "decode" in low or "column" in low)
    ):
        return (
            "⚠️ ASIC response error · retry"
            if en
            else "⚠️ ASIC · ошибка ответа API (повтор)"
        )
    if s:
        brief = s.replace("live poll fail:", "").strip()
        if len(brief) > 60:
            brief = brief[:57] + "…"
        return f"⚠️ ASIC offline · {brief}"
    return "⚠️ ASIC offline"


def tg_note_live_poll_ok() -> None:
    """Any successful miner poll resets offline streak (no spam after blips)."""
    global _tg_offline_streak, _tg_offline_notified
    with _tg_notify_lock:
        _tg_offline_streak = 0
        _tg_offline_notified = False


def tg_note_live_poll_fail(err) -> None:
    """
    Count consecutive ASIC poll failures.
    Telegram only after notify_offline_streak in a row (default 3).
    One message per outage; resets when poll succeeds again.
    Per-chat notify_offline is applied in tg_broadcast.
    """
    global _tg_offline_streak, _tg_offline_notified
    with _tg_cfg_lock:
        if not _tg_cfg.get("enabled") or not _tg_cfg.get("bot_token"):
            return
        if not _tg_cfg.get("chat_ids"):
            return
        try:
            need = max(1, min(30, int(_tg_cfg.get("notify_offline_streak") or 3)))
        except (TypeError, ValueError):
            need = 3

    with _tg_notify_lock:
        _tg_offline_streak += 1
        streak = _tg_offline_streak
        already = _tg_offline_notified
        if streak < need or already:
            return
        _tg_offline_notified = True

    def _msg(lang: str) -> str:
        t = _fmt_asic_offline_msg(err, lang=lang)
        if need > 1:
            t = f"{t} ({streak}×)"
        return t

    tg_broadcast(
        _msg("ru"),
        debounce_key=None,
        notify_kind="offline",
        text_by_lang={"ru": _msg("ru"), "en": _msg("en")},
    )


def tg_on_policy_event(kind: str, msg: str, extra: dict | None = None) -> None:
    with _tg_cfg_lock:
        if not _tg_cfg.get("enabled") or not _tg_cfg.get("bot_token"):
            return
        if not _tg_cfg.get("chat_ids"):
            return

    msg_l = (msg or "").lower()
    kind = (kind or "info").lower()
    extra = extra or {}

    # ASIC offline handled by tg_note_live_poll_fail (streak) — skip here
    if "live poll fail" in msg_l:
        return

    if "safety" in msg_l or str(extra.get("source") or "").startswith("safety"):
        icon = "🛑" if kind in ("ok", "err", "warn") else "ℹ️"
        tg_broadcast(
            f"{icon} Safety\n{msg}",
            debounce_key="safety:" + msg[:40],
            notify_kind="safety",
            text_by_lang={
                "ru": f"{icon} Safety\n{msg}",
                "en": f"{icon} Safety\n{msg}",
            },
        )
        return

    if kind == "err" or "fail" in msg_l or str(msg or "").upper().startswith("FAIL "):
        # Humanize: FAIL working=suspend: can't access write cmd
        # → 🚫 Ошибка установки режима Suspend (custom emoji)
        emoji = _tg_ctrl_err_emoji_html()
        text_ru = f"{emoji} {_tg_fail_human_text(msg, 'ru')}"
        text_en = f"{emoji} {_tg_fail_human_text(msg, 'en')}"
        tg_broadcast(
            text_ru,
            debounce_key="err:" + (msg or "")[:50],
            notify_kind="events",
            text_by_lang={"ru": text_ru, "en": text_en},
        )
        return

    if kind == "ok" and (
        msg.startswith("AUTO ") or msg.startswith("APPLY ") or "fix working" in msg_l
    ):
        if "working=suspend" in msg_l or "enforce:suspend" in msg_l:
            tg_broadcast(
                f"⏸ Suspend\n{msg}",
                debounce_key="susp:" + msg[:40],
                notify_kind="zone",
                text_by_lang={
                    "ru": f"⏸ Suspend\n{msg}",
                    "en": f"⏸ Suspend\n{msg}",
                },
            )
            return
        if "working=resume" in msg_l:
            tg_broadcast(
                f"▶️ Resume\n{msg}",
                debounce_key="res:" + msg[:40],
                notify_kind="zone",
            )
            return
        src = str(extra.get("source") or "")
        if src in ("z0", "z1", "z2", "z3") or any(
            f" {z}" in msg or msg.startswith(f"AUTO {z}") for z in ("z0", "z1", "z2", "z3")
        ):
            tg_broadcast(
                f"📐 Zone\n{msg}",
                debounce_key="zone:" + msg[:50],
                notify_kind="zone",
            )
        else:
            tg_broadcast(
                f"✅ {msg}",
                debounce_key="pol:" + msg[:50],
                notify_kind="events",
            )


# Human titles for heat zones (UI header + Telegram)
ZONE_TITLES = {
    "z0": "Z0 · High Heat",
    "z1": "Z1 · Normal heat",
    "z2": "Z2 · Reduced heat",
    "z3": "Z3 · No heat",
    "critical": "Safety · Critical",
    "safety": "Safety · Critical",
}


def zone_title(zone_id) -> str:
    """z3 → 'Z3 · No heat'; safety sticky → full Safety title."""
    if zone_id is None or zone_id == "":
        return "—"
    k = str(zone_id).strip().lower()
    if k in ZONE_TITLES:
        return ZONE_TITLES[k]
    # allow already-pretty strings
    if "·" in str(zone_id) or " " in str(zone_id):
        return str(zone_id)
    return str(zone_id).upper()


def _tg_fmt_num(v, digits: int = 1, empty: str = "—") -> str:
    try:
        if v is None or v == "":
            return empty
        n = float(v)
        if not (n == n):  # NaN
            return empty
        if digits == 0:
            return str(int(round(n)))
        s = f"{n:.{digits}f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except (TypeError, ValueError):
        return empty


def _tg_zone_emoji(zone_id, *, safety: bool = False) -> str:
    if safety:
        return "🛑"
    k = str(zone_id or "").strip().lower()
    return {
        "z0": "🔥",
        "z1": "🟢",
        "z2": "🔵",
        "z3": "🧊",
        "critical": "🛑",
    }.get(k, "📍")


def _tg_fmt_ts_eu(ts) -> str:
    """
    Policy event timestamps for Telegram:
      04.08.2026 11:32:27
    Accepts ISO (2026-08-04T11:32:27[.fff][Z]) or already-formatted strings.
    """
    if ts is None:
        return "—"
    s = str(ts).strip()
    if not s:
        return "—"
    # already DD.MM.YYYY …
    if re.match(r"^\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}", s):
        return s[:19] if len(s) >= 19 else s
    try:
        raw = s.replace("Z", "+00:00")
        # datetime.fromisoformat handles "2026-08-04T11:32:27" and with micros
        dt = datetime.fromisoformat(raw)
        # drop tz for display (local wall time as stored)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        pass
    # fallback: 2026-08-04 11:32:27 or T-separated
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2}):(\d{2})", s
    )
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return f"{d}.{mo}.{y} {hh}:{mm}:{ss}"
    return s


def _tg_pretty_last_event(msg: str | None) -> str | None:
    if not msg:
        return None
    m = str(msg).strip()
    low = m.lower()
    if "live poll fail" in low or "timed out" in low or "timeout" in low:
        return "⚠️ ASIC offline · timeout"
    if "connection refused" in low:
        return "⚠️ ASIC offline · no connection"
    # Dry Run preview — keep short (full cmd list is noisy)
    if low.startswith("dry_run would"):
        # "DRY_RUN would z3: working=suspend, … · no write (Dry Run)"
        try:
            rest = m.split("would", 1)[1].strip()
            z = rest.split(":", 1)[0].strip().upper()
            body = rest.split(":", 1)[1] if ":" in rest else rest
            body = body.split("·")[0].strip()
            # first action only
            first = body.split(",")[0].strip()
            if "working=suspend" in first.lower() or "working=sleep" in first.lower():
                act = "Suspend"
            elif "working=resume" in first.lower():
                act = "Mining"
            else:
                act = first.replace("working=", "").replace("mode=", "mode ")
            return f"Dry Run · {z} → {act} (без записи)"
        except Exception:
            return "Dry Run · preview (без записи)"
    # APPLY / AUTO — not for Status card (noise); Action log keeps them
    if m.startswith("AUTO ") or m.startswith("APPLY "):
        return None
    if m.upper().startswith("FORCE_STOP"):
        return None
    # TG action log: "chat 123 · @user · Filtration ON" — Action log only
    if low.startswith("chat ") or " · chat " in low:
        return None
    if "filtration" in low or "фильтрац" in low or "насос" in low:
        return None
    # FAIL action=value: err — multi-line block via _tg_status_fail_lines
    if m.upper().startswith("FAIL "):
        return m
    return m


def _tg_ctrl_err_emoji_html() -> str:
    return (
        f'<tg-emoji emoji-id="{_TG_CTRL_ERR_EMOJI_ID}">'
        f"{_TG_CTRL_ERR_EMOJI_FB}</tg-emoji>"
    )


def _tg_fail_human_text(
    msg: str = "",
    lang: str = "ru",
    *,
    cmd: str | None = None,
    reply: str | None = None,
) -> str:
    """
    Map FAIL working=suspend: … → laconic human message.
    e.g. «Ошибка установки режима Suspend»
    """
    en = str(lang or "ru").lower().startswith("en")
    c = (cmd or "").strip()
    r = (reply or "").strip()
    if not c:
        m = str(msg or "").strip()
        if m.upper().startswith("FAIL "):
            m = m[5:].strip()
        if ":" in m:
            left, right = m.split(":", 1)
            c = left.strip()
            r = right.strip()
        else:
            c = m
    # Full blob for reply heuristics (raw API Msg)
    blob = f"{msg} {r} {c}".lower()
    # Whatsminer Code 45: write API locked (Tools/web may still work)
    if "can't access write" in blob or "cant access write" in blob:
        return (
            "Write API locked · cycle API password in WhatsMinerTool, set same in poolheat"
            if en
            else "API записи закрыт · смените API-пароль в WhatsMinerTool и укажите его в poolheat"
        )
    if "enc json load" in blob:
        return (
            "Wrong API password (miner cannot decrypt write)"
            if en
            else "Неверный API-пароль (майнер не расшифровал write)"
        )
    if "over max connect" in blob:
        return (
            "API token limit (over max connect) · wait ~30 min or reboot ASIC"
            if en
            else "Лимит токенов API (over max connect) · ждите ~30 мин или reboot ASIC"
        )
    action = ""
    value = ""
    if "=" in c:
        action, value = c.split("=", 1)
        action = action.strip().lower()
        value = value.strip().lower()
    else:
        action = c.lower()

    # working / Mining Control
    if action in ("working", "work", "mining", "working_mode"):
        if value in ("sleep", "suspend", "power_off", "off"):
            return (
                "Suspend mode set failed"
                if en
                else "Ошибка установки режима Suspend"
            )
        if value in ("resume", "power_on", "on", "mining"):
            return (
                "Resume mode set failed"
                if en
                else "Ошибка установки режима Resume"
            )
        return (
            f"Mining Control set failed ({value or '—'})"
            if en
            else f"Ошибка Mining Control ({value or '—'})"
        )
    if action == "mode":
        return (
            f"Power Mode set failed ({value or '—'})"
            if en
            else f"Ошибка Power Mode ({value or '—'})"
        )
    if action in ("power_limit", "set_power_limit", "adjust_power_limit"):
        return "Power Limit set failed" if en else "Ошибка Power Limit"
    if action == "power_pct":
        return "Power pct set failed" if en else "Ошибка Power pct"
    if action in ("reboot", "reboot_asic", "system_reboot"):
        return "Reboot command failed" if en else "Ошибка reboot"
    if action in ("restart", "restart_miner", "restart_btminer"):
        return "Restart miner failed" if en else "Ошибка restart miner"
    # fallback: short original without raw API dump
    if c:
        return f"Ошибка: {c}" if not en else f"Error: {c}"
    return "Ошибка управления" if not en else "Control error"


def _tg_status_fail_lines(
    msg: str,
    lang: str = "ru",
    *,
    cmd: str | None = None,
    reply: str | None = None,
) -> list[str]:
    """
    One line for Status / Miner:
      🚫 Ошибка установки режима Suspend
    """
    text = _tg_fail_human_text(msg, lang, cmd=cmd, reply=reply)
    return [f"{_tg_ctrl_err_emoji_html()} {text}"]


def _tg_zone_line(zone_id, *, safety: bool = False) -> str:
    """'🧊  Zone: Z3 – No heat'"""
    if safety:
        title = zone_title("critical").replace(" · ", " – ")
        return f"🛑  Zone: {title}"
    title = zone_title(zone_id).replace(" · ", " – ")
    return f"{_tg_zone_emoji(zone_id)}  Zone: {title}"


def _tg_active_preset_name() -> str:
    """Name of last applied zone-map preset, or —."""
    try:
        with _zone_presets_lock:
            aid = _zone_presets.get("active_id")
            if not aid:
                return "—"
            for p in _zone_presets.get("presets") or []:
                if p.get("id") == aid:
                    name = str(p.get("name") or "").strip()
                    return name if name else "—"
    except Exception:
        pass
    return "—"


def _tg_zone_label(zone_id, *, safety: bool = False) -> str:
    """Compact: Z3 (No heat) · Safety."""
    if safety:
        return "Safety"
    k = str(zone_id or "").strip().lower()
    return {
        "z0": "Z0 (High Heat)",
        "z1": "Z1 (Normal heat)",
        "z2": "Z2 (Reduced heat)",
        "z3": "Z3 (No heat)",
        "critical": "Safety",
        "safety": "Safety",
    }.get(k) or (zone_title(zone_id).replace(" · ", " (") + ")" if zone_id else "—")


def _tg_zone_profile_for_status(zone_id, *, safety: bool = False) -> dict:
    """Profile that defines «Режим зоны» (Safety → on_crit)."""
    try:
        zc = get_zone_cfg()
        zones = zc.get("zones") if isinstance(zc, dict) else {}
        zones = zones if isinstance(zones, dict) else {}
        if safety:
            crit = zones.get("critical") or {}
            if isinstance(crit, dict) and isinstance(crit.get("on_crit"), dict):
                return dict(crit["on_crit"])
            return dict(crit) if isinstance(crit, dict) else {}
        z = zones.get(str(zone_id or "").strip().lower()) or {}
        return dict(z) if isinstance(z, dict) else {}
    except Exception:
        return {}


def _tg_format_zone_mode(profile: dict | None, lang: str = "ru") -> str:
    """
    Suspend
    or Mining · Low · 1800W · 90%
    (only flags enabled on the zone profile).
    """
    en = str(lang or "ru").lower().startswith("en")
    p = profile if isinstance(profile, dict) else {}
    if not p:
        return "—"

    work_en = bool(p.get("work_en"))
    work = str(p.get("work") or "").strip().lower()
    if work_en and work in ("suspend", "sleep"):
        return "Suspend"

    parts: list[str] = []
    if work_en and work in ("resume", "mining"):
        parts.append("Mining")
    elif work_en:
        parts.append(work.capitalize() if work else "Mining")

    if p.get("mode_en"):
        m = str(p.get("mode") or "").strip().lower()
        if m in ("low", "normal", "high"):
            # short: Mining · Low · 1800W (no "Power Mode" suffix)
            parts.append(m.capitalize())
        elif m:
            parts.append(m)

    if p.get("lim_en"):
        try:
            lim = int(round(float(p.get("lim"))))
            parts.append(f"{lim}W")
        except (TypeError, ValueError):
            pass

    if p.get("pct_en"):
        try:
            pct = int(round(float(p.get("pct"))))
            parts.append(f"{pct}%")
        except (TypeError, ValueError):
            pass

    if not parts:
        return "—" if en else "—"
    return " · ".join(parts)


def _tg_street_c() -> float | None:
    """Outdoor °C from weather cache (Open-Meteo)."""
    try:
        w = fetch_weather_current(force=False)
        if not w or w.get("enabled") is False:
            return None
        if w.get("ok") or w.get("stale"):
            return _f(w.get("temp_c"))
    except Exception:
        pass
    return None


# Pool surface U-value for heat-loss model (same as UI dashboard)
POOL_U_W_M2K = 25.0


def compute_heat_balance(
    live: dict | None = None,
    street_c: float | None = None,
) -> dict | None:
    """
    Same model as UI «Нагрев / остывание»:
      T_water = pool water sensor (config water_sensor, default liquid)
      Tw = T_water − hex_delta_c
      Q_loss = U · S · max(0, Tw − Ta)
      Q_in   = min(P_miner, HEX capacity)   [HEX = ṁ·c·ΔT if known]
      Q_net  = Q_in − Q_loss
      rate   = Q_net / (m·c) · 3600   °C/h   (+ heat, − cool)
    Returns None if inputs incomplete.
    """
    try:
        der = pool_derived()
    except Exception:
        return None
    S = _f(der.get("surface_m2"))
    mass = _f(der.get("mass_kg"))
    if S is None or S <= 0 or mass is None or mass <= 0:
        return None

    live = live or {}
    Tliq, water_sens = resolve_pool_water(live, der.get("water_sensor"))
    if Tliq is None:
        return None
    hex_dt = _f(der.get("hex_delta_c")) or 0.0
    if hex_dt < 0:
        hex_dt = 0.0
    Tw = float(Tliq) - float(hex_dt)

    Ta = street_c
    if Ta is None:
        Ta = _tg_street_c()
    if Ta is None:
        return None

    P_W = _f(live.get("power"))
    if P_W is None or P_W < 0:
        return None

    dT = Tw - float(Ta)
    Q_loss_W = POOL_U_W_M2K * float(S) * max(0.0, dT)

    hex_cap_W = None
    hp = _f(der.get("hex_power_kw"))
    if hp is not None and hp > 0:
        hex_cap_W = float(hp) * 1000.0
    else:
        flow = _f(der.get("flow_m3h"))
        if flow is not None and flow > 0 and hex_dt > 0:
            m_dot = float(flow) * 1000.0 / 3600.0
            hex_cap_W = m_dot * 4186.0 * float(hex_dt)

    Q_in_W = float(P_W)
    limited = False
    if hex_cap_W is not None and hex_cap_W > 0 and Q_in_W > hex_cap_W:
        Q_in_W = hex_cap_W
        limited = True

    Q_net_W = Q_in_W - Q_loss_W
    c = 4186.0
    rate = (Q_net_W / (float(mass) * c)) * 3600.0  # °C/h

    if Q_net_W > 50:
        bal = "heat"  # нагрев / избыток
    elif Q_net_W < -50:
        bal = "cool"  # остывание / недостаток
    else:
        bal = "hold"

    return {
        "ok": True,
        "rate_c_per_h": rate,
        "q_net_kw": Q_net_W / 1000.0,
        "q_in_kw": Q_in_W / 1000.0,
        "q_loss_kw": Q_loss_W / 1000.0,
        "tw_c": Tw,
        "t_water_c": float(Tliq),
        "water_sensor": water_sens,
        "ta_c": float(Ta),
        "dt_c": dT,
        "limited_by_hex": limited,
        "balance": bal,  # heat | cool | hold
    }


def _tg_heat_balance_lines(
    live: dict | None,
    street_c: float | None = None,
    lang: str = "ru",
) -> list[str]:
    """Telegram lines for heating / cooling block."""
    en = str(lang or "ru").lower().startswith("en")
    hb = compute_heat_balance(live, street_c)
    if not hb:
        return []
    rate = float(hb["rate_c_per_h"])
    qn = float(hb["q_net_kw"])
    qi = float(hb["q_in_kw"])
    ql = float(hb["q_loss_kw"])
    bal = hb.get("balance") or "hold"

    def sgn(v: float, dig: int) -> str:
        s = f"{v:.{dig}f}"
        if v > 0:
            return "+" + s
        return s

    if bal == "heat":
        icon = "🔥"
        bal_lab = "Heating" if en else "Нагрев"
    elif bal == "cool":
        icon = "❄️"
        bal_lab = "Cooling" if en else "Остывание"
    else:
        icon = "⚖️"
        bal_lab = "Balance" if en else "Баланс"

    # Exact layout (Telegram):
    # ❄️ Остывание
    # -0.075 °C/h  ·  -2.12 kW
    # in: 1.99 kW · loss: 4.11 kW
    cap = " · HEX cap" if hb.get("limited_by_hex") else ""
    head = f"{icon} {bal_lab}"
    line1 = f"{sgn(rate, 3)} °C/h  ·  {sgn(qn, 2)} kW"
    line2 = f"in: {_tg_fmt_num(qi, 2)} kW · loss: {_tg_fmt_num(ql, 2)} kW{cap}"
    return [head, line1, line2]


def _tg_active_errors_lines(live: dict | None, lang: str = "ru", *, limit: int = 6) -> list[str]:
    """Active ASIC firmware errors (get_error_code) — shown on Miner card only."""
    en = str(lang or "ru").lower().startswith("en")
    errs = (live or {}).get("miner_errors") or []
    if not errs:
        return []
    out = ["⚠️ Errors:" if en else "⚠️ Ошибки:"]
    for e in errs[: max(1, int(limit))]:
        if isinstance(e, dict):
            code = e.get("code") or e.get("error_code") or ""
            cause = e.get("cause") or e.get("msg") or e.get("message") or ""
            # Prefer human cause; skip redundant "Error code NNNN"
            cause_s = str(cause or "").strip()
            if cause_s.lower().startswith("error code"):
                cause_s = ""
            if code and cause_s:
                line = f"  {code}  {cause_s}"
            elif code:
                line = f"  {code}"
            elif cause_s:
                line = f"  {cause_s}"
            else:
                line = f"  {e}"
            out.append(line[:120])
        else:
            out.append(f"  {str(e)[:120]}")
    more = len(errs) - limit
    if more > 0:
        out.append(f"  … +{more}" if en else f"  … ещё {more}")
    return out


def _tg_policy_block_lines(
    *,
    preset: str,
    z_lab: str,
    z_mode: str,
    dry: bool,
    fs: bool,
    lang: str = "ru",
) -> list[str]:
    """
    RU — exact spaces after colon (count, not column pad):

    Пресет:       Теплый бассейн      (7 spaces)
    T зона:         Z2 (Reduced heat) (9 spaces)
    Цель:           Mining · Low · …  (11 spaces)
    Dry Run:      вкл. ручной режим   (6 spaces)
    Force Stop:  выкл.                (2 spaces)
    """
    en = str(lang or "ru").lower().startswith("en")
    if en:
        dry_s = "on. manual mode" if dry else "off. auto mode"
        fs_s = "on." if fs else "off."
        # EN: keep same space counts as RU labels of similar length
        return [
            f"Preset:{' ' * 7}<b>{_tg_html_esc(preset)}</b>",
            f"T zone:{' ' * 9}<b>{_tg_html_esc(z_lab)}</b>",
            f"Target:{' ' * 11}<b>{_tg_html_esc(z_mode)}</b>",
            f"Dry Run:{' ' * 6}<b>{_tg_html_esc(dry_s)}</b>",
            f"Force Stop:{' ' * 2}<b>{_tg_html_esc(fs_s)}</b>",
        ]
    dry_s = "вкл. ручной режим" if dry else "выкл. авто режим"
    fs_s = "вкл." if fs else "выкл."
    return [
        f"Пресет:{' ' * 7}<b>{_tg_html_esc(preset)}</b>",
        f"T зона:{' ' * 9}<b>{_tg_html_esc(z_lab)}</b>",
        f"Цель:{' ' * 11}<b>{_tg_html_esc(z_mode)}</b>",
        f"Dry Run:{' ' * 6}<b>{_tg_html_esc(dry_s)}</b>",
        f"Force Stop:{' ' * 2}<b>{_tg_html_esc(fs_s)}</b>",
    ]


def _tg_t_ctrl_sensor_label(sensor: str, lang: str = "ru") -> str:
    """Short sensor name for Telegram (status Liquid / T_ctrl line)."""
    en = str(lang or "ru").lower().startswith("en")
    s = _normalize_t_ctrl_sensor(sensor)
    if en:
        return {
            "liquid": "liquid",
            "env": "env",
            "chip_avg": "chip avg",
            "chip_max": "chip max",
            "board_max": "board max",
        }.get(s, s)
    return {
        "liquid": "liquid",
        "env": "env",
        "chip_avg": "chip avg",
        "chip_max": "chip max",
        "board_max": "board max",
    }.get(s, s)


def _tg_t_ctrl_from_live(live: dict | None = None) -> tuple[float | None, str]:
    """Selected T_ctrl °C + sensor id (from zone map config)."""
    try:
        zc = get_zone_cfg()
        sens = zc.get("t_ctrl_sensor")
    except Exception:
        sens = "liquid"
    return resolve_t_ctrl(live if isinstance(live, dict) else {}, sens)


def _tg_live_snapshot(
    *,
    force: bool = False,
    online_max_age_sec: float | None = None,
) -> tuple[dict, bool, Exception | None]:
    """
    Live for Telegram — default path NEVER hits the miner.

    Background policy/collector already poll :4028 and fill ``_cache``.
    Bot commands (/status, /miner, most buttons) only read that snapshot so
    getUpdates stays fast (~0.3–1s = Telegram RTT).

    force=True (only Miner «Обновить»): run fetch_live once and refresh cache.

    online: True if last successful poll is recent (see miner_is_online /
    _last_live_ok_ts). No cache or poll too old → offline without probing.
    """
    global _cache, _cache_ts
    # Consider ASIC online if we polled successfully within ~3 control intervals
    # (floor 60s so a slow Peak still counts as online between ticks).
    if online_max_age_sec is None:
        try:
            with _miner_cfg_lock:
                poll = int(POLL_INTERVAL_SEC)
        except Exception:
            poll = 5
        online_max_age_sec = float(max(60, min(300, poll * 3)))

    if force:
        try:
            live = fetch_live()
            with _cache_lock:
                _cache = live
                _cache_ts = time.time()
            return live, True, None
        except Exception as e:
            # fall through to last-good if any
            with _cache_lock:
                cached = dict(_cache) if isinstance(_cache, dict) else None
            if cached and cached.get("ok") and miner_is_online(
                max_age_sec=float(online_max_age_sec), probe=False
            ):
                return cached, True, None
            return {}, False, e

    with _cache_lock:
        cached = dict(_cache) if isinstance(_cache, dict) else None
    if not cached or not cached.get("ok"):
        return {}, False, RuntimeError("no live cache (waiting for background poll)")

    if miner_is_online(max_age_sec=float(online_max_age_sec), probe=False):
        return cached, True, None

    # Cache exists but background polls have been failing → offline, no probe
    return {}, False, RuntimeError("ASIC offline (stale poll)")


def _tg_status_fleet_power(live: dict | None) -> tuple[float | None, float | None, float | None]:
    """
    Fleet totals for Status ⚡️ line (W, TH/s, J/T).
    Today: single configured ASIC. When multi-host lands, sum all live rows here.
    """
    live = live if isinstance(live, dict) else {}
    # Future: sum over fleet snapshots. For now one host = fleet.
    rows = [live] if live.get("ok") or live.get("power") is not None or live.get("hashrate_th") is not None else []
    if not rows and live:
        rows = [live]
    pw_sum = 0.0
    hr_sum = 0.0
    n_pw = 0
    n_hr = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        p = _f(r.get("power"))
        h = _f(r.get("hashrate_th"))
        if p is not None:
            pw_sum += float(p)
            n_pw += 1
        if h is not None:
            hr_sum += float(h)
            n_hr += 1
    pw = pw_sum if n_pw else None
    hr = hr_sum if n_hr else None
    try:
        jt = _sample_eff_jt(pw, hr)
    except Exception:
        jt = None
    return pw, hr, jt


def _tg_status_text(lang: str = "ru") -> str:
    """
    Laconic Status card (no host / online / last_event):

    🏊‍♂️ {project} :: Статус
    —————————————————————
    ⚡️  W · TH/s · J/T   (fleet total)
    💦 Фильтрация: …
    🌡  Температуры: …
    ❄️ Остывание / 🔥 Нагрев …
    policy block
    """
    en = str(lang or "ru").lower().startswith("en")
    proj = get_project_name() or "poolheat"
    title = (
        f"🏊‍♂️ {_tg_html_esc(proj)} :: Status"
        if en
        else f"🏊‍♂️ {_tg_html_esc(proj)} :: Статус"
    )
    sep = "—————————————————————"

    try:
        live, online, err = _tg_live_snapshot()
        if not online:
            raise err or RuntimeError("offline")
    except Exception:
        online = False
        live = {}

    try:
        pol = get_policy_status()
    except Exception:
        pol = {}

    street = _tg_street_c()
    t_ctrl, _ = _tg_t_ctrl_from_live(live if online else {})
    chip = live.get("chip_max") if online else None
    pw, hr, jt = _tg_status_fleet_power(live if online else None)

    if jt is not None and float(jt) > 0:
        power_line = (
            f"⚡️  {_tg_fmt_num(pw, 0)} W  ·  {_tg_fmt_num(hr, 1)} TH/s ·  "
            f"{_tg_fmt_num(jt, 1)} J/T"
        )
    else:
        power_line = (
            f"⚡️  {_tg_fmt_num(pw, 0)} W  ·  {_tg_fmt_num(hr, 1)} TH/s"
        )

    hz = pol.get("heat_zone")
    safety = bool(pol.get("safety_sticky"))
    if not hz and not safety and t_ctrl is not None:
        try:
            zc = get_zone_cfg()
            hz = _place_heat_zone(
                float(t_ctrl),
                float(zc.get("t0", 24)),
                float(zc.get("t1", 26)),
                float(zc.get("t2", 28)),
            )
        except Exception:
            hz = None

    dry = bool(pol.get("dry_run"))
    fs = bool(pol.get("force_stop"))
    preset = _tg_active_preset_name()
    z_lab = _tg_zone_label(hz, safety=safety)
    z_mode = _tg_format_zone_mode(
        _tg_zone_profile_for_status(hz, safety=safety), lang
    )
    policy_block = _tg_policy_block_lines(
        preset=preset, z_lab=z_lab, z_mode=z_mode, dry=dry, fs=fs, lang=lang
    )

    if en:
        temps_h = "🌡  Temperatures:"
        temp_lines = [
            f"Water:    {_tg_fmt_num(t_ctrl, 1)} °C",
            f"Street:  {_tg_fmt_num(street, 1)} °C",
            f"Chips:   {_tg_fmt_num(chip, 1)} °C",
        ]
    else:
        temps_h = "🌡  Температуры:"
        temp_lines = [
            f"Вода:    {_tg_fmt_num(t_ctrl, 1)} °C",
            f"Улица:  {_tg_fmt_num(street, 1)} °C",
            f"Чипы:   {_tg_fmt_num(chip, 1)} °C",
        ]

    lines = [
        title,
        sep,
        power_line,
    ]
    fl_lines = _tg_filtration_status_lines(lang)
    if fl_lines:
        lines.append("")
        lines.extend(fl_lines)
    lines += [
        "",
        temps_h,
        *temp_lines,
    ]

    if online:
        hb_lines = _tg_heat_balance_lines(live, street, lang)
        if hb_lines:
            lines.append("")
            lines.extend(hb_lines)

    if pol.get("override_active"):
        rem = int(pol.get("override_remaining_sec") or 0)
        mm, ss = divmod(max(0, rem), 60)
        lab_ov = "Override" if en else "Override"
        lab_left = "left" if en else "осталось"
        lines.append("")
        lines.append(f"🎛  {lab_ov} {mm}m {ss:02d}s {lab_left}")

    lines.append("")
    lines.extend(policy_block)
    # no last_event — Status stays laconic; Action log has full history
    return "\n".join(lines)


def _tg_status_inline(lang: str = "ru") -> dict:
    """Status card: preset submenu + refresh (no miner I/O)."""
    en = str(lang or "ru").lower().startswith("en")
    pname = _tg_active_preset_name()
    if pname and pname != "—":
        # keep button ≤ ~40 chars (Telegram UI)
        short = pname if len(pname) <= 22 else (pname[:20] + "…")
        preset_l = (
            f"📋 Preset · {short}" if en else f"📋 Пресет · {short}"
        )
    else:
        preset_l = "📋 Preset" if en else "📋 Пресет"
    return {
        "inline_keyboard": [
            [
                {"text": preset_l, "callback_data": "st:preset"},
            ],
            [
                {
                    "text": "🔄 Refresh" if en else "🔄 Обновить",
                    "callback_data": "st:refresh",
                },
            ],
        ]
    }


def _tg_status_preset_inline(lang: str = "ru") -> dict:
    """Submenu: pick zone-map preset to apply."""
    en = str(lang or "ru").lower().startswith("en")
    rows: list[list[dict]] = []
    try:
        data = list_zone_presets()
        active = data.get("active_id")
        presets = data.get("presets") or []
    except Exception:
        active = None
        presets = []
    if not presets:
        rows.append(
            [
                {
                    "text": (
                        "No presets — save in UI"
                        if en
                        else "Нет пресетов — сохраните в UI"
                    ),
                    "callback_data": "st:back",
                }
            ]
        )
    else:
        for p in presets:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            name = str(p.get("name") or pid).strip() or pid
            if len(name) > 28:
                name = name[:26] + "…"
            mark = "· " if pid == active else ""
            # callback_data max 64 bytes
            cb = f"st:preset:{pid}"
            if len(cb.encode("utf-8")) > 64:
                continue
            rows.append([{"text": f"{mark}{name}", "callback_data": cb}])
    rows.append(
        [
            {
                "text": "◀️ Back" if en else "◀️ Назад",
                "callback_data": "st:back",
            }
        ]
    )
    return {"inline_keyboard": rows}


def _tg_status_preset_text(lang: str = "ru") -> str:
    """Header for preset picker submenu."""
    en = str(lang or "ru").lower().startswith("en")
    cur = _tg_active_preset_name()
    if en:
        return (
            f"📋 <b>Preset</b>\n"
            f"Active: <b>{_tg_html_esc(cur)}</b>\n\n"
            f"Choose a zone-map preset:"
        )
    return (
        f"📋 <b>Пресет</b>\n"
        f"Активный: <b>{_tg_html_esc(cur)}</b>\n\n"
        f"Выберите пресет зоны:"
    )


def _tg_send_status(
    chat_id,
    lang: str = "ru",
    *,
    edit_message_id: int | None = None,
    view: str = "root",
) -> None:
    """
    Status card with inline controls.
    view=root → full status + preset/refresh buttons
    view=preset → submenu to apply zone-map preset
    """
    if view == "preset":
        text = _tg_status_preset_text(lang)
        markup = _tg_status_preset_inline(lang)
    else:
        text = _tg_status_text(lang=lang)
        markup = _tg_status_inline(lang)
    if edit_message_id is not None:
        tg_edit_message(
            chat_id,
            edit_message_id,
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    else:
        tg_send_message(
            chat_id,
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )


def _tg_fmt_mac(mac) -> str:
    """AA:BB:CC:DD:EE:FF from raw Whatsminer mac string."""
    s = re.sub(r"[^0-9A-Fa-f]", "", str(mac or ""))
    if len(s) == 12:
        return ":".join(s[i : i + 2].upper() for i in range(0, 12, 2))
    raw = str(mac or "").strip()
    return raw.upper() if raw else "—"


def _tg_pretty_mode(mode) -> str:
    if mode is None or mode == "":
        return "—"
    s = str(mode).strip()
    if s.lower() in ("low", "normal", "high"):
        return s.capitalize()
    return s


def _tg_pretty_work(work, en: bool = True) -> str:
    w = str(work or "").strip().lower()
    if w in ("sleep", "suspend"):
        return "⏸️  Suspend Mining"
    if w in ("resume", "mining"):
        return "▶️  Resume Mining"
    return f"⚙️  {work or '—'}"


def _tg_info_inline(lang: str = "ru") -> dict:
    en = str(lang or "ru").lower().startswith("en")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 Refresh" if en else "🔄 Обновить",
                    "callback_data": "i:refresh",
                },
                {
                    "text": "⛏ Miner" if en else "⛏ Майнер",
                    "callback_data": "i:miner",
                },
            ]
        ]
    }


def _tg_info_text(lang: str = "ru") -> str:
    """Info tab: identity, boards, PSU (like UI #info miner block)."""
    en = str(lang or "ru").lower().startswith("en")
    proj = get_project_name()
    live, online, err = _tg_live_snapshot()
    if not online:
        live = {}

    try:
        ident = get_miner_identity_cached(force=False) or {}
    except Exception:
        ident = {}

    host = (live.get("host") if online else None) or ident.get("host") or f"{HOST_MINER}:{PORT_MINER}"
    mtype = (ident.get("miner_type") or "").strip() or "—"
    mac = _tg_fmt_mac(ident.get("mac"))
    sn = (ident.get("minersn") or "").strip() or "—"
    fw = (ident.get("fw_ver") or "").strip() or "—"
    api_ver = (ident.get("api_ver") or "").strip() or "—"
    platform = (ident.get("platform") or "").strip() or "—"
    chip = (ident.get("chip") or "").strip() or "—"

    lines = [
        f"ℹ️  {'Info' if en else 'Инфо'} · {proj}",
        "————————————",
        "",
        f"{_tg_miner_host_prefix()}{host}",
        ("🟢 online" if online else "🔴 offline"),
        "",
        f"{'Type' if en else 'Модель'}:  {mtype}",
        f"Platform:  {platform}",
        f"Chip:  {chip}",
        f"MAC:  {mac}",
        f"SN:  {sn}",
        f"FW:  {fw}",
        f"API:  {api_ver}",
    ]

    if not online:
        lines.append("")
        lines.append(_fmt_asic_offline_msg(err, lang=lang))
        return "\n".join(lines)

    boards_t = live.get("boards") or []
    upfreq = live.get("upfreq") or []
    id_boards = ident.get("boards") or []
    tagged_th = ident.get("tagged_th") or ident.get("factory_th")
    if tagged_th is not None:
        lines.append("")
        lines.append(
            f"Tagged:  {_tg_fmt_num(tagged_th, 2)} TH/s"
            + (
                f"  ({_tg_fmt_num(ident.get('tagged_ghs') or ident.get('factory_ghs'), 0)} GHS)"
                if (ident.get("tagged_ghs") or ident.get("factory_ghs"))
                else ""
            )
        )
    if boards_t or id_boards:
        lines.append("")
        lines.append(
            "Hashboards · PCB SN · Tagged:"
            if en
            else "Хешплаты · PCB SN · Tagged:"
        )
        n = max(len(boards_t), len(id_boards), 3)
        for i in range(min(n, 4)):
            t = boards_t[i] if i < len(boards_t) else None
            uf = upfreq[i] if i < len(upfreq) else None
            pcb = "—"
            th_s = "—"
            if i < len(id_boards) and isinstance(id_boards[i], dict):
                b = id_boards[i]
                pcb = (b.get("pcb_sn") or "").strip() or "—"
                thv = b.get("tagged_th")
                if thv is None and b.get("tagged_ghs") is not None:
                    try:
                        thv = float(b["tagged_ghs"]) / 1000.0
                    except (TypeError, ValueError):
                        thv = None
                if thv is not None:
                    th_s = f"{_tg_fmt_num(thv, 2)} TH"
            uf_s = "upfreq ✓" if uf and int(uf) else "upfreq …"
            lines.append(
                f"  HB{i}  {_tg_fmt_num(t, 1)} °C · {uf_s} · {pcb} · {th_s}"
            )

    psu_t = live.get("psu_temp")
    psu_fan = live.get("psu_fan")
    psu_vin = live.get("psu_vin")
    psu_iin = live.get("psu_iin")
    psu_model = live.get("psu_model") or ident.get("psu_model")
    powersn = (ident.get("powersn") or "").strip()
    if (
        psu_t is not None
        or psu_fan is not None
        or psu_vin is not None
        or psu_iin is not None
        or psu_model
        or powersn
    ):
        lines.append("")
        lines.append("PSU:")
        if psu_model:
            lines.append(f"  {psu_model}")
        if powersn:
            lines.append(f"  SN  {powersn}")
        bits = []
        if psu_vin is not None:
            bits.append(f"Vin {_tg_fmt_num(psu_vin, 0)} V")
        if psu_iin is not None:
            bits.append(f"Iin {_tg_fmt_num(psu_iin, 2)} A")
        if psu_t is not None:
            bits.append(f"{_tg_fmt_num(psu_t, 0)} °C")
        if psu_fan is not None:
            bits.append(f"{_tg_fmt_num(psu_fan, 0)} rpm")
        if bits:
            lines.append(f"  {' · '.join(bits)}")

    t_ctrl, t_ctrl_sensor = _tg_t_ctrl_from_live(live)
    chip_max = live.get("chip_max")
    env = live.get("env")
    sens_lab = _tg_t_ctrl_sensor_label(t_ctrl_sensor, lang)
    lines.append("")
    # T_ctrl first (zone map); env/chip for context
    if en:
        lines.append(
            f"T_ctrl ({sens_lab}) {_tg_fmt_num(t_ctrl, 1)} °C · "
            f"Env {_tg_fmt_num(env, 1)} °C · Chip {_tg_fmt_num(chip_max, 1)} °C"
        )
    else:
        lines.append(
            f"T_ctrl ({sens_lab}) {_tg_fmt_num(t_ctrl, 1)} °C · "
            f"Env {_tg_fmt_num(env, 1)} °C · Чип {_tg_fmt_num(chip_max, 1)} °C"
        )

    # Firmware errors shown on Miner card only

    return "\n".join(lines)


def _tg_send_info(
    chat_id,
    lang: str = "ru",
    *,
    edit_message_id: int | None = None,
) -> None:
    text = _tg_info_text(lang=lang)
    markup = _tg_info_inline(lang)
    if edit_message_id is not None:
        tg_edit_message(
            chat_id,
            edit_message_id,
            text,
            reply_markup=markup,
            miner_host_emoji=True,
        )
    else:
        tg_send_message(
            chat_id, text, reply_markup=markup, miner_host_emoji=True
        )


def _tg_miner_inline(lang: str = "ru", live: dict | None = None) -> dict:
    """Control panel like UI #miner — Power Mode / Mining Control / Limit / pct."""
    en = str(lang or "ru").lower().startswith("en")
    live = live or {}
    mode = str(live.get("mode_norm") or live.get("mode") or "").strip().lower()
    work = str(live.get("work_measured") or "").strip().lower()
    lim = _f(live.get("power_limit_measured") or live.get("power_limit"))
    pct_set = _f(live.get("power_pct_cmd"))
    # mark current mode/work with ·
    def mlab(v: str, label: str) -> str:
        return f"· {label}" if mode == v else label

    def wlab(is_sleep: bool, label: str) -> str:
        cur_sleep = work in ("sleep", "suspend")
        if is_sleep == cur_sleep and work not in ("", "—", "none"):
            return f"· {label}"
        return label

    low_l = mlab("low", "Low")
    norm_l = mlab("normal", "Normal")
    high_l = mlab("high", "High")
    sus_l = wlab(True, "⏸ Suspend" if en else "⏸ Suspend")
    res_l = wlab(False, "▶️ Resume" if en else "▶️ Resume")
    lim_s = f"{int(lim)} W" if lim is not None else "Limit"
    # mark current power pct (local cmd or estimate)
    pct_cur = None
    if pct_set is not None:
        pct_cur = int(round(float(pct_set)))
    else:
        pw = _f(live.get("power"))
        if pw is not None and lim is not None and lim > 0 and float(pw) > 0:
            pct_cur = int(round(100.0 * float(pw) / float(lim)))

    def plab(p: int) -> str:
        if pct_cur is not None and abs(pct_cur - p) <= 2:
            return f"· {p}%"
        return f"{p}%"

    dry = _tg_dry_run_on()
    if en:
        dry_l = "🧪 Dry Run · ON" if dry else "🧪 Dry Run · OFF"
    else:
        dry_l = "🧪 Dry Run · ВКЛ" if dry else "🧪 Dry Run · ВЫКЛ"

    return {
        "inline_keyboard": [
            [
                {"text": sus_l, "callback_data": "m:work:sleep"},
                {"text": res_l, "callback_data": "m:work:resume"},
            ],
            [
                {"text": low_l, "callback_data": "m:mode:low"},
                {"text": norm_l, "callback_data": "m:mode:normal"},
                {"text": high_l, "callback_data": "m:mode:high"},
            ],
            [
                {"text": "−500 W", "callback_data": "m:limd:-500"},
                {"text": lim_s, "callback_data": "m:refresh"},
                {"text": "+500 W", "callback_data": "m:limd:500"},
            ],
            [
                {"text": plab(50), "callback_data": "m:pct:50"},
                {"text": plab(80), "callback_data": "m:pct:80"},
                {"text": plab(100), "callback_data": "m:pct:100"},
            ],
            [
                {"text": dry_l, "callback_data": "m:dry:toggle"},
            ],
            [
                {
                    "text": "🔁 Reboot ASIC" if en else "🔁 Reboot ASIC",
                    "callback_data": "m:reboot",
                },
                {
                    "text": "🔃 Restart miner" if en else "🔃 Restart miner",
                    "callback_data": "m:restart",
                },
            ],
            [
                {
                    "text": "Pools" if en else "Пулы",
                    "callback_data": "m:pools",
                    "icon_custom_emoji_id": _TG_POOLS_BTN_EMOJI_ID,
                },
                {
                    "text": "🔄 Refresh" if en else "🔄 Обновить",
                    "callback_data": "m:refresh",
                },
                {
                    "text": "ℹ️ Info" if en else "ℹ️ Инфо",
                    "callback_data": "m:info",
                },
            ],
        ]
    }


def _tg_fmt_ts_local(ts) -> str:
    """ISO / epoch → DD.MM.YYYY HH:MM:SS for TG cards."""
    if ts is None or ts == "":
        return "—"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(float(ts))
        else:
            s = str(ts).strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                # "2026-08-03T19:55:25" already handled; other formats fall through
                return str(ts)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(ts)


def _tg_fmt_dur_sec(raw) -> str:
    """
    Seconds (int/float) → compact RU-style duration for TG.
    1д 8ч · 5ч 12м · 42м · 15с
    """
    if raw is None or raw == "":
        return "—"
    try:
        sec = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if sec < 0 or sec != sec:  # NaN
        return "—"
    s = int(sec)
    d = s // 86400
    s %= 86400
    h = s // 3600
    s %= 3600
    m = s // 60
    s %= 60
    if d > 0:
        return f"{d}д {h}ч"
    if h > 0:
        return f"{h}ч {m}м"
    if m > 0:
        return f"{m}м"
    return f"{s}с"


def _tg_miner_text(lang: str = "ru", live: dict | None = None, online: bool = True, err=None) -> str:
    """Miner control card — laconic measured + last write (UI #miner)."""
    en = str(lang or "ru").lower().startswith("en")
    if live is None:
        # Never force-poll here — caller passes force via _tg_send_miner
        live, online, err = _tg_live_snapshot(force=False)
        if not online:
            live = {}

    host = live.get("host") or f"{HOST_MINER}:{PORT_MINER}"
    up_m = _tg_fmt_dur_sec(live.get("uptime")) if online else "—"
    if online:
        link = f"🟢 online · uptime {up_m}"
    else:
        link = "🔴 offline"
    # HTML host line (parse_mode=HTML in _tg_send_miner)
    lines = [
        _tg_miner_host_line_html(str(host)),
        link,
    ]
    if not online:
        lines.append("")
        lines.append(_tg_html_esc(_fmt_asic_offline_msg(err, lang=lang)))
        return "\n".join(lines)

    pw = live.get("power")
    hr = live.get("hashrate_th")
    mode = _tg_pretty_mode(live.get("mode") or live.get("mode_norm"))
    work = _tg_pretty_work(live.get("work_measured"), en)
    lim = live.get("power_limit_measured") or live.get("power_limit")
    pct_set = live.get("power_pct_cmd")
    pct_meas = live.get("power_pct_reported")
    freq = live.get("freq_avg")

    dry = _tg_dry_run_on()
    # Spacing tuned in Telegram (proportional font) — keep exact gaps:
    # Power Mode:        Low
    # Mining Control:   ▶️  Resume Mining
    # Статус:                  Тюнинг
    # Power Limit :        2000 W
    # Power pct :           set 100%  |  meas 0%
    # Dry Run:                вкл.
    # Force Stop:           выкл.
    if en:
        dry_s = "on." if dry else "off."
        fs_s = "on." if get_force_stop() else "off."
        st_lab = "Status:"
    else:
        dry_s = "вкл." if dry else "выкл."
        fs_s = "вкл." if get_force_stop() else "выкл."
        st_lab = "Статус:"

    # lifecycle: starting / stopping / tuning / running / stopped
    try:
        rs = mining_run_status(live)
        run_st = rs.get("label_en") if en else rs.get("label_ru")
    except Exception:
        run_st = live.get("run_status_en" if en else "run_status_ru") or "—"
    # Elapsed = mining session time (summary.Elapsed)
    el_s = _tg_fmt_dur_sec(live.get("elapsed"))
    if el_s and el_s != "—":
        run_st = f"{run_st} · {el_s}"

    try:
        eff_m = _sample_eff_jt(pw, hr)
    except Exception:
        eff_m = None
    if eff_m is not None and float(eff_m) > 0:
        miner_pw = (
            f"⚡️  {_tg_fmt_num(pw, 0)} W  ·  {_tg_fmt_num(hr, 1)} TH/s  ·  "
            f"{_tg_fmt_num(eff_m, 1)} J/T"
        )
    else:
        miner_pw = f"⚡️  {_tg_fmt_num(pw, 0)} W  ·  {_tg_fmt_num(hr, 1)} TH/s"

    lines += [
        "",
        miner_pw,
        f"Freq  {_tg_fmt_num(freq, 0)} MHz",
        "",
        f"Power Mode:        {mode}",
        f"Mining Control:   {work}",
        f"{st_lab}{' ' * 18}{run_st}",
        f"Power Limit :        {_tg_fmt_num(lim, 0)} W",
        (
            f"Power pct :           set {_tg_fmt_num(pct_set, 0)}%  |  "
            f"meas {_tg_fmt_num(pct_meas, 0)}%"
        ),
        f"Dry Run:                {dry_s}",
        f"Force Stop:           {fs_s}",
    ]

    # Firmware get_error_code (e.g. 2000, 2010 All pools disabled)
    err_lines = _tg_active_errors_lines(live, lang)
    if err_lines:
        lines.append("")
        lines.extend(err_lines)

    lw = live.get("last_write") or {}
    if isinstance(lw, dict) and (lw.get("ts") or lw.get("action") is not None):
        ok = lw.get("ok")
        action = lw.get("action")
        value = lw.get("value")
        cmd = f"{action}={value}" if action is not None else "—"
        if ok:
            mark = "✅"
            lines.append("")
            lines.append(f"last write: {_tg_fmt_ts_local(lw.get('ts'))}")
            lines.append(f"{mark} {_tg_html_esc(cmd)}")
        else:
            # 🚫 FAIL working=sleep: can't access write cmd
            lines.append("")
            lines.extend(
                _tg_status_fail_lines(
                    "",
                    lang,
                    cmd=cmd,
                    reply=str(lw.get("error") or "—"),
                )
            )

    return "\n".join(lines)


def _tg_send_miner(
    chat_id,
    lang: str = "ru",
    *,
    edit_message_id: int | None = None,
    force_refresh: bool = False,
) -> None:
    """
    Miner control card. force_refresh=True only for «Обновить» — hits ASIC;
    all other opens use last background poll (instant).
    """
    live, online, err = _tg_live_snapshot(force=bool(force_refresh))
    if not online:
        live = {}
    text = _tg_miner_text(lang=lang, live=live, online=online, err=err)
    markup = _tg_miner_inline(lang, live if online else None)
    # HTML: host custom emoji + control-error custom emoji
    if edit_message_id is not None:
        tg_edit_message(
            chat_id,
            edit_message_id,
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    else:
        tg_send_message(
            chat_id, text, reply_markup=markup, parse_mode="HTML"
        )


def _tg_apply_miner_write(action: str, value, lang: str = "ru") -> str:
    """apply_set wrapper → short OK / error string for callback toast."""
    en = str(lang or "ru").lower().startswith("en")
    try:
        out = apply_set(action, value, DEFAULT_API_PASSWORD)
        if isinstance(out, dict) and out.get("skipped"):
            if action in ("power_limit", "set_power_limit", "adjust_power_limit"):
                return (
                    f"Power Limit already {value} W"
                    if en
                    else f"Power Limit уже {value} W"
                )
            if action == "power_pct":
                return (
                    f"Power pct already {value}%"
                    if en
                    else f"Power pct уже {value}%"
                )
            if action == "mode":
                return (
                    f"Power Mode already {str(value).upper()}"
                    if en
                    else f"Power Mode уже {str(value).upper()}"
                )
            if action in ("working", "working_mode", "work", "mining"):
                v = str(value).lower()
                if v in ("sleep", "suspend"):
                    return "Mining Control already Suspend" if en else "Уже Suspend"
                return "Mining Control already Resume" if en else "Уже Resume"
            return "✅ already set" if en else "✅ уже установлено"
        if action == "mode":
            return f"Power Mode: {str(value).upper()}"
        if action in ("working", "working_mode", "work", "mining"):
            v = str(value).lower()
            if v in ("sleep", "suspend"):
                return "Mining Control: Suspend"
            return "Mining Control: Resume"
        if action in ("power_limit", "set_power_limit", "adjust_power_limit"):
            return f"Power Limit: {value} W"
        if action == "power_pct":
            return f"Power pct: {value}%"
        if action in ("reboot", "reboot_asic", "system_reboot"):
            return (
                "🔁 Reboot ASIC sent · offline a few min"
                if en
                else "🔁 Reboot ASIC отправлен · offline несколько мин"
            )
        if action in ("restart", "restart_miner", "restart_btminer", "btminer_restart"):
            return (
                "🔃 Restart miner sent · upfreq…"
                if en
                else "🔃 Restart miner отправлен · upfreq…"
            )
        warn = (out or {}).get("warning") if isinstance(out, dict) else None
        if warn:
            return f"✅ OK · {warn}"
        return "✅ OK" if en else "✅ OK"
    except Exception as e:
        return f"❌ {e}"


def _tg_dry_run_inline(lang: str = "ru", dry: bool | None = None) -> dict:
    en = str(lang or "ru").lower().startswith("en")
    if dry is None:
        dry = _tg_dry_run_on()
    if en:
        on_l = "● ON" if dry else "ON"
        off_l = "● OFF" if not dry else "OFF"
    else:
        on_l = "● ВКЛ" if dry else "ВКЛ"
        off_l = "● ВЫКЛ" if not dry else "ВЫКЛ"
    return {
        "inline_keyboard": [
            [
                {"text": f"🧪 {on_l}", "callback_data": "d:on"},
                {"text": f"⚡️ {off_l}", "callback_data": "d:off"},
            ],
            [
                {
                    "text": "🔄 Refresh" if en else "🔄 Обновить",
                    "callback_data": "d:refresh",
                },
            ],
        ]
    }


def _tg_dry_run_text(lang: str = "ru", dry: bool | None = None) -> str:
    en = str(lang or "ru").lower().startswith("en")
    if dry is None:
        dry = _tg_dry_run_on()
    if en:
        if dry:
            return (
                "🧪 Dry Run · ON\n"
                "————————————\n"
                "Heat zones: ignored (keep current mode)\n"
                "Safety Critical (chip): still writes\n"
                "Manual Miner controls: still work\n"
                "\n"
                "Turn OFF → zone auto writes to ASIC"
            )
        return (
            "🧪 Dry Run · OFF\n"
            "————————————\n"
            "Heat zones: auto write to ASIC\n"
            "Safety Critical (chip): writes\n"
            "\n"
            "Turn ON → keep current mode · zones preview only"
        )
    if dry:
        return (
            "🧪 Dry Run · ВКЛ\n"
            "————————————\n"
            "Зоны: игнорируются (режим как есть)\n"
            "Safety Critical (чип): пишет\n"
            "Ручное управление Майнер: работает\n"
            "\n"
            "ВЫКЛ → авто-зоны пишут на ASIC"
        )
    return (
        "🧪 Dry Run · ВЫКЛ\n"
        "————————————\n"
        "Зоны: авто-запись на ASIC\n"
        "Safety Critical (чип): пишет\n"
        "\n"
        "ВКЛ → режим как есть · зоны только preview"
    )


def _tg_set_dry_run(on: bool, lang: str = "ru") -> str:
    en = str(lang or "ru").lower().startswith("en")
    try:
        apply_miner_settings(dry_run=bool(on), persist=True)
        if on:
            return (
                "✅ Dry Run ON · manual mode"
                if en
                else "✅ Dry Run ВКЛ · ручной режим"
            )
        return (
            "⚠️ Dry Run OFF · zone auto ON"
            if en
            else "⚠️ Dry Run ВЫКЛ · авто-зоны ВКЛ"
        )
    except Exception as e:
        return f"❌ Dry Run: {e}"


def _tg_send_dry_run(
    chat_id,
    lang: str = "ru",
    *,
    edit_message_id: int | None = None,
    note: str | None = None,
    refresh_keyboard: bool = False,
) -> None:
    dry = _tg_dry_run_on()
    text = _tg_dry_run_text(lang, dry)
    if note:
        text = f"{note}\n\n{text}"
    markup = _tg_dry_run_inline(lang, dry)
    if edit_message_id is not None:
        tg_edit_message(chat_id, edit_message_id, text, reply_markup=markup)
    else:
        tg_send_message(chat_id, text, reply_markup=markup)
    if refresh_keyboard:
        # reply keyboard label shows · ON/OFF
        tg_send_message(
            chat_id,
            "⌨️ " + ("Menu updated" if str(lang).lower().startswith("en") else "Меню обновлено"),
            reply_markup=_tg_main_keyboard(lang, chat_id),
        )


def _tg_commands_help(lang: str = "ru") -> str:
    """Line-by-line command list with short descriptions (/help, /start)."""
    en = str(lang or "ru").lower().startswith("en")
    if en:
        return (
            "Available commands:\n"
            "/status — heat status\n"
            "/miner — control · info · pools\n"
            "/info — ASIC info\n"
            "/pools — mining pools\n"
            "/dry_run [on|off] — Dry Run\n"
            "/mode low|normal|high\n"
            "/limit <W> — power limit\n"
            "/pct <0-100> — power pct\n"
            "/reboot_asic — full device reboot\n"
            "/restart_miner — restart btminer\n"
            "/settings — settings hub\n"
            "/update — check / install software update\n"
            "/profile — language & notifies\n"
            "/events — event log (zones · writes)\n"
            "\n"
            "Force Stop (no arg = toggle)\n"
            "/force_stop [on|off] — emergency stop\n"
            "⏹ Stop mining · ▶️ Continue mining\n"
            "/filtration [on|off] — pump filter (OFF locked while mining unless allowed)\n"
            "/lang_ru — Russian\n"
            "/lang_en — English\n"
            "\n"
            "Notifications (no arg = toggle)\n"
            "/notify_zone [on|off] — zones\n"
            "/notify_safety [on|off] — safety\n"
            "/notify_offline [on|off] — offline ASIC\n"
            "/notify_events [on|off] — event writes\n"
            "/notify_all [on|off] — all notifications"
        )
    return (
        "Доступные команды:\n"
        "/status — статус (тепло)\n"
        "/miner — управление · инфо · пулы\n"
        "/info — инфо ASIC\n"
        "/pools — пулы\n"
        "/dry_run [on|off] — Dry Run\n"
        "/mode low|normal|high\n"
        "/limit <W> — power limit\n"
        "/pct <0-100> — power pct\n"
        "/reboot_asic — полный reboot ASIC\n"
        "/restart_miner — restart btminer\n"
        "/settings — настройки бота\n"
        "/update — проверка / установка обновления\n"
        "/profile — язык и уведомления\n"
        "/events — журнал событий (зоны · записи)\n"
        "\n"
        "Force Stop (без arg = переключить)\n"
        "/force_stop [on|off] — экстренная остановка\n"
        "⏹ Остановить майнинг · ▶️ Продолжить майнинг\n"
        "/filtration [on|off] — фильтрация (ВЫКЛ при майнинге — если не разрешено)\n"
        "/lang_ru — русский\n"
        "/lang_en — English\n"
        "\n"
        "Уведомления (без arg = переключить)\n"
        "/notify_zone [on|off] — зоны\n"
        "/notify_safety [on|off] — safety\n"
        "/notify_offline [on|off] — offline ASIC\n"
        "/notify_events [on|off] — события / записи\n"
        "/notify_all [on|off] — все уведомления"
    )


def _tg_prefs_text(
    chat_id, prefs: dict | None = None, *, view: str = "root"
) -> str:
    """Profile card text (HTML parse_mode — <b> headers)."""
    p = prefs or _tg_get_chat_prefs(chat_id)
    en = str(p.get("lang") or "ru").lower().startswith("en")
    on = (lambda v: "on." if v else "off.") if en else (lambda v: "вкл." if v else "выкл.")
    lang_label = "English" if en else "Русский"
    v = str(view or "root").lower()
    expanded_notify = v.startswith("notif")
    expanded_sec = v.startswith("sec") or v in ("menu", "sections", "menu_sec")

    def _sec(key: str) -> str:
        return on(p.get(key, True))

    if en:
        n_head = (
            "🔔 <b>Notifications</b> · edit below"
            if expanded_notify
            else "🔔 <b>Notifications</b>"
        )
        s_head = (
            "📱 <b>Menu sections</b> · edit below"
            if expanded_sec
            else "📱 <b>Menu sections</b>"
        )
        return (
            f"👤 Profile (chat {chat_id})\n"
            f"————————————\n"
            f"🌐 Language: {lang_label}\n"
            f"\n"
            f"{n_head}\n"
            f"Zone notifications: {on(p.get('notify_zone'))}\n"
            f"Safety notifications: {on(p.get('notify_safety'))}\n"
            f"Offline notifications: {on(p.get('notify_offline'))}\n"
            f"Events notifications: {on(p.get('notify_events'))}\n"
            f"\n"
            f"{s_head}\n"
            f"Miner: {_sec('show_miner')}\n"
            f"Events: {_sec('show_policy')}\n"
            f"Force Stop: {_sec('show_force_stop')}\n"
            f"Filtration: {_sec('show_filtration')}\n"
            f"Settings: {_sec('show_settings')}\n"
            f"Help: {_sec('show_help')}\n"
            f"\n"
            f"Control: {on(p.get('commands_en'))}\n"
            f"Force Stop confirm: {on(p.get('confirm_force_stop', True))}"
        )
    n_head = (
        "🔔 <b>Уведомления</b> · настройте ниже"
        if expanded_notify
        else "🔔 <b>Уведомления</b>"
    )
    s_head = (
        "📱 <b>Разделы меню</b> · настройте ниже"
        if expanded_sec
        else "📱 <b>Разделы меню</b>"
    )
    return (
        f"👤 Профайл (chat {chat_id})\n"
        f"————————————\n"
        f"🌐 Язык: {lang_label}\n"
        f"\n"
        f"{n_head}\n"
        f"Уведомления зоны: {on(p.get('notify_zone'))}\n"
        f"Уведомления Safety: {on(p.get('notify_safety'))}\n"
        f"Уведомления Offline: {on(p.get('notify_offline'))}\n"
        f"Уведомления событий: {on(p.get('notify_events'))}\n"
        f"\n"
        f"{s_head}\n"
        f"Майнер: {_sec('show_miner')}\n"
        f"События: {_sec('show_policy')}\n"
        f"Force Stop: {_sec('show_force_stop')}\n"
        f"Фильтрация: {_sec('show_filtration')}\n"
        f"Настройки: {_sec('show_settings')}\n"
        f"Справка: {_sec('show_help')}\n"
        f"\n"
        f"Управление: {on(p.get('commands_en'))}\n"
        f"Подтверждение Force Stop: {on(p.get('confirm_force_stop', True))}"
    )


def _tg_parse_onoff(s: str) -> bool | None:
    v = str(s or "").strip().lower()
    if v in ("1", "on", "true", "yes", "вкл", "да", "enable", "enabled"):
        return True
    if v in ("0", "off", "false", "no", "выкл", "нет", "disable", "disabled"):
        return False
    return None


def _tg_apply_notify_cmd(chat_id, what: str, arg: str | None, prefs: dict) -> dict:
    """
    what: zone|safety|offline|events|all|commands|confirm_fs
    arg: on|off|None (None = toggle)
    """
    key_map = {
        "zone": "notify_zone",
        "safety": "notify_safety",
        "offline": "notify_offline",
        "events": "notify_events",
        "event": "notify_events",
        "commands": "commands_en",
        "cmd": "commands_en",
        "confirm_fs": "confirm_force_stop",
        "confirm_force_stop": "confirm_force_stop",
    }
    onoff = _tg_parse_onoff(arg) if arg else None
    if what in ("all", "*"):
        if onoff is None:
            # toggle all based on majority / zone as reference
            onoff = not bool(prefs.get("notify_zone", True))
        return _tg_set_chat_prefs(
            chat_id,
            notify_zone=onoff,
            notify_safety=onoff,
            notify_offline=onoff,
            notify_events=onoff,
        )
    pk = key_map.get(what)
    if not pk:
        return prefs
    if onoff is None:
        onoff = not bool(prefs.get(pk, True))
    return _tg_set_chat_prefs(chat_id, **{pk: onoff})


def _tg_send_profile(
    chat_id,
    prefs: dict | None = None,
    *,
    edit_message_id: int | None = None,
    view: str = "root",
) -> None:
    """Personal prefs: language + notifications + control kill-switch."""
    p = prefs or _tg_get_chat_prefs(chat_id)
    text = _tg_prefs_text(chat_id, p, view=view)
    markup = _tg_settings_inline(chat_id, p, view=view)
    if edit_message_id is not None:
        ok = tg_edit_message(
            chat_id, edit_message_id, text, reply_markup=markup, parse_mode="HTML"
        )
        if not ok:
            # fallback: send a fresh card if edit rejected
            tg_send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    else:
        tg_send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


# Back-compat alias (older call sites)
def _tg_send_settings(
    chat_id, prefs: dict | None = None, *, edit_message_id: int | None = None
) -> None:
    _tg_send_profile(chat_id, prefs, edit_message_id=edit_message_id)


def _tg_bot_settings_text(lang: str = "ru") -> str:
    en = str(lang or "ru").lower().startswith("en")
    ver = get_app_version()
    if en:
        return (
            "⚙️ Settings\n"
            "————————————\n"
            f"Version: {ver}\n"
            "\n"
            "Choose a section:"
        )
    return (
        "⚙️ Настройки\n"
        "————————————\n"
        f"Версия: {ver}\n"
        "\n"
        "Выберите раздел:"
    )


def _tg_bot_settings_inline(lang: str = "ru") -> dict:
    en = str(lang or "ru").lower().startswith("en")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 Update" if en else "🔄 Обновление",
                    "callback_data": "cfg:update",
                }
            ],
            [
                {
                    "text": "👤 Profile" if en else "👤 Профайл",
                    "callback_data": "cfg:profile",
                }
            ],
            [
                {
                    "text": "🏠 Menu" if en else "🏠 Меню",
                    "callback_data": "cfg:menu",
                }
            ],
        ]
    }


def _tg_send_bot_settings(
    chat_id, lang: str = "ru", *, edit_message_id: int | None = None
) -> None:
    text = _tg_bot_settings_text(lang)
    markup = _tg_bot_settings_inline(lang)
    if edit_message_id is not None:
        tg_edit_message(chat_id, edit_message_id, text, reply_markup=markup)
    else:
        tg_send_message(chat_id, text, reply_markup=markup)


def _tg_update_status_phrase(st: str | None, en: bool) -> str:
    st = str(st or "unknown")
    if en:
        return {
            "up_to_date": "up to date",
            "update_available": "update available",
            "local_ahead": "local ahead",
            "branch_only": "branch only",
            "error": "check error",
            "unknown": "not checked yet",
        }.get(st, st)
    return {
        "up_to_date": "актуально",
        "update_available": "есть обновление",
        "local_ahead": "локально новее",
        "branch_only": "только ветка",
        "error": "ошибка проверки",
        "unknown": "ещё не проверяли",
    }.get(st, st)


def _tg_update_text(lang: str = "ru", check: dict | None = None) -> str:
    """
    Exact spacing (RU, spaces after colon — tuned in Telegram):

    Установлено:  0.3.6

    Репозиторий:  0.3.6
    Статус:               актуально
    Источник:         release
    Tag:                     v0.3.6
    Commit:            1090f627c4b1
    Проверка:        04.08.2026 10:20:54
    """
    en = str(lang or "ru").lower().startswith("en")
    cur = get_app_version()
    chk = check if isinstance(check, dict) else None
    busy = bool(_update_state.get("busy"))
    if chk is None:
        last = _update_state.get("last_check")
        chk = last if isinstance(last, dict) else None

    # Fixed spaces after ":" — do not smart-align (Telegram proportional font)
    SP_RU = {
        "Установлено": 2,
        "Репозиторий": 2,
        "Статус": 15,
        "Источник": 9,
        "Tag": 21,
        "Commit": 12,
        "Проверка": 8,
    }
    SP_EN = {
        "Installed": 2,
        "Repository": 2,
        "Status": 15,
        "Source": 9,
        "Tag": 21,
        "Commit": 12,
        "Checked": 8,
    }

    def row(label: str, value: str, table: dict) -> str:
        n = int(table.get(label, 2))
        return f"{label}:{' ' * n}{value}"

    sp = SP_EN if en else SP_RU
    lines: list[str] = []
    if en:
        lines.append("🔄 Update")
        lines.append("————————————")
        lines.append(row("Installed", cur, sp))
    else:
        lines.append("🔄 Обновление")
        lines.append("————————————")
        lines.append(row("Установлено", cur, sp))

    if busy:
        lines.append("")
        lines.append("⏳ " + ("install in progress…" if en else "установка…"))

    if not chk:
        lines.append("")
        lines.append(
            "Press «Check» to query GitHub."
            if en
            else "Нажмите «Проверить» для запроса к GitHub."
        )
        return "\n".join(lines)

    latest = chk.get("latest_version") or "—"
    st = _tg_update_status_phrase(chk.get("status"), en)
    lines.append("")
    if en:
        lines.append(row("Repository", str(latest), sp))
        lines.append(row("Status", st, sp))
        if chk.get("source"):
            lines.append(row("Source", str(chk.get("source")), sp))
        if chk.get("tag"):
            lines.append(row("Tag", str(chk.get("tag")), sp))
        if chk.get("commit_sha"):
            lines.append(row("Commit", str(chk.get("commit_sha")), sp))
        if chk.get("checked_at"):
            lines.append(row("Checked", _tg_fmt_ts_local(chk.get("checked_at")), sp))
    else:
        lines.append(row("Репозиторий", str(latest), sp))
        lines.append(row("Статус", st, sp))
        if chk.get("source"):
            lines.append(row("Источник", str(chk.get("source")), sp))
        if chk.get("tag"):
            lines.append(row("Tag", str(chk.get("tag")), sp))
        if chk.get("commit_sha"):
            lines.append(row("Commit", str(chk.get("commit_sha")), sp))
        if chk.get("checked_at"):
            lines.append(row("Проверка", _tg_fmt_ts_local(chk.get("checked_at")), sp))

    if chk.get("error"):
        lines.append("")
        lines.append(f"❌ {chk.get('error')}")
    # no release notes body in TG
    if chk.get("status") == "update_available":
        lines.append("")
        lines.append(
            "Install will replace serve.py + UI, then restart."
            if en
            else "Установка заменит serve.py + UI и перезапустит сервис."
        )
    return "\n".join(lines)


def _tg_update_inline(lang: str = "ru", check: dict | None = None) -> dict:
    en = str(lang or "ru").lower().startswith("en")
    chk = check if isinstance(check, dict) else None
    busy = bool(_update_state.get("busy"))
    if chk is None:
        last = _update_state.get("last_check")
        chk = last if isinstance(last, dict) else {}
    st = str((chk or {}).get("status") or "")
    can_install = (not busy) and st in (
        "update_available",
        "branch_only",
        "up_to_date",
        "local_ahead",
    )
    # Always offer install when we have a check result (reinstall / branch)
    # but hide while busy
    rows: list[list[dict]] = [
        [
            {
                "text": (
                    "🔍 Check again" if en else "🔍 Проверить"
                )
                if chk
                else ("🔍 Check for updates" if en else "🔍 Проверить обновления"),
                "callback_data": "cfg:update:check",
            }
        ]
    ]
    if can_install or (not busy and st == "update_available"):
        label = "⬇️ Install" if en else "⬇️ Установить"
        if st == "update_available":
            lv = (chk or {}).get("latest_version")
            if lv:
                label = (
                    f"⬇️ Install {lv}" if en else f"⬇️ Установить {lv}"
                )
        rows.append([{"text": label, "callback_data": "cfg:update:install"}])
    elif busy:
        rows.append(
            [
                {
                    "text": "⏳ Working…" if en else "⏳ Идёт установка…",
                    "callback_data": "cfg:update:refresh",
                }
            ]
        )
    rows.append(
        [
            {
                "text": "🔄 Refresh" if en else "🔄 Обновить",
                "callback_data": "cfg:update:refresh",
            },
            {
                "text": "◀️ Back" if en else "◀️ Назад",
                "callback_data": "cfg:home",
            },
        ]
    )
    return {"inline_keyboard": rows}


def _tg_update_confirm_inline(lang: str = "ru") -> dict:
    en = str(lang or "ru").lower().startswith("en")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Yes, install" if en else "✅ Да, установить",
                    "callback_data": "cfg:update:install_yes",
                }
            ],
            [
                {
                    "text": "❌ Cancel" if en else "❌ Отмена",
                    "callback_data": "cfg:update",
                }
            ],
        ]
    }


def _tg_send_update(
    chat_id,
    lang: str = "ru",
    *,
    edit_message_id: int | None = None,
    check: dict | None = None,
) -> None:
    text = _tg_update_text(lang, check=check)
    markup = _tg_update_inline(lang, check=check)
    if edit_message_id is not None:
        tg_edit_message(chat_id, edit_message_id, text, reply_markup=markup)
    else:
        tg_send_message(chat_id, text, reply_markup=markup)


def _tg_handle_callback(cq: dict) -> None:
    """Inline button presses on settings screen."""
    data = str(cq.get("data") or "")
    cq_id = str(cq.get("id") or "")
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    mid = msg.get("message_id")
    # ALWAYS answer soon — otherwise Telegram shows a spinning clock.
    # Each branch answers (with optional toast); do not answer here first.
    if not cq_id:
        return
    if chat_id is None:
        tg_answer_callback(cq_id)
        return
    if not _tg_chat_allowed(chat_id):
        tg_answer_callback(cq_id, "⛔ not allowed", alert=True)
        return

    try:
        prefs = _tg_ensure_chat_prefs(chat_id)
        en = str(prefs.get("lang") or "ru").lower().startswith("en")
        lang = prefs.get("lang") or "ru"
        parts = data.split(":")
        # fs:yes:1 | fs:yes:0 | fs:no — Force Stop confirm (profile confirm_force_stop)
        if parts and parts[0] == "fs":
            sub = parts[1] if len(parts) >= 2 else ""
            fu = cq.get("from") if isinstance(cq.get("from"), dict) else None
            if sub == "no":
                # no log — mode did not change
                tg_answer_callback(cq_id, "Cancel" if en else "Отмена")
                try:
                    tg_edit_message(
                        chat_id,
                        mid,
                        "❌ Force Stop cancelled" if en else "❌ Force Stop отменён",
                        reply_markup={"inline_keyboard": []},
                    )
                except Exception:
                    pass
                return
            if sub == "yes":
                want = True
                if len(parts) >= 3:
                    want = parts[2] not in ("0", "off", "false", "no")
                tg_answer_callback(cq_id, "OK")
                try:
                    tg_edit_message(
                        chat_id,
                        mid,
                        ("Applying…" if en else "Применяю…"),
                        reply_markup={"inline_keyboard": []},
                    )
                except Exception:
                    pass
                _tg_apply_force_stop_result(
                    chat_id, bool(want), lang, from_user=fu, log=True
                )
                return
            tg_answer_callback(cq_id)
            return
        # cfg: — bot Settings hub + Update subsection
        # cfg:home | cfg:menu | cfg:profile | cfg:update | cfg:update:check | …
        if len(parts) >= 2 and parts[0] == "cfg":
            action = parts[1]
            if action in ("home", "settings"):
                tg_answer_callback(cq_id, "OK")
                _tg_send_bot_settings(chat_id, lang, edit_message_id=mid)
                return
            if action == "menu":
                tg_answer_callback(cq_id)
                tg_send_message(
                    chat_id,
                    "🏠 " + ("Main menu" if en else "Главное меню"),
                    reply_markup=_tg_main_keyboard(lang, chat_id),
                )
                return
            if action == "profile":
                tg_answer_callback(cq_id, "OK")
                _tg_send_profile(chat_id, prefs, edit_message_id=mid)
                return
            if action == "update":
                sub = parts[2] if len(parts) >= 3 else ""
                if not sub:
                    tg_answer_callback(cq_id, "OK")
                    _tg_send_update(chat_id, lang, edit_message_id=mid)
                    return
                if sub == "refresh":
                    tg_answer_callback(cq_id, "OK")
                    _tg_send_update(chat_id, lang, edit_message_id=mid)
                    return
                if sub == "check":
                    tg_answer_callback(
                        cq_id,
                        "Checking…" if en else "Проверка…",
                    )
                    # show spinner-ish state
                    try:
                        tg_edit_message(
                            chat_id,
                            mid,
                            (
                                "🔄 Update\n————————————\n⏳ Checking GitHub…"
                                if en
                                else "🔄 Обновление\n————————————\n⏳ Проверка GitHub…"
                            ),
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "◀️ Back" if en else "◀️ Назад",
                                            "callback_data": "cfg:home",
                                        }
                                    ]
                                ]
                            },
                        )
                    except Exception:
                        pass
                    try:
                        chk = check_github_update()
                    except Exception as e:
                        chk = {
                            "ok": False,
                            "status": "error",
                            "error": str(e),
                            "current_version": get_app_version(),
                            "checked_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    _tg_send_update(chat_id, lang, edit_message_id=mid, check=chk)
                    return
                if sub == "install":
                    # confirmation step
                    tg_answer_callback(cq_id)
                    with _update_lock:
                        last = _update_state.get("last_check") or {}
                    lv = (last or {}).get("latest_version") or (last or {}).get("tag") or GITHUB_BRANCH
                    conf = (
                        f"⬇️ Install update?\n"
                        f"————————————\n"
                        f"Target: {lv}\n"
                        f"serve.py + UI will be replaced.\n"
                        f"Service restarts after install."
                        if en
                        else f"⬇️ Установить обновление?\n"
                        f"————————————\n"
                        f"Цель: {lv}\n"
                        f"Будут заменены serve.py + UI.\n"
                        f"После установки сервис перезапустится."
                    )
                    tg_edit_message(
                        chat_id,
                        mid,
                        conf,
                        reply_markup=_tg_update_confirm_inline(lang),
                    )
                    return
                if sub == "install_yes":
                    with _update_lock:
                        if _update_state.get("busy"):
                            tg_answer_callback(
                                cq_id,
                                "Already running" if en else "Уже идёт",
                                alert=True,
                            )
                            return
                    tg_answer_callback(
                        cq_id,
                        "Installing…" if en else "Установка…",
                    )
                    try:
                        tg_edit_message(
                            chat_id,
                            mid,
                            (
                                "⬇️ Installing from GitHub…\n"
                                "Bot may go quiet for ~30–60s, then come back."
                                if en
                                else "⬇️ Установка с GitHub…\n"
                                "Бот может замолчать на ~30–60 с, затем вернётся."
                            ),
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "🏠 Menu" if en else "🏠 Меню",
                                            "callback_data": "cfg:menu",
                                        }
                                    ]
                                ]
                            },
                        )
                    except Exception:
                        pass

                    def _do_install() -> None:
                        try:
                            result = apply_github_update(None)
                            ok = bool(result.get("ok"))
                            if ok:
                                to_v = result.get("to_version") or get_app_version() or "?"
                                from_v = result.get("from_version")
                                # survive process kill — notify after restart
                                _queue_update_restart_notify(
                                    chat_id,
                                    lang,
                                    from_version=str(from_v) if from_v else None,
                                    to_version=str(to_v),
                                    source="telegram",
                                )
                                msg = (
                                    f"✅ Installed {to_v}\n"
                                    f"Restarting…"
                                    if en
                                    else f"✅ Установлено {to_v}\n"
                                    f"Перезапуск…"
                                )
                            else:
                                msg = (
                                    f"❌ Install failed: {result.get('error') or result}"
                                    if en
                                    else f"❌ Ошибка установки: {result.get('error') or result}"
                                )
                            try:
                                tg_send_message(
                                    chat_id,
                                    msg,
                                    reply_markup=_tg_main_keyboard(lang, chat_id),
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            try:
                                tg_send_message(
                                    chat_id,
                                    f"❌ update: {e}",
                                    reply_markup=_tg_main_keyboard(lang, chat_id),
                                )
                            except Exception:
                                pass

                    threading.Thread(
                        target=_do_install, name="tg-update-install", daemon=True
                    ).start()
                    return
                tg_answer_callback(cq_id)
                _tg_send_update(chat_id, lang, edit_message_id=mid)
                return
            tg_answer_callback(cq_id)
            return

        # s:lang:ru | s:notify | s:notify:back | s:tog:zone | s:refresh | s:menu
        if len(parts) >= 2 and parts[0] == "s":
            action = parts[1]
            if action == "lang" and len(parts) >= 3:
                new_lang = "en" if parts[2].startswith("en") else "ru"
                tg_answer_callback(
                    cq_id, "English" if new_lang == "en" else "Русский"
                )
                prefs = _tg_set_chat_prefs(chat_id, lang=new_lang)
                _tg_send_profile(chat_id, prefs, edit_message_id=mid, view="root")
                tg_send_message(
                    chat_id,
                    "⌨️ " + ("Menu updated" if new_lang == "en" else "Меню обновлено"),
                    reply_markup=_tg_main_keyboard(new_lang, chat_id),
                )
                return
            if action == "notify":
                # s:notify | s:notify:back | s:notify:open
                sub = parts[2] if len(parts) >= 3 else "open"
                prefs = _tg_get_chat_prefs(chat_id)
                view = "root" if sub in ("back", "close", "root") else "notify"
                toast = (
                    ("Notifications" if view == "notify" else "Back")
                    if en
                    else ("Уведомления" if view == "notify" else "Назад")
                )
                tg_answer_callback(cq_id, toast)
                try:
                    _tg_send_profile(
                        chat_id, prefs, edit_message_id=mid, view=view
                    )
                except Exception as e:
                    print(f"[tg] notify panel: {e}")
                    try:
                        _tg_send_profile(chat_id, prefs, view=view)
                    except Exception as e2:
                        print(f"[tg] notify panel send: {e2}")
                return
            if action == "sections":
                # s:sections | s:sections:back
                sub = parts[2] if len(parts) >= 3 else "open"
                prefs = _tg_get_chat_prefs(chat_id)
                view = "root" if sub in ("back", "close", "root") else "sections"
                toast = (
                    ("Menu sections" if view == "sections" else "Back")
                    if en
                    else ("Разделы меню" if view == "sections" else "Назад")
                )
                tg_answer_callback(cq_id, toast)
                try:
                    _tg_send_profile(
                        chat_id, prefs, edit_message_id=mid, view=view
                    )
                except Exception as e:
                    print(f"[tg] sections panel: {e}")
                    try:
                        _tg_send_profile(chat_id, prefs, view=view)
                    except Exception as e2:
                        print(f"[tg] sections panel send: {e2}")
                return
            if action == "togsec" and len(parts) >= 3:
                # s:togsec:miner|policy|force_stop|filtration|settings|help
                sid = parts[2]
                pk = _TG_SECTION_TOG.get(sid)
                tg_answer_callback(cq_id, "OK")
                if pk:
                    cur = bool(prefs.get(pk, True))
                    prefs = _tg_set_chat_prefs(chat_id, **{pk: not cur})
                else:
                    prefs = _tg_get_chat_prefs(chat_id)
                _tg_send_profile(
                    chat_id, prefs, edit_message_id=mid, view="sections"
                )
                # push updated main keyboard
                try:
                    tg_send_message(
                        chat_id,
                        "⌨️ " + ("Menu updated" if en else "Меню обновлено"),
                        reply_markup=_tg_main_keyboard(lang, chat_id),
                    )
                except Exception:
                    pass
                return
            if action == "tog" and len(parts) >= 3:
                what = parts[2]
                tg_answer_callback(cq_id, "OK")
                prefs = _tg_apply_notify_cmd(chat_id, what, None, prefs)
                # stay inside notifications panel when toggling notify flags
                view = (
                    "notify"
                    if what in ("zone", "safety", "offline", "events")
                    else "root"
                )
                _tg_send_profile(
                    chat_id, prefs, edit_message_id=mid, view=view
                )
                return
            if action == "refresh":
                tg_answer_callback(cq_id, "OK")
                prefs = _tg_get_chat_prefs(chat_id)
                _tg_send_profile(chat_id, prefs, edit_message_id=mid, view="root")
                return
            if action == "menu":
                tg_answer_callback(cq_id)
                tg_send_message(
                    chat_id,
                    "🏠 " + ("Main menu" if en else "Главное меню"),
                    reply_markup=_tg_main_keyboard(lang, chat_id),
                )
                return
        # d:on | d:off | d:refresh — Dry Run
        if len(parts) >= 2 and parts[0] == "d":
            action = parts[1]
            if action == "refresh":
                tg_answer_callback(cq_id, "OK")
                _tg_send_dry_run(chat_id, lang, edit_message_id=mid)
                return
            if action in ("on", "off", "toggle"):
                if action == "toggle":
                    want = not _tg_dry_run_on()
                else:
                    want = action == "on"
                note = _tg_set_dry_run(want, lang)
                tg_answer_callback(cq_id, note[:180], alert=note.startswith("❌") or note.startswith("⚠️"))
                _tg_send_dry_run(
                    chat_id,
                    lang,
                    edit_message_id=mid,
                    note=note,
                    refresh_keyboard=True,
                )
                return
            tg_answer_callback(cq_id)
            return

        # i:refresh | i:miner  — Info card
        if len(parts) >= 2 and parts[0] == "i":
            action = parts[1]
            if action == "refresh":
                tg_answer_callback(cq_id, "OK")
                _tg_send_info(chat_id, lang, edit_message_id=mid)
                return
            if action == "miner":
                tg_answer_callback(cq_id)
                _tg_send_miner(chat_id, lang)
                return
            tg_answer_callback(cq_id)
            return

        # st: — Status card (preset submenu + refresh)
        # st:refresh | st:preset | st:preset:<id> | st:back
        if len(parts) >= 2 and parts[0] == "st":
            action = parts[1]
            if action == "refresh":
                tg_answer_callback(cq_id, "OK")
                # cache-only snapshot (same as /status) — no miner poll
                _tg_send_status(chat_id, lang, edit_message_id=mid, view="root")
                return
            if action == "preset":
                if len(parts) >= 3:
                    # apply preset id
                    pid = parts[2]
                    try:
                        out = apply_zone_preset(pid)
                        name = str((out or {}).get("name") or pid)
                        toast = (
                            f"Preset: {name}"
                            if en
                            else f"Пресет: {name}"
                        )
                        tg_answer_callback(cq_id, toast[:180])
                    except Exception as e:
                        tg_answer_callback(
                            cq_id,
                            (f"Preset error: {e}" if en else f"Ошибка пресета: {e}")[
                                :180
                            ],
                            alert=True,
                        )
                    _tg_send_status(
                        chat_id, lang, edit_message_id=mid, view="root"
                    )
                    return
                # open submenu
                tg_answer_callback(cq_id, "OK")
                _tg_send_status(
                    chat_id, lang, edit_message_id=mid, view="preset"
                )
                return
            if action == "back":
                tg_answer_callback(cq_id, "OK")
                _tg_send_status(chat_id, lang, edit_message_id=mid, view="root")
                return
            tg_answer_callback(cq_id)
            return

        # m: — Miner control (UI #miner)
        # m:refresh | m:work:sleep|resume | m:mode:low|normal|high
        # m:limd:±500 | m:pct:N | m:dry:… | m:pools | m:info
        if len(parts) >= 2 and parts[0] == "m":
            action = parts[1]
            fu = cq.get("from") if isinstance(cq.get("from"), dict) else None
            if action == "refresh":
                # Only place that force-polls the miner for Telegram UI
                tg_answer_callback(cq_id, "OK")
                _tg_send_miner(
                    chat_id, lang, edit_message_id=mid, force_refresh=True
                )
                return
            if action == "info":
                tg_answer_callback(cq_id)
                _tg_send_info(chat_id, lang)
                return
            if action == "dry":
                # m:dry | m:dry:toggle | m:dry:on | m:dry:off
                sub = parts[2] if len(parts) >= 3 else "toggle"
                if sub in ("toggle", "tog", ""):
                    want = not _tg_dry_run_on()
                elif sub in ("on", "1", "true"):
                    want = True
                elif sub in ("off", "0", "false"):
                    want = False
                else:
                    want = not _tg_dry_run_on()
                _tg_log_control(
                    chat_id,
                    fu,
                    "Dry Run " + ("ON" if want else "OFF"))
                note = _tg_set_dry_run(want, lang)
                tg_answer_callback(
                    cq_id,
                    note[:180],
                    alert=note.startswith("❌") or note.startswith("⚠️"),
                )
                _tg_send_miner(chat_id, lang, edit_message_id=mid)
                return
            # m:reboot | m:reboot:yes | m:restart | m:restart:yes
            if action in ("reboot", "restart"):
                sub = parts[2] if len(parts) >= 3 else ""
                if sub in ("no", "cancel", "0"):
                    tg_answer_callback(cq_id, "OK" if en else "Отмена")
                    return
                if sub not in ("yes", "go", "1", "ok"):
                    # ask confirm
                    tg_answer_callback(cq_id)
                    if action == "reboot":
                        q = (
                            "🔁 Reboot ASIC?\nFull device reboot · offline several minutes."
                            if en
                            else "🔁 Reboot ASIC?\nПолный reboot устройства · offline несколько минут."
                        )
                        yes_l = "✅ Reboot" if en else "✅ Reboot"
                        no_l = "❌ Cancel" if en else "❌ Отмена"
                        yes_cb, no_cb = "m:reboot:yes", "m:reboot:no"
                    else:
                        q = (
                            "🔃 Restart miner?\nRestarts btminer only · upfreq after."
                            if en
                            else "🔃 Restart miner?\nТолько btminer · затем upfreq."
                        )
                        yes_l = "✅ Restart" if en else "✅ Restart"
                        no_l = "❌ Cancel" if en else "❌ Отмена"
                        yes_cb, no_cb = "m:restart:yes", "m:restart:no"
                    tg_send_message(
                        chat_id,
                        q,
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {"text": yes_l, "callback_data": yes_cb},
                                    {"text": no_l, "callback_data": no_cb},
                                ]
                            ]
                        },
                    )
                    return
                # confirmed
                if action == "reboot":
                    _tg_log_control(chat_id, fu, "Reboot ASIC")
                    msg = _tg_apply_miner_write("reboot", "asic", lang)
                else:
                    _tg_log_control(chat_id, fu, "Restart miner")
                    msg = _tg_apply_miner_write("restart_miner", "btminer", lang)
                tg_answer_callback(cq_id, msg[:180], alert=msg.startswith("❌"))
                tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
                return
            if action == "work" and len(parts) >= 3:
                want = "sleep" if parts[2] in ("sleep", "suspend") else "resume"
                _tg_log_control(
                    chat_id,
                    fu,
                    "Mining Control " + ("Suspend" if want == "sleep" else "Resume"))
                msg = _tg_apply_miner_write("working", want, lang)
                tg_answer_callback(cq_id, msg[:180], alert=msg.startswith("❌"))
                _tg_send_miner(chat_id, lang, edit_message_id=mid)
                return
            # legacy
            if action in ("suspend", "resume"):
                want = "sleep" if action == "suspend" else "resume"
                _tg_log_control(
                    chat_id,
                    fu,
                    "Mining Control " + ("Suspend" if want == "sleep" else "Resume"))
                msg = _tg_apply_miner_write("working", want, lang)
                tg_answer_callback(cq_id, msg[:180], alert=msg.startswith("❌"))
                _tg_send_miner(chat_id, lang, edit_message_id=mid)
                return
            if action == "mode" and len(parts) >= 3:
                mode = parts[2].lower()
                if mode not in ("low", "normal", "high"):
                    tg_answer_callback(cq_id, "mode?", alert=True)
                    return
                _tg_log_control(
                    chat_id, fu, f"Power Mode {mode.upper()}"
                )
                msg = _tg_apply_miner_write("mode", mode, lang)
                tg_answer_callback(cq_id, msg[:180], alert=msg.startswith("❌"))
                _tg_send_miner(chat_id, lang, edit_message_id=mid)
                return
            if action == "limd" and len(parts) >= 3:
                try:
                    delta = int(parts[2])
                except ValueError:
                    tg_answer_callback(cq_id, "bad delta", alert=True)
                    return
                live, ok_live, e_live = _tg_live_snapshot()
                if not ok_live:
                    tg_answer_callback(
                        cq_id, f"❌ {e_live or 'offline'}", alert=True
                    )
                    return
                cur = _live_power_limit_w(live)
                if cur is None:
                    cur = 0
                cur_i = int(round(float(cur)))
                new_lim = max(0, min(20000, cur_i + delta))
                # same limit after clamp (± at 0/max) or noop — do not re-write ASIC
                if new_lim == cur_i:
                    msg = (
                        f"Power Limit already {cur_i} W"
                        if en
                        else f"Power Limit уже {cur_i} W"
                    )
                    tg_answer_callback(cq_id, msg[:180])
                    _tg_send_miner(chat_id, lang, edit_message_id=mid)
                    return
                _tg_log_control(
                    chat_id,
                    fu,
                    f"Power Limit {new_lim} W")
                msg = _tg_apply_miner_write("power_limit", new_lim, lang)
                tg_answer_callback(cq_id, msg[:180], alert=msg.startswith("❌"))
                _tg_send_miner(chat_id, lang, edit_message_id=mid)
                return
            if action == "pct" and len(parts) >= 3:
                try:
                    pct = int(parts[2])
                except ValueError:
                    tg_answer_callback(cq_id, "pct?", alert=True)
                    return
                # early skip: same % already set → no set_power_pct (no mining restart)
                try:
                    live, _, _ = _tg_live_snapshot()
                    have = _f(live.get("power_pct_cmd"))
                    if have is None:
                        have = _f(_state.get("power_pct_cmd"))
                    if have is not None and abs(float(have) - float(pct)) < 0.5:
                        msg = (
                            f"Power pct already {pct}%"
                            if en
                            else f"Power pct уже {pct}%"
                        )
                        tg_answer_callback(cq_id, msg[:180])
                        _tg_send_miner(chat_id, lang, edit_message_id=mid)
                        return
                except Exception:
                    pass
                _tg_log_control(
                    chat_id, fu, f"Power pct {pct}%"
                )
                msg = _tg_apply_miner_write("power_pct", pct, lang)
                tg_answer_callback(cq_id, msg[:180], alert=msg.startswith("❌"))
                _tg_send_miner(chat_id, lang, edit_message_id=mid)
                return
            if action == "pools":
                tg_answer_callback(cq_id)
                try:
                    body = fetch_mining_pools(force=True)
                    pools = body.get("pools") or []
                    if not pools:
                        tg_send_message(
                            chat_id,
                            "pools: empty" if en else "pools: пусто",
                            reply_markup=_tg_main_keyboard(lang, chat_id),
                        )
                    else:
                        lines = ["Pools:" if en else "Пулы:"]
                        for p in pools:
                            act = "●" if p.get("active") else "○"
                            lines.append(
                                f"{act} #{p.get('pool')} {p.get('url')}\n"
                                f"  {p.get('user')} · {p.get('status')} · "
                                f"A={p.get('accepted')} R={p.get('rejected')}"
                            )
                        tg_send_message(
                            chat_id,
                            "\n".join(lines),
                            reply_markup=_tg_main_keyboard(lang, chat_id),
                        )
                except Exception as e:
                    tg_send_message(
                        chat_id,
                        f"pools error: {e}",
                        reply_markup=_tg_main_keyboard(lang, chat_id),
                    )
                return
            tg_answer_callback(cq_id)
            return
        tg_answer_callback(cq_id)
    except Exception as e:
        print(f"[tg] callback error: {e}")
        try:
            tg_answer_callback(cq_id, "error", alert=True)
        except Exception:
            pass


def _tg_utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram entity offsets are UTF-16 code units."""
    try:
        u16 = (text or "").encode("utf-16-le")
        start = max(0, int(offset)) * 2
        end = start + max(0, int(length)) * 2
        return u16[start:end].decode("utf-16-le")
    except Exception:
        try:
            o = int(offset)
            n = int(length)
            return (text or "")[o : o + n]
        except Exception:
            return ""


def _tg_extract_custom_emoji(msg: dict | None) -> list[dict]:
    """
    Parse custom_emoji_id from message entities / stickers.
    Returns list of {id, emoji, source} or sticker file meta without custom id.
    """
    msg = msg if isinstance(msg, dict) else {}
    out: list[dict] = []
    seen: set[str] = set()

    def _add(eid, emoji: str, source: str) -> None:
        if not eid:
            return
        key = str(eid)
        if key in seen:
            return
        seen.add(key)
        out.append({"id": key, "emoji": emoji or "?", "source": source})

    body = str(msg.get("text") or msg.get("caption") or "")
    for ent_key in ("entities", "caption_entities"):
        ents = msg.get(ent_key)
        if not isinstance(ents, list):
            continue
        for ent in ents:
            if not isinstance(ent, dict):
                continue
            if str(ent.get("type") or "") != "custom_emoji":
                continue
            eid = ent.get("custom_emoji_id")
            ch = _tg_utf16_slice(
                body, int(ent.get("offset") or 0), int(ent.get("length") or 0)
            )
            _add(eid, ch, "custom_emoji")

    st = msg.get("sticker")
    if isinstance(st, dict):
        if st.get("custom_emoji_id"):
            _add(st.get("custom_emoji_id"), str(st.get("emoji") or "sticker"), "sticker")
        else:
            # ordinary sticker — useful ids but not custom emoji
            out.append(
                {
                    "id": None,
                    "file_id": st.get("file_id"),
                    "emoji": st.get("emoji"),
                    "set_name": st.get("set_name"),
                    "source": "sticker_file",
                }
            )
    return out


def _tg_html_esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _tg_format_emoji_ids(items: list[dict], lang: str = "ru") -> str:
    """
    HTML body for /emoji tool.
    Custom emoji are sent as <tg-emoji> so clients render the pack art,
    not only the unicode fallback character.
    """
    en = str(lang or "ru").lower().startswith("en")
    if not items:
        return (
            "No custom emoji found.\nSend a premium/custom emoji (not a keyboard button)."
            if en
            else "Custom emoji не найден.\nПришлите premium/custom emoji (не кнопку клавиатуры)."
        )
    lines = ["🔎 <b>custom_emoji_id</b>:"]
    for i, it in enumerate(items, 1):
        if it.get("source") == "sticker_file":
            lines.append(
                f"{i}. sticker (not custom emoji)\n"
                f"   emoji: {_tg_html_esc(it.get('emoji') or '—')}\n"
                f"   set: {_tg_html_esc(it.get('set_name') or '—')}\n"
                f"   file_id: <code>{_tg_html_esc(it.get('file_id') or '—')}</code>"
            )
            continue
        eid = re.sub(r"[^0-9]", "", str(it.get("id") or ""))
        em = it.get("emoji") or "⭐"
        em_esc = _tg_html_esc(em)
        if not eid:
            lines.append(f"{i}. {em_esc}  →  —")
            continue
        # Live custom glyph in the reply + plain id
        lines.append(
            f"{i}. <tg-emoji emoji-id=\"{eid}\">{em_esc}</tg-emoji>"
            f"  →  <code>{eid}</code>"
        )
        # Escaped snippet for copy-paste into bot code
        snippet = (
            f"&lt;tg-emoji emoji-id=\"{eid}\"&gt;{em_esc}&lt;/tg-emoji&gt;"
        )
        lines.append(f"   HTML: <code>{snippet}</code>")
    lines.append("")
    lines.append(
        "Mode still ON — send more, or /emoji to toggle off"
        if en
        else "Режим ВКЛ — пришлите ещё, или /emoji чтобы выключить"
    )
    lines.append(
        "\n<i>If you see only the standard glyph, bot owner needs Telegram Premium "
        "(Bot API custom emoji).</i>"
        if en
        else "\n<i>Если виден только стандартный 📦 — у владельца бота нужен "
        "Telegram Premium (custom emoji в Bot API).</i>"
    )
    return "\n".join(lines)


def _tg_emoji_wait_on(chat_id) -> None:
    with _tg_emoji_wait_lock:
        _tg_emoji_wait.add(_tg_cid_key(chat_id))


def _tg_emoji_wait_off(chat_id) -> None:
    with _tg_emoji_wait_lock:
        _tg_emoji_wait.discard(_tg_cid_key(chat_id))


def _tg_emoji_wait_active(chat_id) -> bool:
    with _tg_emoji_wait_lock:
        return _tg_cid_key(chat_id) in _tg_emoji_wait


def _tg_handle_command(
    chat_id, text: str, from_user: dict | None, msg: dict | None = None
) -> None:
    raw = (text or "").strip()
    text = _tg_normalize_incoming_text(raw)

    # Hidden capture mode: next message with custom emoji → dump ids
    if _tg_emoji_wait_active(chat_id) and msg is not None:
        low = (text or "").strip().lower()
        # let /emoji* and /cancel fall through to command handlers
        cmd0 = low.split()[0].split("@", 1)[0] if low.startswith("/") else ""
        if cmd0 not in ("/emoji", "/cancel"):
            prefs = (
                _tg_get_chat_prefs(chat_id)
                if _tg_chat_allowed(chat_id)
                else dict(DEFAULT_CHAT_PREFS)
            )
            lang = prefs.get("lang") or "ru"
            items = _tg_extract_custom_emoji(msg)
            if items or msg.get("sticker") or msg.get("entities") or msg.get(
                "caption_entities"
            ):
                tg_send_message(
                    chat_id,
                    _tg_format_emoji_ids(items, lang),
                    parse_mode="HTML",
                )
                return
            if text and not text.startswith("/"):
                en = str(lang).lower().startswith("en")
                tg_send_message(
                    chat_id,
                    (
                        "No custom_emoji entity. Send a custom/premium emoji, or /emoji to toggle off"
                        if en
                        else "Нет custom_emoji. Пришлите custom/premium emoji или /emoji чтобы выключить"
                    ),
                )
                return

    if not text:
        # sticker without wait mode — ignore
        return
    # plain text that is not a button → short hint (don't hang silently)
    if not text.startswith("/"):
        if _tg_chat_allowed(chat_id):
            prefs = _tg_get_chat_prefs(chat_id)
            lang = prefs.get("lang") or "ru"
            en = str(lang).lower().startswith("en")
            tg_send_message(
                chat_id,
                "Use the buttons below 👇" if en else "Используйте кнопки внизу 👇",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
        return
    # /cmd@BotName → /cmd
    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    with _tg_cfg_lock:
        has_ids = bool(_tg_cfg.get("chat_ids"))

    prefs = _tg_get_chat_prefs(chat_id) if _tg_chat_allowed(chat_id) else dict(DEFAULT_CHAT_PREFS)
    lang = prefs.get("lang") or "ru"
    en = str(lang).lower().startswith("en")

    if cmd in ("/start", "/help", "/chatid"):
        uname = (from_user or {}).get("username") or ""
        intro = f"🏠 {get_project_name()} bot\nchat_id: {chat_id}"
        if uname:
            intro += f"\nuser: @{uname}"
        if not has_ids or not _tg_chat_allowed(chat_id):
            intro += (
                "\n\nAdd this chat_id in Settings → Telegram → Chat IDs, enable bot, Save."
                if en
                else "\n\nДобавьте chat_id в Настройки → Telegram → Chat IDs, включите бота, Сохранить."
            )
            tg_send_message(chat_id, intro)
            return
        _tg_ensure_chat_prefs(chat_id)
        prefs = _tg_get_chat_prefs(chat_id)
        lang = prefs.get("lang") or "ru"
        en = str(lang).lower().startswith("en")
        intro += "\n\n" + _tg_commands_help(lang)
        tg_send_message(chat_id, intro, reply_markup=_tg_main_keyboard(lang, chat_id))
        return

    if not _tg_chat_allowed(chat_id):
        tg_send_message(
            chat_id,
            (
                f"⛔ chat_id {chat_id} not in allowlist.\nAdd it in Settings → Telegram."
                if en
                else f"⛔ chat_id {chat_id} не в allowlist.\nДобавьте в Настройки → Telegram."
            ),
        )
        return

    prefs = _tg_ensure_chat_prefs(chat_id)
    lang = prefs.get("lang") or "ru"
    en = str(lang).lower().startswith("en")

    # ── personal settings / language / notify (always allowed) ──
    if cmd in ("/settings", "/config", "/setup"):
        _tg_send_bot_settings(chat_id, lang)
        return

    # Hidden: custom emoji id capture (not on keyboard / public help)
    # /emoji          → toggle ON/OFF
    # /emoji on|off   → force state
    if cmd == "/emoji":
        arg0 = str(args[0]).lower() if args else ""
        force_on = arg0 in ("on", "1", "вкл", "start", "enable")
        force_off = arg0 in ("off", "0", "stop", "exit", "cancel", "выкл", "стоп")
        if force_off or (not force_on and not arg0 and _tg_emoji_wait_active(chat_id)):
            # explicit off OR bare /emoji while already ON → toggle OFF
            _tg_emoji_wait_off(chat_id)
            tg_send_message(
                chat_id,
                "🔎 emoji mode OFF" if en else "🔎 режим emoji ВЫКЛ",
            )
            return
        # ON (toggle from off, or explicit on)
        items = _tg_extract_custom_emoji(msg) if msg else []
        if items:
            _tg_emoji_wait_on(chat_id)
            tg_send_message(
                chat_id,
                _tg_format_emoji_ids(items, lang),
                parse_mode="HTML",
            )
            return
        _tg_emoji_wait_on(chat_id)
        tg_send_message(
            chat_id,
            (
                "🔎 emoji mode ON\n"
                "Send a custom/premium emoji (or custom-emoji sticker).\n"
                "I’ll reply with custom_emoji_id + HTML snippet.\n"
                "/emoji — toggle off"
                if en
                else "🔎 режим emoji ВКЛ\n"
                "Пришлите custom/premium emoji (или стикер-custom-emoji).\n"
                "Отвечу custom_emoji_id + HTML-фрагмент.\n"
                "/emoji — выключить"
            ),
        )
        return

    if cmd == "/cancel" and _tg_emoji_wait_active(chat_id):
        _tg_emoji_wait_off(chat_id)
        tg_send_message(
            chat_id,
            "🔎 emoji mode OFF" if en else "🔎 режим emoji ВЫКЛ",
        )
        return

    if cmd in ("/update", "/updates"):
        _tg_send_update(chat_id, lang)
        return

    if cmd in ("/prefs", "/my", "/profile", "/prof"):
        _tg_send_profile(chat_id, prefs)
        return

    if cmd in ("/lang_ru", "/lang_en"):
        new_lang = "en" if cmd.endswith("_en") else "ru"
        prefs = _tg_set_chat_prefs(chat_id, lang=new_lang)
        tg_send_message(
            chat_id,
            ("✅ Language: English" if new_lang == "en" else "✅ Язык: Русский"),
            reply_markup=_tg_main_keyboard(new_lang, chat_id),
        )
        _tg_send_profile(chat_id, prefs)
        return

    if cmd in ("/lang", "/language"):
        # /lang ru | /lang en
        if args:
            new_lang = "en" if str(args[0]).lower().startswith("en") else "ru"
            prefs = _tg_set_chat_prefs(chat_id, lang=new_lang)
            tg_send_message(
                chat_id,
                ("✅ Language: English" if new_lang == "en" else "✅ Язык: Русский"),
                reply_markup=_tg_main_keyboard(new_lang, chat_id),
            )
            _tg_send_profile(chat_id, prefs)
        else:
            tg_send_message(chat_id, "/lang_ru  or  /lang_en")
        return

    # /notify_zone [on|off]  ·  no arg = toggle
    notify_cmds = {
        "/notify_zone": "zone",
        "/notify_safety": "safety",
        "/notify_offline": "offline",
        "/notify_events": "events",
        "/notify_all": "all",
        "/notify_commands": "commands",
    }
    if cmd in notify_cmds:
        arg = args[0] if args else None
        prefs = _tg_apply_notify_cmd(chat_id, notify_cmds[cmd], arg, prefs)
        tg_send_message(
            chat_id,
            ("✅ Saved" if en else "✅ Сохранено"),
        )
        _tg_send_profile(chat_id, prefs)
        return

    # /notify zone on
    if cmd == "/notify":
        if not args:
            _tg_send_profile(chat_id, prefs)
            return
        what = str(args[0]).lower().replace("-", "_")
        arg = args[1] if len(args) > 1 else None
        prefs = _tg_apply_notify_cmd(chat_id, what, arg, prefs)
        _tg_send_profile(chat_id, prefs)
        return

    # global / per-chat kill-switch for control commands
    with _tg_cfg_lock:
        global_cmd = bool(_tg_cfg.get("commands_en", True))
    if (not global_cmd or not prefs.get("commands_en", True)) and cmd not in (
        "/start",
        "/help",
        "/chatid",
        "/settings",
        "/config",
        "/setup",
        "/update",
        "/updates",
        "/emoji",
        "/cancel",
        "/prefs",
        "/my",
        "/profile",
        "/prof",
        "/lang",
        "/lang_ru",
        "/lang_en",
        "/notify",
        "/notify_zone",
        "/notify_safety",
        "/notify_offline",
        "/notify_events",
        "/notify_all",
        "/notify_commands",
    ):
        tg_send_message(
            chat_id,
            "Control OFF — open /profile to enable"
            if en
            else "Управление ВЫКЛ — откройте /profile чтобы включить",
            reply_markup=_tg_main_keyboard(lang, chat_id),
        )
        return

    if cmd == "/status":
        _tg_send_status(chat_id, lang)
        return

    if cmd == "/miner":
        _tg_send_miner(chat_id, lang)
        return

    if cmd in ("/info", "/asic"):
        _tg_send_info(chat_id, lang)
        return

    if cmd in ("/dry_run", "/dryrun", "/dry"):
        # /dry_run [on|off] — lives in Miner section; no arg → open miner
        if args:
            onoff = _tg_parse_onoff(str(args[0]))
            if onoff is None:
                tg_send_message(
                    chat_id,
                    "Usage: /dry_run [on|off]" if en else "Использование: /dry_run [on|off]",
                    reply_markup=_tg_main_keyboard(lang, chat_id),
                )
                return
            _tg_log_control(
                chat_id,
                from_user,
                "Dry Run " + ("ON" if onoff else "OFF"))
            note = _tg_set_dry_run(onoff, lang)
            tg_send_message(chat_id, note, reply_markup=_tg_main_keyboard(lang, chat_id))
            _tg_send_miner(chat_id, lang)
            return
        _tg_send_miner(chat_id, lang)
        return

    if cmd in ("/mode", "/powermode"):
        if not args or str(args[0]).lower() not in ("low", "normal", "high"):
            tg_send_message(
                chat_id,
                "Usage: /mode low|normal|high" if en else "Использование: /mode low|normal|high",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        mode_v = str(args[0]).lower()
        _tg_log_control(
            chat_id, from_user, f"Power Mode {mode_v.upper()}"
        )
        msg = _tg_apply_miner_write("mode", mode_v, lang)
        tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
        _tg_send_miner(chat_id, lang)
        return

    if cmd in ("/limit", "/power_limit", "/pwlimit"):
        if not args:
            tg_send_message(
                chat_id,
                "Usage: /limit <W>" if en else "Использование: /limit <Вт>",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        try:
            watts = int(float(args[0]))
        except ValueError:
            tg_send_message(chat_id, "bad W", reply_markup=_tg_main_keyboard(lang, chat_id))
            return
        _tg_log_control(
            chat_id, from_user, f"Power Limit {watts} W"
        )
        msg = _tg_apply_miner_write("power_limit", watts, lang)
        tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
        _tg_send_miner(chat_id, lang)
        return

    if cmd in ("/pct", "/power_pct", "/pwpct"):
        if not args:
            tg_send_message(
                chat_id,
                "Usage: /pct <0-100>" if en else "Использование: /pct <0-100>",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        try:
            pct = int(float(args[0]))
        except ValueError:
            tg_send_message(chat_id, "bad %", reply_markup=_tg_main_keyboard(lang, chat_id))
            return
        _tg_log_control(chat_id, from_user, f"Power pct {pct}%")
        msg = _tg_apply_miner_write("power_pct", pct, lang)
        tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
        _tg_send_miner(chat_id, lang)
        return

    if cmd in ("/reboot_asic", "/reboot", "/reboot_miner"):
        # require explicit confirm arg for slash command safety
        conf = str(args[0]).lower() if args else ""
        if conf not in ("yes", "go", "1", "ok", "confirm", "да"):
            tg_send_message(
                chat_id,
                (
                    "🔁 Reboot ASIC — full device reboot.\n"
                    "Confirm: /reboot_asic yes"
                    if en
                    else "🔁 Reboot ASIC — полный reboot устройства.\n"
                    "Подтверждение: /reboot_asic yes"
                ),
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        _tg_log_control(chat_id, from_user, "Reboot ASIC")
        msg = _tg_apply_miner_write("reboot", "asic", lang)
        tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
        return

    if cmd in ("/restart_miner", "/restart_btminer", "/restart"):
        conf = str(args[0]).lower() if args else ""
        if conf not in ("yes", "go", "1", "ok", "confirm", "да"):
            tg_send_message(
                chat_id,
                (
                    "🔃 Restart miner — btminer only.\n"
                    "Confirm: /restart_miner yes"
                    if en
                    else "🔃 Restart miner — только btminer.\n"
                    "Подтверждение: /restart_miner yes"
                ),
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        _tg_log_control(
            chat_id, from_user, "Restart miner"
        )
        msg = _tg_apply_miner_write("restart_miner", "btminer", lang)
        tg_send_message(chat_id, msg, reply_markup=_tg_main_keyboard(lang, chat_id))
        return

    if cmd == "/events":
        pol = get_policy_status()
        evs = pol.get("events") or []
        zlab = (
            zone_title("critical")
            if pol.get("safety_sticky")
            else zone_title(pol.get("heat_zone"))
        )
        dry = bool(pol.get("dry_run"))
        fs = bool(pol.get("force_stop"))
        want = pol.get("want_work") or "—"
        have = pol.get("measured_work") or "—"
        if en:
            title = "📋 <b>Events</b>"
            empty = "no events yet (log is kept on server after restart)"
            more_fmt = "… +{n} older (on server)"
        else:
            title = "📋 <b>События</b>"
            empty = "пока пусто (журнал хранится на сервере после рестарта)"
            more_fmt = "… ещё {n} на сервере"
        # zone: <b>Z3 No heat</b> · want: <b>suspend</b> have: <b>suspend</b>
        # Dry Run: <b>OFF</b> · Force Stop: <b>OFF</b>
        z_show = _tg_html_esc(str(zlab or "—").replace(" · ", " "))
        want_s = _tg_html_esc(str(want or "—"))
        have_s = _tg_html_esc(str(have or "—"))
        dry_s = "ON" if dry else "OFF"
        fs_s = "ON" if fs else "OFF"
        line_z = (
            f"zone: <b>{z_show}</b> · want: <b>{want_s}</b> have: <b>{have_s}</b>"
        )
        line_f = (
            f"Dry Run: <b>{dry_s}</b> · Force Stop: <b>{fs_s}</b>"
        )
        lines = [title, "", line_z, line_f, ""]
        if not evs:
            lines.append(empty)
        else:
            show_n = 15
            for e in evs[:show_n]:
                if not isinstance(e, dict):
                    continue
                ts = _tg_fmt_ts_eu(e.get("ts"))
                kind = str(e.get("kind") or "—").strip() or "—"
                msg = str(e.get("msg") or "").strip()
                # Telegram HTML — escape free text
                lines.append(
                    f"{_tg_html_esc(ts)} [{_tg_html_esc(kind)}] "
                    f"{_tg_html_esc(msg)}".rstrip()
                )
            rest = len(evs) - show_n
            if rest > 0:
                lines.append(more_fmt.format(n=rest))
        tg_send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=_tg_main_keyboard(lang, chat_id),
            parse_mode="HTML",
        )
        return

    if cmd == "/pools":
        try:
            body = fetch_mining_pools(force=True)
            pools = body.get("pools") or []
            if not pools:
                tg_send_message(
                    chat_id,
                    "pools: empty / " + str(body.get("error") or "—")
                    if en
                    else "pools: пусто / " + str(body.get("error") or "—"),
                    reply_markup=_tg_main_keyboard(lang, chat_id),
                )
                return
            lines = ["Pools:"]
            for p in pools:
                act = "●" if p.get("active") else "○"
                lines.append(
                    f"{act} #{p.get('pool')} {p.get('url')}\n"
                    f"  {p.get('user')} · {p.get('status')} · "
                    f"A={p.get('accepted')} R={p.get('rejected')}"
                )
            tg_send_message(
                chat_id,
                "\n".join(lines),
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
        except Exception as e:
            tg_send_message(chat_id, f"pools error: {e}", reply_markup=_tg_main_keyboard(lang, chat_id))
        return

    if cmd in (
        "/force_stop",
        "/forcestop",
        "/stop_work",
        "/stopwork",
        "/suspend",
        "/resume",
        "/sleep",
        "/mining",
    ):
        # One control: Force Stop (sticky Suspend). Toggle if no arg.
        # /suspend|/stop → ON; /resume|/continue → OFF; bare button → toggle.
        arg0 = str(args[0]).lower() if args else ""
        onoff = _tg_parse_onoff(arg0) if arg0 else None
        if onoff is None:
            if cmd in ("/suspend", "/sleep", "/stop_work", "/stopwork"):
                onoff = True
            elif cmd in ("/resume", "/mining"):
                onoff = False
            elif arg0 in ("stop", "halt"):
                onoff = True
            elif arg0 in ("continue", "clear", "go"):
                onoff = False
            else:
                onoff = not get_force_stop()
        # Profile: «Подтверждение Force Stop» — ask before apply (default ON)
        # Log only after apply (confirm Yes or no-confirm path) — not on button press
        need_confirm = bool(prefs.get("confirm_force_stop", True))
        if need_confirm:
            _tg_offer_force_stop_confirm(chat_id, bool(onoff), lang)
        else:
            _tg_apply_force_stop_result(
                chat_id, bool(onoff), lang, from_user=from_user, log=True
            )
        return

    if cmd in ("/filtration", "/filter"):
        try:
            st = get_filtration_status()
        except Exception as e:
            tg_send_message(
                chat_id,
                f"❌ filtration: {e}",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        if not st.get("enabled"):
            tg_send_message(
                chat_id,
                (
                    "💧 Filtration disabled in Settings"
                    if en
                    else "💧 Фильтрация отключена в настройках"
                ),
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        arg0 = str(args[0]).lower() if args else ""
        onoff = _tg_parse_onoff(arg0) if arg0 else None
        if onoff is None:
            # bare button / no arg → toggle
            onoff = not (st.get("on") is True)
        # OFF locked while mining unless allow_off_while_mining (can_turn_off)
        if not onoff and st.get("mining") is True and not st.get("can_turn_off"):
            tg_send_message(
                chat_id,
                (
                    "🔒 Filtration OFF unavailable while mining\n"
                    "Stop mining first, or enable «Allow OFF while mining» in Settings"
                    if en
                    else "🔒 Фильтрация ВЫКЛ недоступна при майнинге\n"
                    "Остановите майнинг или включите «Разрешить OFF при mining» в настройках"
                ),
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
            return
        try:
            _tg_log_control(
                chat_id,
                from_user,
                "Filtration " + ("ON" if onoff else "OFF"))
            out = filtration_set(bool(onoff), source="telegram", force=False)
            if not out.get("ok"):
                err = out.get("error") or "fail"
                # re-pretty with chat language if raw slipped through
                if "\n" not in str(err) or "Не удалось" not in str(err):
                    err = _filtration_user_error(
                        Exception(str(err)),
                        on=bool(onoff),
                        backend=out.get("backend") or st.get("backend"),
                        lang=lang,
                    )
                tg_send_message(
                    chat_id,
                    f"❌ {err}",
                    reply_markup=_tg_main_keyboard(lang, chat_id),
                )
                return
            # Same wording as auto-notify; keyboard rebuilt after last_on update
            got_on = out.get("on")
            if got_on is None:
                got_on = onoff
            if en:
                msg = (
                    "💦 Filtration pump is on"
                    if got_on
                    else "🚱 Filtration pump is off"
                )
            else:
                msg = (
                    "💦 Насос фильтрации включен"
                    if got_on
                    else "🚱 Насос фильтрации выключен"
                )
            tg_send_message(
                chat_id,
                msg,
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
        except Exception as e:
            tg_send_message(
                chat_id,
                "❌ "
                + _filtration_user_error(
                    e,
                    on=bool(onoff),
                    backend=st.get("backend") if isinstance(st, dict) else None,
                    lang=lang,
                ),
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
        return

    if cmd == "/override":
        mins = 30.0
        if args:
            try:
                mins = float(args[0])
            except ValueError:
                pass
        try:
            set_policy_override(minutes=mins, clear=False)
            tg_send_message(
                chat_id,
                f"✅ Override {mins:.0f} min" if en else f"✅ Override {mins:.0f} мин",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
        except Exception as e:
            tg_send_message(chat_id, f"❌ override: {e}")
        return

    if cmd == "/override_off":
        try:
            set_policy_override(clear=True)
            tg_send_message(
                chat_id,
                "✅ Override cleared" if en else "✅ Override снят",
                reply_markup=_tg_main_keyboard(lang, chat_id),
            )
        except Exception as e:
            tg_send_message(chat_id, f"❌ {e}")
        return

    tg_send_message(
        chat_id,
        "Unknown. Use buttons or /help" if en else "Неизвестно. Кнопки или /help",
        reply_markup=_tg_main_keyboard(lang, chat_id),
    )


def _tg_cmd_label_from_text(text: str) -> str:
    """Short label for timing log: /status, button label, or truncated text."""
    t = (text or "").strip()
    if not t:
        return "(empty)"
    # slash commands
    if t.startswith("/"):
        # /status@bot args → /status
        head = t.split()[0]
        head = head.split("@", 1)[0]
        return head[:60]
    # reply keyboard / free text — first line, short
    line = t.split("\n", 1)[0].strip()
    return line[:60] if line else "(empty)"


def _tg_process_update(upd: dict) -> None:
    uid = upd.get("update_id")
    # inline button callback
    cq = upd.get("callback_query")
    if isinstance(cq, dict):
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        data = str(cq.get("data") or "")
        _tg_req_begin(
            kind="callback",
            cmd=f"cb:{data}"[:100],
            chat_id=chat_id,
            update_id=uid,
        )
        try:
            _tg_remember_chat(msg.get("chat"), cq.get("from"))
            _tg_handle_callback(cq)
            _tg_req_end()
        except Exception as e:
            print(f"[tg] callback: {e}")
            _tg_req_end(error=str(e))
        return

    msg = upd.get("message") or upd.get("edited_message")
    if not isinstance(msg, dict):
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = msg.get("text") or msg.get("caption") or ""
    kind = "edited" if upd.get("edited_message") else "message"
    _tg_req_begin(
        kind=kind,
        cmd=_tg_cmd_label_from_text(text)
        if text
        else ("(sticker)" if msg.get("sticker") else "(msg)"),
        chat_id=chat_id,
        update_id=uid,
    )
    try:
        try:
            _tg_remember_chat(chat, msg.get("from"))
        except Exception:
            pass
        # always pass full msg (entities / stickers) — needed for /emoji capture
        if text or msg.get("sticker") or msg.get("entities") or msg.get("caption_entities"):
            _tg_handle_command(chat_id, text, msg.get("from"), msg=msg)
        _tg_req_end()
    except Exception as e:
        print(f"[tg] message: {e}")
        _tg_req_end(error=str(e))


def telegram_loop() -> None:
    """Long-poll getUpdates. Independent of browser UI."""
    _tg_stop.wait(timeout=3.0)
    while not _tg_stop.is_set():
        with _tg_cfg_lock:
            enabled = bool(_tg_cfg.get("enabled"))
            token = str(_tg_cfg.get("bot_token") or "").strip()
            offset = int(_tg_cfg.get("offset") or 0)
        if not enabled or not token:
            _tg_stop.wait(timeout=5.0)
            continue
        try:
            # resolve bot identity once
            with _tg_state_lock:
                need_me = _tg_state.get("me") is None
            if need_me:
                try:
                    me = _tg_api("getMe", timeout=15).get("result") or {}
                    with _tg_state_lock:
                        _tg_state["me"] = me.get("username") or me.get("id")
                        _tg_state["ok"] = True
                    print(f"[tg] bot @{_tg_state.get('me')}")
                except Exception as e:
                    with _tg_state_lock:
                        _tg_state["last_error"] = str(e)
                        _tg_state["ok"] = False
                    print(f"[tg] getMe fail: {e}")
                    _tg_stop.wait(timeout=10.0)
                    continue

            # IMPORTANT: must include callback_query or inline buttons spin forever
            body = _tg_api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": [
                        "message",
                        "edited_message",
                        "callback_query",
                    ],
                },
                timeout=35,
            )
            updates = body.get("result") or []
            with _tg_state_lock:
                _tg_state["polls"] = int(_tg_state.get("polls") or 0) + 1
                _tg_state["last_update_ts"] = datetime.now().isoformat(timespec="seconds")
                _tg_state["ok"] = True
                _tg_state["last_error"] = None

            max_id = offset
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                uid = int(upd.get("update_id") or 0)
                if uid >= max_id:
                    max_id = uid + 1
                try:
                    _tg_process_update(upd)
                except Exception as e:
                    print(f"[tg] handle update: {e}")
            if max_id != offset:
                with _tg_cfg_lock:
                    _tg_cfg["offset"] = max_id
                # persist offset (throttled — flash write on Peak is slow)
                try:
                    _save_telegram_cfg()
                except Exception:
                    pass
            # flush dirty prefs if throttle skipped an earlier write
            try:
                if _tg_save_dirty:
                    _save_telegram_cfg(force=True)
            except Exception:
                pass
        except Exception as e:
            with _tg_state_lock:
                _tg_state["ok"] = False
                _tg_state["last_error"] = str(e)
            print(f"[tg] poll: {e}")
            _tg_stop.wait(timeout=5.0)


def tg_test_send() -> dict:
    """Send a test message to all chat_ids."""
    with _tg_cfg_lock:
        if not _tg_cfg.get("bot_token"):
            return {"ok": False, "error": "bot_token empty"}
        if not _tg_cfg.get("chat_ids"):
            return {"ok": False, "error": "chat_ids empty — write /start to bot and add chat_id"}
    n = tg_broadcast(
        f"✅ Test · {get_project_name()}\n"
        f"{datetime.now().isoformat(timespec='seconds')}\n\n"
        + _tg_status_text()
    )
    return {"ok": n > 0, "sent": n, "status": get_telegram_cfg(redact=True).get("status")}


_load_telegram_cfg()


# ─── server-side policy control ───────────────────────────────────────────────


def _load_policy_events() -> None:
    """Restore action/policy log from disk after restart."""
    try:
        raw = _load_json(POLICY_EVENTS_FILE, {"events": []})
    except Exception as e:
        print(f"[policy] load events: {e}")
        return
    evs = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(evs, list):
        return
    clean: list = []
    rewritten = 0
    for e in evs:
        if not isinstance(e, dict):
            continue
        raw_msg = str(e.get("msg") or "")
        kind = str(e.get("kind") or "—")
        # rewrite verbose TG lines for display + disk
        if kind == "tg" or raw_msg.lower().startswith("chat "):
            new_msg = _rewrite_tg_event_msg(raw_msg)
            if new_msg != raw_msg:
                rewritten += 1
                raw_msg = new_msg
        clean.append(
            {
                "ts": str(e.get("ts") or ""),
                "kind": kind,
                "msg": raw_msg,
                **{
                    k: v
                    for k, v in e.items()
                    if k not in ("ts", "kind", "msg") and v is not None
                },
            }
        )
        if len(clean) >= POLICY_EVENTS_MAX:
            break
    with _policy_lock:
        _policy_ctrl["events"] = clean
        _policy_ctrl["last_event"] = clean[0] if clean else None
    if clean:
        print(f"[policy] loaded {len(clean)} events from {POLICY_EVENTS_FILE.name}")
    if rewritten:
        try:
            _save_policy_events()
            print(f"[policy] rewrote {rewritten} laconic TG event lines on disk")
        except Exception as e:
            print(f"[policy] rewrite save: {e}")


def _save_policy_events() -> None:
    """Persist ring buffer to DATA/policy_events.json."""
    with _policy_lock:
        evs = list(_policy_ctrl.get("events") or [])[:POLICY_EVENTS_MAX]
    try:
        _save_json(POLICY_EVENTS_FILE, {"events": evs, "max": POLICY_EVENTS_MAX})
    except Exception as e:
        print(f"[policy] save events: {e}")


def _policy_log(kind: str, msg: str, **extra) -> None:
    ev = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "msg": msg,
        **extra,
    }
    with _policy_lock:
        _policy_ctrl["last_event"] = ev
        events = _policy_ctrl.get("events") or []
        events.insert(0, ev)
        _policy_ctrl["events"] = events[:POLICY_EVENTS_MAX]
    print(f"[policy] {ev['ts']} {kind}: {msg}")
    # disk so OTA / restart does not wipe the log
    try:
        _save_policy_events()
    except Exception:
        pass
    # Telegram notify (best-effort, non-blocking)
    try:
        tg_on_policy_event(kind, msg, extra)
    except Exception:
        pass


_load_policy_events()


def _place_heat_zone(liq: float, t0: float, t1: float, t2: float) -> str:
    """Absolute placement without hysteresis."""
    if liq <= t0:
        return "z0"  # High Heat
    if liq <= t1:
        return "z1"  # Normal
    if liq >= t2:
        return "z3"  # No heat
    return "z2"  # Reduced


def _evaluate_heat_zone(
    liquid: float | None,
    sticky: str | None,
    t0: float,
    t1: float,
    t2: float,
    h: float,
) -> str:
    """
    Z0 High Heat · Z1 Normal · Z2 Reduced · Z3 No heat.
    Thresholds: liquid ≤ T0 | T0–T1 | T1–T2 | ≥ T2. Hysteresis h at edges.
    """
    valid = ("z0", "z1", "z2", "z3")
    if liquid is None or not isinstance(liquid, (int, float)):
        return sticky if sticky in valid else "z2"
    liq = float(liquid)
    h = max(0.2, float(h))
    if sticky not in valid:
        return _place_heat_zone(liq, t0, t1, t2)
    if sticky == "z0":
        if liq >= t0 + h:
            return _place_heat_zone(liq, t0, t1, t2)
        return "z0"
    if sticky == "z3":
        if liq <= t2 - h:
            return _place_heat_zone(liq, t0, t1, t2)
        return "z3"
    if sticky == "z1":
        if liq <= t0:
            return "z0"
        if liq >= t1 + h:
            return _place_heat_zone(liq, t0, t1, t2)
        return "z1"
    # sticky z2 Reduced
    if liq <= t1 - h:
        return _place_heat_zone(liq, t0, t1, t2)
    if liq >= t2:
        return "z3"
    return "z2"


def _upfreq_block(live: dict) -> bool:
    """
    True while hashboards are still ramping (Upfreq Complete ≠ 1).
    Not meaningful when miner is Suspended — boards report upfreq=0 while off,
    which must NOT block Z1 resume (deadlock: suspend → forever warmup).
    """
    # Suspend / off: ignore upfreq gate
    try:
        if _measured_work_state(live) == "sleep":
            return False
    except Exception:
        mo = str(live.get("mineroff") or "").strip().lower()
        if mo in ("true", "1", "yes"):
            return False
        p = _f(live.get("power"))
        h = _f(live.get("hashrate_th"))
        if (p is not None and p < 50) and (h is None or h < 1):
            return False
    up = live.get("upfreq") or []
    try:
        return any(int(u) != 1 for u in up)
    except (TypeError, ValueError):
        return False


def _normalize_work_side(v) -> str | None:
    """suspend | resume | None from work_cmd / work_measured."""
    s = str(v or "").strip().lower()
    if s in ("sleep", "suspend", "power_off", "off"):
        return "suspend"
    if s in ("resume", "mining", "power_on", "on"):
        return "resume"
    return None


def mining_run_status(live: dict | None) -> dict:
    """
    Miner lifecycle for UI / Telegram:
      starting  — commanded Resume, API still Suspend
      stopping  — commanded Suspend, API still Resume
      tuning    — mining, boards still Upfreq
      running   — mining, Upfreq complete
      stopped   — Suspend (stable)
    """
    live = live or {}
    meas = _normalize_work_side(live.get("work_measured"))
    if meas is None:
        try:
            mw = _measured_work_state(live)
            meas = "suspend" if mw == "sleep" else "resume"
        except Exception:
            meas = None
    cmd = _normalize_work_side(live.get("work_cmd"))

    # commanded vs measured mismatch = transitional
    if cmd == "resume" and meas == "suspend":
        return {
            "key": "starting",
            "label_ru": "Запускается",
            "label_en": "Starting",
        }
    if cmd == "suspend" and meas == "resume":
        return {
            "key": "stopping",
            "label_ru": "Останавливается",
            "label_en": "Stopping",
        }

    if meas == "resume":
        try:
            tuning = _upfreq_block(live)
        except Exception:
            tuning = False
        if tuning:
            return {
                "key": "tuning",
                "label_ru": "Тюнинг",
                "label_en": "Tuning",
            }
        return {
            "key": "running",
            "label_ru": "Работает",
            "label_en": "Running",
        }

    if meas == "suspend":
        return {
            "key": "stopped",
            "label_ru": "Остановлен",
            "label_en": "Stopped",
        }

    return {"key": "unknown", "label_ru": "—", "label_en": "—"}


_MODE_RANK = {"low": 0, "normal": 1, "high": 2}


def _cmd_is_upward(action: str, value, live: dict) -> bool:
    """
    True if command increases heat/power vs measured state.
    Used by warmup gate: block upward, allow downward + Critical.
    """
    action = (action or "").strip().lower()
    if action == "working":
        want = "suspend" if str(value).lower() in ("suspend", "sleep") else "resume"
        have = _live_work(live)
        # resume while suspended (or idle) → more heat
        return want == "resume" and have == "suspend"
    if action == "mode":
        want = str(value).strip().lower()
        have = _live_mode(live)
        wr = _MODE_RANK.get(want)
        if wr is None:
            return False
        if have is None:
            # unknown measured: treat normal/high as upward risk
            return wr > 0
        return wr > _MODE_RANK.get(have, 0)
    if action == "power_limit":
        try:
            want = float(value)
        except (TypeError, ValueError):
            return False
        have = _live_limit_w(live)
        if have is None:
            return want > 0
        return want > float(have) + 50.0
    if action == "power_pct":
        try:
            want = float(value)
        except (TypeError, ValueError):
            return False
        have = None
        pw = _f(live.get("power"))
        lim = _live_limit_w(live)
        if pw is not None and lim is not None and lim > 0:
            have = 100.0 * pw / lim
        else:
            have = _f(live.get("power_pct_reported"))
        if have is None:
            return want > 50
        return want > float(have) + 5.0
    return False


def _filter_downward_cmds(
    cmds: list[tuple[str, object]], live: dict
) -> list[tuple[str, object]]:
    """Keep only non-upward commands (downward / same / suspend)."""
    out: list[tuple[str, object]] = []
    for action, value in cmds:
        if _cmd_is_upward(action, value, live):
            continue
        out.append((action, value))
    return out


def _override_active(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _policy_lock:
        until = float(_policy_ctrl.get("override_until_ts") or 0)
    return until > now


def set_policy_override(minutes: float | None = None, *, clear: bool = False) -> dict:
    """Start override for N minutes, or clear. Safety Critical still applies."""
    with _policy_lock:
        if clear or not minutes or float(minutes) <= 0:
            _policy_ctrl["override_until_ts"] = 0.0
            msg = "OVERRIDE cleared · zone auto resumed"
        else:
            mins = max(1.0, min(24 * 60.0, float(minutes)))
            _policy_ctrl["override_until_ts"] = time.time() + mins * 60.0
            msg = f"OVERRIDE {mins:.0f}m · zone auto off · Safety on"
    _policy_log("ok", msg)
    return get_policy_status()


def _persist_force_stop(on: bool) -> None:
    """Write force_stop into config.json (keep other keys)."""
    try:
        path = _miner_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        existing["force_stop"] = bool(on)
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[policy] force_stop persist fail: {e}")


def get_force_stop() -> bool:
    with _policy_lock:
        return bool(_policy_ctrl.get("force_stop"))


def set_force_stop(on: bool, *, apply_now: bool = True) -> dict:
    """
    Emergency Force Stop: sticky Suspend.
    Ignores heat-zone auto and Dry Run until cleared.
    Safety Critical still may write (also suspend-oriented).
    """
    on = bool(on)
    with _policy_lock:
        prev = bool(_policy_ctrl.get("force_stop"))
        _policy_ctrl["force_stop"] = on
    _persist_force_stop(on)
    if on and not prev:
        _policy_log("ok", "FORCE_STOP ON · Suspend")
        if apply_now:
            try:
                apply_set("working", "sleep", DEFAULT_API_PASSWORD)
                _policy_log("ok", "APPLY working=suspend", source="force_stop")
            except Exception as e:
                _policy_log("err", f"FORCE_STOP write fail: {e}", source="force_stop")
    elif not on and prev:
        _policy_log("ok", "FORCE_STOP OFF")
    elif on:
        _policy_log("info", "FORCE_STOP still ON")
    return get_policy_status()


def _zone_entry_commands(z: dict | None) -> list[tuple[str, object]]:
    """Desired write list from zone actuator flags (not yet filtered by live)."""
    if not isinstance(z, dict):
        return []
    cmds: list[tuple[str, object]] = []
    if z.get("work_en"):
        w = str(z.get("work") or "resume").lower()
        cmds.append(("working", "suspend" if w in ("suspend", "sleep") else "resume"))
    if z.get("mode_en"):
        m = str(z.get("mode") or "low").lower()
        if m in ("low", "normal", "high"):
            cmds.append(("mode", m))
    if z.get("lim_en"):
        try:
            lim = int(float(z.get("lim", 0)))
            if 0 <= lim <= 20000:
                cmds.append(("power_limit", lim))
        except (TypeError, ValueError):
            pass
    if z.get("pct_en"):
        try:
            pct = int(float(z.get("pct", 0)))
            if 0 <= pct <= 100:
                cmds.append(("power_pct", pct))
        except (TypeError, ValueError):
            pass
    return cmds


def _live_work(live: dict) -> str:
    """resume | suspend from measured miner state (power / hashrate / mineroff)."""
    return "suspend" if _measured_work_state(live) == "sleep" else "resume"


def _desired_work_from_profile(profile: dict | None) -> str | None:
    """suspend | resume if zone profile enables Mining Control, else None."""
    if not isinstance(profile, dict) or not profile.get("work_en"):
        return None
    w = str(profile.get("work") or "resume").lower()
    return "suspend" if w in ("suspend", "sleep") else "resume"


def _live_mode(live: dict) -> str | None:
    m = str(live.get("mode_norm") or live.get("mode") or "").strip().lower()
    for k in ("low", "normal", "high"):
        if k in m:
            return k
    return None


def _live_limit_w(live: dict) -> float | None:
    # prefer power_limit_set (configured on ASIC); summary Power Limit is 0 in Suspend
    return _f(live.get("power_limit_set")) or _f(live.get("power_limit_measured")) or _f(
        live.get("power_limit")
    )


def _diff_commands_vs_live(
    desired: list[tuple[str, object]],
    live: dict,
    *,
    limit_tol_w: float = 100.0,
    pct_tol: float = 5.0,
) -> list[tuple[str, object]]:
    """
    Only keep commands where miner state differs from desired.
    Zone unchanged + state already correct → empty list (no write, no notify).
    """
    out: list[tuple[str, object]] = []
    for action, value in desired:
        if action == "working":
            want = "suspend" if str(value).lower() in ("suspend", "sleep") else "resume"
            have = _live_work(live)
            if want != have:
                out.append(("working", want))
        elif action == "mode":
            want = str(value).strip().lower()
            have = _live_mode(live)
            if have is None or have != want:
                out.append(("mode", want))
        elif action == "power_limit":
            want = int(value)
            have = _live_limit_w(live)
            if have is None or abs(float(have) - want) > float(limit_tol_w):
                out.append(("power_limit", want))
        elif action == "power_pct":
            want = int(value)
            # Prefer estimate Power/Limit; fall back to hash_percent if >0
            have = None
            pw = _f(live.get("power"))
            lim = _live_limit_w(live)
            if pw is not None and lim is not None and lim > 0:
                have = 100.0 * pw / lim
            else:
                have = _f(live.get("power_pct_reported"))
                if have is not None and have <= 0:
                    have = None
            if have is None or abs(float(have) - want) > float(pct_tol):
                out.append(("power_pct", want))
    return out


def _policy_apply_commands(cmds: list[tuple[str, object]], source: str) -> bool:
    ok_all = True
    for action, value in cmds:
        # TCP-only actions: skip during over-max backoff (LuCI mode still ok)
        act = str(action or "").lower()
        needs_tcp = act in (
            "working",
            "working_mode",
            "work",
            "mining",
            "power_pct",
            "power_limit",
            "set_power_limit",
            "adjust_power_limit",
            "factory_reset",
            "factory",
        )
        if needs_tcp and _tcp_write_blocked():
            ok_all = False
            _policy_log(
                "err",
                f"SKIP {action}={value}: TCP write backoff (over max connect)",
                source=source,
            )
            break
        try:
            apply_set(action, value, DEFAULT_API_PASSWORD)
            _policy_log("ok", f"APPLY {action}={value}", source=source)
        except Exception as e:
            ok_all = False
            err_s = str(e)
            if _msg_is_over_max_connect(err_s):
                _note_tcp_write_exhausted()
            _policy_log("err", f"FAIL {action}={value}: {e}", source=source)
            break
    _invalidate_cache()
    return ok_all


def get_policy_status() -> dict:
    now = time.time()
    with _policy_lock:
        ctrl = dict(_policy_ctrl)
        events = list(ctrl.get("events") or [])
        ov_until = float(ctrl.get("override_until_ts") or 0)
        warmup_since = ctrl.get("warmup_since_ts")
    with _miner_cfg_lock:
        dry = bool(DRY_RUN)
        poll = int(POLL_INTERVAL_SEC)
    zc = get_zone_cfg()
    ov_rem = max(0, int(ov_until - now)) if ov_until > now else 0
    return {
        "ok": True,
        "server_side": True,
        "enabled": bool(ctrl.get("enabled", True)),
        "dry_run": dry,
        "poll_interval_sec": poll,
        "heat_zone": ctrl.get("heat_zone"),
        "heat_zone_title": (
            zone_title("critical")
            if ctrl.get("safety_sticky")
            else zone_title(ctrl.get("heat_zone"))
        ),
        "t_ctrl": ctrl.get("t_ctrl"),
        "t_ctrl_sensor": _normalize_t_ctrl_sensor(
            ctrl.get("t_ctrl_sensor") or zc.get("t_ctrl_sensor")
        ),
        "safety_sticky": bool(ctrl.get("safety_sticky")),
        "last_key": ctrl.get("last_key"),
        "streak_key": ctrl.get("streak_key"),
        "streak_count": int(ctrl.get("streak_count") or 0),
        "want_work": ctrl.get("want_work"),
        "measured_work": ctrl.get("measured_work"),
        "force_stop": bool(ctrl.get("force_stop")),
        "last_apply_ts": ctrl.get("last_apply_ts"),
        "last_event": ctrl.get("last_event"),
        "events": events[:40],
        "override_active": ov_until > now,
        "override_until_ts": ov_until if ov_until > now else None,
        "override_remaining_sec": ov_rem,
        "warmup_since_ts": warmup_since,
        "thresholds": {
            "t0": zc.get("t0"),
            "t1": zc.get("t1"),
            "t2": zc.get("t2"),
            "h": zc.get("h"),
            "t_crit": zc.get("t_crit"),
            "t_crit_clear": zc.get("t_crit_clear"),
            "t_ctrl_sensor": _normalize_t_ctrl_sensor(zc.get("t_ctrl_sensor")),
            "t_ctrl_sensors": list(T_CTRL_SENSORS),
            "dwell_sec": zc.get("dwell_sec"),
            "settle_sec": zc.get("settle_sec"),
            "streak": zc.get("streak"),
            "min_write_interval_sec": zc.get("min_write_interval_sec"),
            "limit_tol_w": zc.get("limit_tol_w"),
            "warmup_en": bool(zc.get("warmup_en", True)),
            "warmup_downward_only": bool(zc.get("warmup_downward_only", True)),
            "max_warmup_wait_min": int(zc.get("max_warmup_wait_min", 30) or 30),
        },
    }


def policy_tick() -> None:
    """
    One control cycle — write ONLY when measured miner state ≠ desired for active profile.
    Zone stays Z2 and Suspend already on → silent (no write, no toast spam).
    """
    global DRY_RUN
    with _policy_lock:
        if not _policy_ctrl.get("enabled", True):
            return
        heat_sticky = _policy_ctrl.get("heat_zone")
        safety_sticky = bool(_policy_ctrl.get("safety_sticky"))
        last_key = _policy_ctrl.get("last_key")
        streak_key = _policy_ctrl.get("streak_key")
        streak_count = int(_policy_ctrl.get("streak_count") or 0)
        last_apply_ts = float(_policy_ctrl.get("last_apply_ts") or 0)

    try:
        live = fetch_live()
        with _cache_lock:
            global _cache, _cache_ts
            _cache = live
            _cache_ts = time.time()
        tg_note_live_poll_ok()
    except Exception as e:
        _policy_log("warn", f"live poll fail: {e}")
        try:
            tg_note_live_poll_fail(e)
        except Exception:
            pass
        return

    liquid = _f(live.get("liquid"))
    chip_max = _f(live.get("chip_max"))
    upfreq_block = _upfreq_block(live)

    zc = get_zone_cfg()
    t0 = float(zc.get("t0", 24))
    t1 = float(zc.get("t1", 26))
    t2 = float(zc.get("t2", 28))
    h = float(zc.get("h", 0.5))
    t_crit = float(zc.get("t_crit", 70))
    t_crit_clear = float(zc.get("t_crit_clear", 65))
    t_ctrl_sensor = _normalize_t_ctrl_sensor(zc.get("t_ctrl_sensor"))
    t_ctrl, t_ctrl_sensor = resolve_t_ctrl(live, t_ctrl_sensor)
    streak_need = max(1, int(zc.get("streak", 3) or 3))
    dwell_sec = max(0, int(zc.get("dwell_sec", 600) or 0))
    settle_sec = max(0, int(zc.get("settle_sec", 300) or 0))
    min_write = max(10, int(zc.get("min_write_interval_sec", 60) or 60))
    limit_tol = float(zc.get("limit_tol_w", 100) or 100)
    warmup_en = bool(zc.get("warmup_en", True))
    warmup_downward_only = bool(zc.get("warmup_downward_only", True))
    max_warmup_wait_min = max(1, min(240, int(zc.get("max_warmup_wait_min", 30) or 30)))
    zones = zc.get("zones") or {}

    now_ts = time.time()
    # warmup timer for max_warmup_wait_min
    with _policy_lock:
        if upfreq_block and warmup_en:
            if not _policy_ctrl.get("warmup_since_ts"):
                _policy_ctrl["warmup_since_ts"] = now_ts
            warmup_since = float(_policy_ctrl.get("warmup_since_ts") or now_ts)
        else:
            _policy_ctrl["warmup_since_ts"] = None
            warmup_since = None
        ov_until = float(_policy_ctrl.get("override_until_ts") or 0)
    override_on = ov_until > now_ts
    warmup_expired = bool(
        warmup_since is not None
        and (now_ts - warmup_since) >= (max_warmup_wait_min * 60.0)
    )
    warmup_active = bool(warmup_en and upfreq_block and not warmup_expired)

    # --- safety sticky ---
    was_safety = safety_sticky
    if chip_max is not None:
        if chip_max >= t_crit:
            safety_sticky = True
        elif chip_max <= t_crit_clear:
            safety_sticky = False

    # Heat map uses selected T_ctrl sensor (default liquid; env if liquid N/A)
    heat_zone = _evaluate_heat_zone(t_ctrl, heat_sticky, t0, t1, t2, h)
    with _policy_lock:
        _policy_ctrl["t_ctrl"] = t_ctrl
        _policy_ctrl["t_ctrl_sensor"] = t_ctrl_sensor

    with _miner_cfg_lock:
        dry = bool(DRY_RUN)
    with _policy_lock:
        force_stop = bool(_policy_ctrl.get("force_stop"))

    desired: str | None = None
    profile: dict | None = None
    is_safety = False

    if safety_sticky:
        desired = "safety:on_crit"
        crit = zones.get("critical") or {}
        profile = crit.get("on_crit") if isinstance(crit, dict) else None
        is_safety = True
    elif was_safety and not safety_sticky and not force_stop:
        # after Critical: on_clear — blocked while Force Stop (would resume)
        desired = "safety:on_clear"
        crit = zones.get("critical") or {}
        profile = crit.get("on_clear") if isinstance(crit, dict) else None
        is_safety = True
    elif force_stop:
        # Emergency: no zone auto; Suspend enforced later (above Dry Run)
        desired = None
        profile = None
        if last_key != "force_stop":
            # short note once when policy enters FS (ON already logged in set_force_stop)
            _policy_log("info", "FORCE_STOP active")
            with _policy_lock:
                _policy_ctrl["last_key"] = "force_stop"
            last_key = "force_stop"
    elif override_on:
        # Manual Override: no zone auto; Safety still handled above
        desired = None
        if last_key != "override":
            rem = int(ov_until - now_ts)
            _policy_log(
                "info",
                f"OVERRIDE active · {rem // 60}m{rem % 60:02d}s left · zone auto paused",
            )
            with _policy_lock:
                _policy_ctrl["last_key"] = "override"
            last_key = "override"
    elif not dry:
        if warmup_active and heat_zone == "z0":
            # cannot go Normal heat while upfreq incomplete
            desired = None
            if last_key != "block:upfreq":
                _policy_log(
                    "info",
                    "WARMUP · Z0 blocked (upfreq) · downward/Critical only"
                    if warmup_downward_only
                    else "WARMUP · Z0 blocked (upfreq)",
                )
                with _policy_lock:
                    _policy_ctrl["last_key"] = "block:upfreq"
                last_key = "block:upfreq"
        else:
            desired = heat_zone
            profile = zones.get(heat_zone)
            if warmup_active and warmup_expired:
                pass  # allow full profile after max wait
    else:
        # Dry Run: keep current miner mode — zones are preview only (no auto write).
        # Safety Critical (chip) still applies via is_safety branch above.
        desired = None
        profile = None
        dry_tag = "dry:" + str(heat_zone)
        if last_key != dry_tag:
            would = _diff_commands_vs_live(
                _zone_entry_commands(zones.get(heat_zone)),
                live,
                limit_tol_w=limit_tol,
            )
            if warmup_active and warmup_downward_only and would:
                would = _filter_downward_cmds(would, live)
            # also note zone MC if it would change work
            wp = zones.get(heat_zone)
            ww = _desired_work_from_profile(wp)
            mw = _live_work(live)
            if ww and mw and ww != mw:
                would = list(would) + [("working", ww)]
            if would:
                _policy_log(
                    "info",
                    f"DRY_RUN would {heat_zone}: "
                    + ", ".join(f"{a}={v}" for a, v in would)
                    + " · no write (Dry Run)",
                    heat_zone=heat_zone,
                )
            with _policy_lock:
                _policy_ctrl["last_key"] = dry_tag
            last_key = dry_tag

    # persist sticky state
    with _policy_lock:
        _policy_ctrl["heat_zone"] = heat_zone
        _policy_ctrl["safety_sticky"] = safety_sticky

    # Desired profile for this tick (None in dry_run unless Safety)
    desired_cmds = _zone_entry_commands(profile) if desired else []
    # Diff vs measured — heart of "don't spam if already correct"
    need_cmds = (
        _diff_commands_vs_live(desired_cmds, live, limit_tol_w=limit_tol)
        if desired_cmds
        else []
    )

    # Warmup: strip upward commands (mode↑ limit↑ resume-from-mining) unless Safety
    # Only while actually mining with incomplete upfreq — not while Suspended.
    warmup_dropped: list[str] = []
    if (
        need_cmds
        and warmup_active
        and warmup_downward_only
        and not is_safety
    ):
        before = list(need_cmds)
        need_cmds = _filter_downward_cmds(need_cmds, live)
        warmup_dropped = [
            f"{a}={v}" for a, v in before if (a, v) not in need_cmds
        ]
        if warmup_dropped and last_key != "block:warmup_up":
            _policy_log(
                "info",
                "WARMUP block upward: " + ", ".join(warmup_dropped),
            )
            with _policy_lock:
                _policy_ctrl["last_key"] = "block:warmup_up"
            last_key = "block:warmup_up"

    # ── Mining Control compliance every poll ─────────────────────────────
    # Zone/Safety profile work_en defines desired Suspend|Resume.
    # Force Stop: always Suspend (above zones & Dry Run).
    # Dry Run: do NOT enforce zone MC — operator keeps current mode.
    # Override: operator owns control (Safety still uses is_safety profile above).
    measured_work = _live_work(live)
    work_profile = profile
    if work_profile is None and not is_safety and heat_zone and not dry and not force_stop:
        work_profile = zones.get(heat_zone)
    if force_stop:
        want_work = "suspend"
    elif dry and not is_safety:
        # zones ignored: no want_work from map / sticky last suspend
        want_work = None
    else:
        want_work = _desired_work_from_profile(work_profile)
        last_wc = str(live.get("work_cmd") or "").strip().lower()
        # sticky last suspend cmd (manual Force Suspend) if zone has no work_en
        if want_work is None and last_wc in ("sleep", "suspend") and not override_on:
            want_work = "suspend"

    # always expose for UI /api/policy (even when already matched)
    with _policy_lock:
        _policy_ctrl["want_work"] = want_work
        _policy_ctrl["measured_work"] = measured_work

    work_mismatch = bool(
        want_work
        and measured_work
        and want_work != measured_work
        and not override_on
        and not (dry and not is_safety and not force_stop)
    )
    if work_mismatch:
        already = any(
            a == "working"
            and (
                (want_work == "suspend" and str(v).lower() in ("suspend", "sleep"))
                or (want_work == "resume" and str(v).lower() in ("resume", "power_on"))
            )
            for a, v in need_cmds
        )
        if not already:
            need_cmds = list(need_cmds) + [("working", want_work)]
        # name the source for logs / streak
        if not desired:
            desired = "force_stop" if force_stop else f"enforce:{want_work}"
        elif want_work == "suspend" and measured_work == "resume":
            # keep zone name but mark as work re-enforce in log via desired
            pass

    # Dry Run hard stop: no zone auto-writes at all (MC included).
    # Only Safety Critical (is_safety) may write — unless Force Stop (below).
    if dry and not is_safety and not force_stop:
        need_cmds = []

    # Force Stop: always Suspend; strip resume; ignore Dry Run for this write
    if force_stop:
        need_cmds = [
            (a, v)
            for a, v in need_cmds
            if not (
                a == "working"
                and str(v).lower() in ("resume", "power_on", "mining")
            )
        ]
        if measured_work and measured_work != "suspend":
            if not any(
                a == "working" and str(v).lower() in ("suspend", "sleep")
                for a, v in need_cmds
            ):
                need_cmds = list(need_cmds) + [("working", "suspend")]
            desired = "force_stop"
        else:
            # already suspended — stay silent (no resume from zones/safety on_clear)
            need_cmds = [
                (a, v)
                for a, v in need_cmds
                if a != "working"
            ]
            # under force_stop, also skip non-safety power nudges
            if not is_safety:
                need_cmds = []
            desired = desired or "force_stop"

    # Filtration (Tapo P100): force ON while mining; optional OFF on suspend
    try:
        filtration_sync_with_mining(measured_work)
    except Exception as e:
        print(f"[filtration] sync: {e}")

    if not need_cmds:
        # Already matches miner — no write, no notification
        # Keep warmup block key so we don't spam the same log every poll
        with _policy_lock:
            if warmup_dropped:
                _policy_ctrl["last_key"] = "block:warmup_up"
                _policy_ctrl["streak_key"] = "block:warmup_up"
            elif desired:
                _policy_ctrl["last_key"] = desired
                _policy_ctrl["streak_key"] = desired
            _policy_ctrl["streak_count"] = 0
        return

    # streak only for pending mismatches
    mismatch_sig = str(desired) + "|" + ",".join(f"{a}={v}" for a, v in need_cmds)
    if streak_key == mismatch_sig:
        streak_count += 1
    else:
        streak_key = mismatch_sig
        streak_count = 1
    # Mining Control only / safety / re-enforce: faster confirm (1–2 samples)
    work_only = all(a == "working" for a, v in need_cmds) and bool(need_cmds)
    suspend_only = all(
        a == "working" and str(v).lower() in ("suspend", "sleep") for a, v in need_cmds
    )
    work_enforce = work_only or str(desired or "").startswith("enforce:")
    if is_safety or suspend_only or work_enforce:
        # suspend after reboot: act on 1–2 polls, not full streak=3
        need = 1 if suspend_only else min(2, streak_need)
    else:
        need = streak_need
    with _policy_lock:
        _policy_ctrl["streak_key"] = streak_key
        _policy_ctrl["streak_count"] = streak_count

    if streak_count < need:
        return

    now = time.time()
    # After write: wait settle before next reconcile.
    # Work re-enforce (esp. Suspend after power-on): short settle so miner
    # cannot free-run for minutes while Z3 expects No heat.
    if suspend_only or (work_enforce and want_work == "suspend"):
        settle_use = min(settle_sec, 20)
        min_write_use = min(min_write, 15)
    elif work_only:
        settle_use = min(settle_sec, 45)
        min_write_use = min(min_write, 20)
    else:
        settle_use = settle_sec
        min_write_use = min_write
    if last_apply_ts and (now - last_apply_ts) < settle_use:
        return
    if last_apply_ts and (now - last_apply_ts) < min_write_use:
        return
    # Dwell only when *changing* zone profile (z1→z2), not work re-fix
    same_profile = last_key == desired or suspend_only or work_only
    if (
        not is_safety
        and not same_profile
        and last_apply_ts
        and (now - last_apply_ts) < dwell_sec
    ):
        return

    _policy_log(
        "ok",
        f"AUTO {desired} · fix "
        + ", ".join(f"{a}={v}" for a, v in need_cmds)
        + f" (have mode={_live_mode(live)} work={measured_work}"
        + (f" want_work={want_work}" if want_work else "")
        + f" P={_f(live.get('power'))} TH={_f(live.get('hashrate_th'))} "
        f"lim={_live_limit_w(live)})",
        dry_run=dry,
        liquid=liquid,
        t_ctrl=t_ctrl,
        t_ctrl_sensor=t_ctrl_sensor,
        chip_max=chip_max,
    )
    ok = _policy_apply_commands(need_cmds, source=str(desired))
    with _policy_lock:
        _policy_ctrl["last_key"] = None if desired == "safety:on_clear" else desired
        # Always stamp last_apply_ts: on FAIL (esp. over max) this enforces
        # settle/min_write so we do not spam get_token every poll cycle.
        _policy_ctrl["last_apply_ts"] = time.time()
        if ok:
            _policy_ctrl["streak_count"] = 0


def policy_loop() -> None:
    """Background control loop (independent of browser UI)."""
    # short delay so HTTP server can bind first
    _policy_stop.wait(timeout=2.0)
    while not _policy_stop.is_set():
        try:
            policy_tick()
        except Exception as e:
            _policy_log("err", f"tick error: {e}")
        with _miner_cfg_lock:
            interval = max(2, int(POLL_INTERVAL_SEC))
        _policy_stop.wait(timeout=interval)


# ─── collector ────────────────────────────────────────────────────────────────


def collector_loop() -> None:
    sample_n = 0
    while not _collector_stop.is_set():
        with _hist_cfg_lock:
            enabled = bool(_hist_cfg.get("enabled", True))
            interval = int(_hist_cfg.get("sample_interval_sec", 30))
            retention = int(_hist_cfg.get("retention_days", 7))
            prune_every = int(_hist_cfg.get("prune_every_samples", 20))

        if enabled:
            try:
                live = fetch_live()
                # refresh cache too
                global _cache, _cache_ts
                with _cache_lock:
                    _cache = live
                    _cache_ts = time.time()
                insert_sample(live_to_sample(live))
                sample_n += 1
                if sample_n % max(1, prune_every) == 0:
                    prune_old(retention)
            except Exception as e:
                # record offline marker so charts can show ASIC Offline bands
                try:
                    insert_sample(offline_sample(str(e)))
                    sample_n += 1
                except Exception:
                    pass
                print(f"[collector] {datetime.now().isoformat(timespec='seconds')} error: {e}")

        # wait interval, but wake early on stop
        _collector_stop.wait(timeout=max(5, interval))


# ─── HTTP ─────────────────────────────────────────────────────────────────────


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        msg = fmt % args if args else fmt
        if "/api/" in str(msg):
            return
        super().log_message(fmt, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/live", "/api/status"):
            self._api_live()
            return
        if path == "/api/history":
            self._api_history()
            return
        if path == "/api/history/config":
            self._api_history_config_get()
            return
        if path == "/api/history/stats":
            self._json_response(200, {"ok": True, **history_stats(), "config": dict(_hist_cfg)})
            return
        if path == "/api/weather":
            self._api_weather_get()
            return
        if path == "/api/weather/config":
            self._api_weather_config_get()
            return
        if path == "/api/weather/search":
            self._api_weather_search()
            return
        if path == "/api/weather/presets":
            self._json_response(200, {"ok": True, "presets": WEATHER_PRESETS})
            return
        if path in ("/api/telegram/config", "/api/telegram"):
            self._json_response(200, {"ok": True, "config": get_telegram_cfg(redact=True)})
            return
        if path == "/api/telegram/status":
            self._json_response(200, {"ok": True, **get_telegram_cfg(redact=True)})
            return
        if path in ("/api/telegram/timing", "/api/telegram/latency"):
            qs = parse_qs(urlparse(self.path).query)
            try:
                lim = int((qs.get("limit") or ["100"])[0])
            except (TypeError, ValueError):
                lim = 100
            self._json_response(200, get_tg_timing(limit=lim))
            return
        if path == "/api/pool/config":
            self._api_pool_config_get()
            return
        if path == "/api/zone/config":
            self._api_zone_config_get()
            return
        if path in ("/api/zone/presets", "/api/zone/preset"):
            self._api_zone_presets_get()
            return
        if path in (
            "/api/miner/pools/presets",
            "/api/pools/presets",
            "/api/pool-presets",
        ):
            self._api_pool_presets_get()
            return
        if path in ("/api/filtration", "/api/filtration/status", "/api/filtration/config"):
            self._json_response(200, get_filtration_status())
            return
        if path in ("/api/chipmap", "/api/chips", "/api/chipmap/status"):
            qs = parse_qs(urlparse(self.path).query)
            force = str((qs.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
            self._json_response(200, get_chipmap(force=force))
            return
        if path in ("/api/chipmap/config",):
            self._json_response(200, {"ok": True, "config": get_chipmap_cfg(redact=True)})
            return
        if path in (
            "/api/luci_proxy",
            "/api/luci_proxy/config",
            "/api/luci-proxy",
            "/api/luci-proxy/config",
        ):
            self._json_response(200, get_luci_proxy_cfg())
            return
        if path in ("/api/miner/errors", "/api/errors/log"):
            self._api_miner_error_log()
            return
        if path == "/api/miner/config":
            self._api_miner_config_get()
            return
        if path in ("/api/config/backup", "/api/backup/config", "/api/config/export"):
            self._api_config_backup_get()
            return
        if path in (
            "/api/miner/write_api",
            "/api/miner/write-status",
            "/api/miner/api_switch",
        ):
            try:
                self._json_response(200, get_write_api_status())
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return
        if path in ("/api/miner/pools", "/api/pools"):
            qs = parse_qs(urlparse(self.path).query)
            force = str((qs.get("force") or ["0"])[0]).lower() in (
                "1",
                "true",
                "yes",
            )
            self._json_response(200, fetch_mining_pools(force=force))
            return
        if path in ("/api/policy", "/api/policy/status"):
            self._json_response(200, get_policy_status())
            return
        if path in ("/api/system/info", "/api/info"):
            qs = parse_qs(urlparse(self.path).query)
            cached_only = str((qs.get("cached") or ["0"])[0]).lower() in (
                "1",
                "true",
                "yes",
            )
            if cached_only:
                c = load_info_cache()
                if c:
                    self._json_response(200, {**c, "from_cache": True})
                else:
                    self._json_response(
                        404, {"ok": False, "error": "no info cache yet", "from_cache": True}
                    )
                return
            try:
                self._json_response(200, collect_system_info())
            except Exception as e:
                # fallback to cache on live failure
                c = load_info_cache()
                if c:
                    self._json_response(
                        200, {**c, "from_cache": True, "live_error": str(e)}
                    )
                else:
                    self._json_response(500, {"ok": False, "error": str(e)})
            return
        if path in ("/api/version", "/api/update/status"):
            self._json_response(
                200,
                {
                    "ok": True,
                    **get_version_info(),
                    "busy": bool(_update_state.get("busy")),
                    "last_check": _update_state.get("last_check"),
                    "last_apply": _update_state.get("last_apply"),
                },
            )
            return
        if path == "/api/update/check":
            try:
                self._json_response(200, check_github_update())
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return
        # SPA tab routes: / · /dashboard · /miner · # handled client-side
        spa_tabs = {
            "miner",
            "map",
            "pool",
            "settings",
            "info",
            "logs",
            "dash",
            "dashboard",
            "overview",
            "zones",
            "zone",
            "home",
        }
        seg = path.strip("/").split("/")[0].lower() if path.strip("/") else ""
        if path == "/" or path == "/index.html" or seg in spa_tabs:
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/set", "/api/control"):
            self._api_set()
            return
        if path == "/api/history/config":
            self._api_history_config_post()
            return
        if path == "/api/weather/config":
            self._api_weather_config_post()
            return
        if path in ("/api/telegram/config", "/api/telegram"):
            self._api_telegram_config_post()
            return
        if path in ("/api/telegram/test", "/api/telegram/send_test"):
            try:
                self._json_response(200, tg_test_send())
            except Exception as e:
                self._json_response(400, {"ok": False, "error": str(e)})
            return
        if path == "/api/pool/config":
            self._api_pool_config_post()
            return
        if path == "/api/zone/config":
            self._api_zone_config_post()
            return
        if path in ("/api/zone/presets", "/api/zone/preset"):
            self._api_zone_presets_post()
            return
        if path in (
            "/api/miner/pools/presets",
            "/api/pools/presets",
            "/api/pool-presets",
        ):
            self._api_pool_presets_post()
            return
        if path in ("/api/filtration", "/api/filtration/config"):
            self._api_filtration_post()
            return
        if path in ("/api/filtration/set", "/api/filtration/on", "/api/filtration/off"):
            self._api_filtration_set()
            return
        if path == "/api/filtration/test":
            self._api_filtration_test()
            return
        if path in ("/api/chipmap/config",):
            try:
                req = self._read_json_body() or {}
                cfg = apply_chipmap_cfg(req if isinstance(req, dict) else {})
                self._json_response(200, {"ok": True, "config": cfg})
            except Exception as e:
                self._json_response(400, {"ok": False, "error": str(e)})
            return
        if path in (
            "/api/luci_proxy",
            "/api/luci_proxy/config",
            "/api/luci-proxy",
            "/api/luci-proxy/config",
        ):
            try:
                req = self._read_json_body() or {}
                body = apply_luci_proxy_cfg(req if isinstance(req, dict) else {})
                self._json_response(200, body)
            except Exception as e:
                self._json_response(400, {"ok": False, "error": str(e)})
            return
        if path in ("/api/chipmap/refresh", "/api/chipmap/poll"):
            self._json_response(200, get_chipmap(force=True))
            return
        if path == "/api/miner/config":
            self._api_miner_config_post()
            return
        if path in ("/api/config/restore", "/api/backup/restore", "/api/config/import"):
            self._api_config_restore_post()
            return
        if path in (
            "/api/miner/write_api",
            "/api/miner/write-status",
            "/api/miner/api_switch",
            "/api/miner/enable_write",
            "/api/miner/enable_api",
        ):
            try:
                req: dict = {}
                try:
                    raw = self._read_json_body()
                    if isinstance(raw, dict):
                        req = raw
                except Exception:
                    req = {}
                action = str(req.get("action") or "status").strip().lower()
                # path /enable_* defaults to enable
                if "enable" in path and action in ("", "status"):
                    action = "enable"
                pw = req.get("password") or req.get("api_password")
                pw = str(pw) if pw is not None else None
                if action in ("enable", "on", "unlock", "switch_on"):
                    out = enable_write_api(
                        password=pw,
                        new_password=(
                            str(req["new_password"])
                            if req.get("new_password") is not None
                            else None
                        ),
                    )
                else:
                    out = get_write_api_status(pw)
                self._json_response(200, out)
            except Exception as e:
                self._json_response(400, {"ok": False, "error": str(e)})
            return
        if path == "/api/policy":
            self._api_policy_post()
            return
        if path == "/api/miner/test":
            self._api_miner_test()
            return
        if path == "/api/history/prune":
            with _hist_cfg_lock:
                days = int(_hist_cfg.get("retention_days", 7))
            n = prune_old(days)
            self._json_response(200, {"ok": True, "deleted": n, "retention_days": days})
            return
        if path in ("/api/history/clear", "/api/history/reset"):
            try:
                n = clear_history_samples()
                self._json_response(
                    200,
                    {"ok": True, "deleted": n, "stats": history_stats()},
                )
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return
        if path in ("/api/miner/errors/clear", "/api/errors/log/clear"):
            try:
                n = clear_miner_error_log()
                self._json_response(200, {"ok": True, "deleted": n})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return
        if path in ("/api/update/check",):
            try:
                self._json_response(200, check_github_update())
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return
        if path in ("/api/update/apply", "/api/update/install"):
            try:
                req = self._read_json_body()
            except Exception:
                req = {}
            if not isinstance(req, dict):
                req = {}
            ref = req.get("ref") or req.get("tag") or req.get("version")
            confirm = req.get("confirm")
            if confirm not in (True, "yes", "true", 1, "1"):
                self._json_response(
                    400,
                    {
                        "ok": False,
                        "error": "confirm=true required to install update from GitHub",
                    },
                )
                return
            try:
                result = apply_github_update(ref=str(ref) if ref else None)
                code = 200 if result.get("ok") else 500
                self._json_response(code, result)
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return
        self.send_error(404)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _json_response(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api_config_backup_get(self) -> None:
        """GET /api/config/backup — download full settings JSON."""
        try:
            body = build_config_backup()
            data = json.dumps(body, ensure_ascii=False, indent=2, default=str).encode(
                "utf-8"
            )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            pname = re.sub(
                r"[^\w.\-]+",
                "_",
                str(body.get("project_name") or "poolheat"),
            )[:40]
            fname = f"{pname}_config_{stamp}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{fname}"'
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json_response(500, {"ok": False, "error": str(e)})

    def _api_config_restore_post(self) -> None:
        """POST /api/config/restore — upload backup JSON and apply."""
        try:
            req = self._read_json_body()
            if not isinstance(req, dict):
                raise ValueError("JSON object required")
            # allow { backup: {...}, sections: [...] } or raw backup
            payload = req.get("backup") if isinstance(req.get("backup"), dict) else req
            sections = req.get("sections") if isinstance(req.get("sections"), list) else None
            if sections is not None:
                sections = [str(s) for s in sections]
            out = restore_config_backup(payload, sections=sections)
            self._json_response(200, out)
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_live(self) -> None:
        global _cache, _cache_ts
        now = time.time()
        with _cache_lock:
            use_cache = (
                _cache is not None
                and (now - _cache_ts) < CACHE_TTL
                and _cache.get("ok")
            )
            if use_cache:
                body = dict(_cache)
            else:
                try:
                    body = fetch_live()
                    _cache = body
                    _cache_ts = now
                except Exception as e:
                    body = {
                        "ok": False,
                        "error": str(e),
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "host": f"{HOST_MINER}:{PORT_MINER}",
                    }
        with _state_lock:
            body["power_pct_cmd"] = _state.get("power_pct_cmd")
            body["power_limit_cmd"] = _state.get("power_limit_cmd")
            body["mode_cmd"] = _state.get("mode_cmd")
            body["work_cmd"] = _state.get("work_cmd")
            body["last_write"] = _state.get("last_write")
            # compact last miner response for UI error panel
            lwr = _state.get("last_write_result")
            if isinstance(lwr, dict):
                body["last_write_result"] = {
                    k: lwr.get(k)
                    for k in ("STATUS", "status", "Msg", "msg", "Code", "Description")
                    if k in lwr
                } or {"raw_keys": list(lwr.keys())[:12]}
            else:
                body["last_write_result"] = lwr
        self._json_response(200 if body.get("ok") else 502, body)

    def _api_set(self) -> None:
        try:
            req = self._read_json_body()
            action = req.get("action") or req.get("cmd")
            value = req.get("value")
            password = req.get("password") or DEFAULT_API_PASSWORD
            result = apply_set(action, value, password)
            self._json_response(200, result)
        except Exception as e:
            # still expose last_write if recorded before raise
            with _state_lock:
                lw = _state.get("last_write")
            self._json_response(
                400,
                {
                    "ok": False,
                    "error": str(e),
                    "last_write": lw,
                },
            )

    def _api_history(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        def one(key, default=None):
            v = qs.get(key, [default])[0]
            return v

        try:
            hours = one("hours")
            hours_f = float(hours) if hours not in (None, "") else None
            max_points = int(one("max", "2000") or 2000)
            since = one("since")
            until = one("until")
            since_f = float(since) if since not in (None, "") else None
            until_f = float(until) if until not in (None, "") else None

            points = query_history(
                hours=hours_f,
                since=since_f,
                until=until_f,
                max_points=max_points,
            )
            with _hist_cfg_lock:
                cfg = dict(_hist_cfg)
            self._json_response(
                200,
                {
                    "ok": True,
                    "count": len(points),
                    "config": cfg,
                    "stats": history_stats(),
                    "points": points,
                },
            )
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_history_config_get(self) -> None:
        with _hist_cfg_lock:
            cfg = dict(_hist_cfg)
        self._json_response(200, {"ok": True, "config": cfg, "stats": history_stats()})

    def _api_history_config_post(self) -> None:
        try:
            req = self._read_json_body()
            with _hist_cfg_lock:
                if "enabled" in req:
                    _hist_cfg["enabled"] = bool(req["enabled"])
                if "retention_days" in req:
                    _hist_cfg["retention_days"] = max(1, min(90, int(req["retention_days"])))
                if "sample_interval_sec" in req:
                    _hist_cfg["sample_interval_sec"] = max(
                        5, min(3600, int(req["sample_interval_sec"]))
                    )
                cfg = dict(_hist_cfg)
            _save_hist_cfg()
            # prune immediately if retention shortened
            deleted = prune_old(cfg["retention_days"])
            self._json_response(
                200, {"ok": True, "config": cfg, "pruned": deleted, "stats": history_stats()}
            )
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_weather_get(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        force = (qs.get("force", ["0"])[0] or "0") in ("1", "true", "yes")
        body = fetch_weather_current(force=force)
        code = 200 if body.get("ok") or body.get("stale") else 502
        self._json_response(code, body)

    def _api_weather_config_get(self) -> None:
        with _weather_cfg_lock:
            cfg = dict(_weather_cfg)
        self._json_response(200, {"ok": True, "config": cfg, "presets": WEATHER_PRESETS})

    def _api_weather_config_post(self) -> None:
        global _weather_cache, _weather_cache_ts
        try:
            req = self._read_json_body()
            with _weather_cfg_lock:
                if "enabled" in req:
                    _weather_cfg["enabled"] = bool(req["enabled"])
                for key in ("city", "country", "admin1", "timezone"):
                    if key in req and req[key] is not None:
                        _weather_cfg[key] = str(req[key])
                if "latitude" in req:
                    _weather_cfg["latitude"] = float(req["latitude"])
                if "longitude" in req:
                    _weather_cfg["longitude"] = float(req["longitude"])
                if "refresh_interval_sec" in req and req["refresh_interval_sec"] is not None:
                    _weather_cfg["refresh_interval_sec"] = _weather_refresh_sec(
                        {"refresh_interval_sec": req["refresh_interval_sec"]}
                    )
                cfg = dict(_weather_cfg)
            _save_weather_cfg()
            with _weather_cache_lock:
                _weather_cache = None
                _weather_cache_ts = 0.0
            weather = fetch_weather_current(cfg, force=True)
            self._json_response(200, {"ok": True, "config": cfg, "weather": weather})
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_weather_search(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q", [""])[0] or "").strip()
        try:
            count = int(qs.get("count", ["12"])[0] or 12)
        except ValueError:
            count = 12
        try:
            results = weather_search_cities(q, count=count)
            self._json_response(200, {"ok": True, "query": q, "results": results})
        except Exception as e:
            self._json_response(502, {"ok": False, "error": str(e), "results": []})

    def _api_telegram_config_post(self) -> None:
        global _tg_cfg
        try:
            req = self._read_json_body()
            if not isinstance(req, dict):
                raise ValueError("JSON object required")
            pending_rm = None
            with _tg_cfg_lock:
                if "enabled" in req:
                    _tg_cfg["enabled"] = bool(req["enabled"])
                if "bot_token" in req and req["bot_token"] is not None:
                    tok = str(req["bot_token"]).strip()
                    # keep previous if client sent redacted placeholder
                    if tok and "…" not in tok and not tok.startswith("••••"):
                        _tg_cfg["bot_token"] = tok
                if "chat_ids" in req:
                    raw = req["chat_ids"]
                    if isinstance(raw, str):
                        raw = [
                            x.strip()
                            for x in raw.replace(";", ",").split(",")
                            if x.strip()
                        ]
                    ids: list = []
                    for x in raw if isinstance(raw, list) else []:
                        try:
                            ids.append(int(str(x).strip()))
                        except (TypeError, ValueError):
                            s = str(x).strip()
                            if s:
                                ids.append(s)
                    _tg_cfg["chat_ids"] = ids
                for k in (
                    "notify_events",
                    "notify_offline",
                    "notify_safety",
                    "notify_zone",
                    "commands_en",
                ):
                    if k in req:
                        _tg_cfg[k] = bool(req[k])
                if "notify_offline_streak" in req and req["notify_offline_streak"] is not None:
                    try:
                        _tg_cfg["notify_offline_streak"] = max(
                            1, min(30, int(req["notify_offline_streak"]))
                        )
                    except (TypeError, ValueError):
                        pass
                if "default_lang" in req and req["default_lang"] is not None:
                    dl = str(req["default_lang"]).lower()
                    _tg_cfg["default_lang"] = "en" if dl.startswith("en") else "ru"
                # remove one chat (allowlist + prefs); optional history wipe
                if "remove_chat_id" in req and req["remove_chat_id"] is not None:
                    rid = req["remove_chat_id"]
                    drop_hist = bool(req.get("remove_chat_history", False))
                    # apply inside lock later via helper — set flag
                    _tg_cfg["_pending_remove"] = (rid, drop_hist)
                # optional web merge of per-chat prefs
                if "chats" in req and isinstance(req["chats"], dict):
                    cur_chats = _tg_cfg.setdefault("chats", {})
                    if not isinstance(cur_chats, dict):
                        cur_chats = {}
                        _tg_cfg["chats"] = cur_chats
                    for ck, cv in req["chats"].items():
                        if not isinstance(cv, dict):
                            continue
                        key = str(ck)
                        base = dict(DEFAULT_CHAT_PREFS)
                        if isinstance(cur_chats.get(key), dict):
                            base.update(cur_chats[key])
                        if "lang" in cv:
                            base["lang"] = (
                                "en"
                                if str(cv["lang"]).lower().startswith("en")
                                else "ru"
                            )
                        for nk in (
                            "notify_events",
                            "notify_offline",
                            "notify_safety",
                            "notify_zone",
                            "commands_en",
                            "confirm_force_stop",
                        ):
                            if nk in cv:
                                base[nk] = bool(cv[nk])
                        # drop legacy key if present
                        base.pop("notify_policy", None)
                        cur_chats[key] = base
                # force getMe refresh on token change
                if "bot_token" in req:
                    with _tg_state_lock:
                        _tg_state["me"] = None
                pending_rm = _tg_cfg.pop("_pending_remove", None)
            if pending_rm:
                rid, drop_hist = pending_rm
                cfg_out = _tg_remove_chat(rid, history=drop_hist)
            else:
                _save_telegram_cfg()
                # rebuild chats map from chat_ids + saved prefs
                _load_telegram_cfg()
                cfg_out = get_telegram_cfg(redact=True)
            # optional immediate getMe
            me = None
            with _tg_cfg_lock:
                en = bool(_tg_cfg.get("enabled") and _tg_cfg.get("bot_token"))
            if en:
                try:
                    me = _tg_api("getMe", timeout=12).get("result")
                    with _tg_state_lock:
                        _tg_state["me"] = (me or {}).get("username") or (me or {}).get(
                            "id"
                        )
                        _tg_state["ok"] = True
                        _tg_state["last_error"] = None
                except Exception as e:
                    with _tg_state_lock:
                        _tg_state["ok"] = False
                        _tg_state["last_error"] = str(e)
            self._json_response(
                200,
                {
                    "ok": True,
                    "config": cfg_out if pending_rm else get_telegram_cfg(redact=True),
                    "me": me,
                    "removed_chat_id": (
                        str(pending_rm[0]) if pending_rm else None
                    ),
                },
            )
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_pool_config_get(self) -> None:
        with _pool_cfg_lock:
            cfg = dict(_pool_cfg)
        derived = pool_derived(cfg)
        self._json_response(200, {"ok": True, "config": cfg, "derived": derived})

    def _api_miner_error_log(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
        except ValueError:
            limit = 100
        try:
            rows = query_miner_error_log(limit=limit)
            self._json_response(
                200,
                {
                    "ok": True,
                    "count": len(rows),
                    "max": MINER_ERROR_LOG_MAX,
                    "errors": rows,
                },
            )
        except Exception as e:
            self._json_response(500, {"ok": False, "error": str(e), "errors": []})

    def _api_pool_config_post(self) -> None:
        try:
            req = self._read_json_body()
            with _pool_cfg_lock:
                for key in ("length_m", "width_m", "depth_m", "flow_m3h", "hex_delta_c"):
                    if key in req and req[key] is not None:
                        v = float(req[key])
                        if key == "flow_m3h":
                            v = max(0.0, min(500.0, v))
                        elif key == "hex_delta_c":
                            v = max(0.1, min(40.0, v))
                        else:
                            v = max(0.1, min(200.0, v))
                        _pool_cfg[key] = v
                if "shape" in req and req["shape"] is not None:
                    _pool_cfg["shape"] = str(req["shape"])
                if "comment" in req and req["comment"] is not None:
                    _pool_cfg["comment"] = str(req["comment"])
                if "water_sensor" in req and req["water_sensor"] is not None:
                    _pool_cfg["water_sensor"] = _normalize_pool_water_sensor(
                        req["water_sensor"]
                    )
                cfg = dict(_pool_cfg)
            _save_pool_cfg()
            derived = pool_derived(cfg)
            self._json_response(200, {"ok": True, "config": cfg, "derived": derived})
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_zone_config_get(self) -> None:
        self._json_response(200, {"ok": True, "config": get_zone_cfg()})

    def _api_zone_config_post(self) -> None:
        try:
            req = self._read_json_body()
            if not isinstance(req, dict):
                raise ValueError("expected JSON object")
            cfg = _coerce_zone_config_dict(req)
            with _zone_cfg_lock:
                _zone_cfg.clear()
                _zone_cfg.update(cfg)
            _save_zone_cfg()
            self._json_response(200, {"ok": True, "config": get_zone_cfg()})
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_zone_presets_get(self) -> None:
        self._json_response(200, list_zone_presets())

    def _api_pool_presets_get(self) -> None:
        self._json_response(200, list_pool_presets(include_secrets=True))

    def _api_pool_presets_post(self) -> None:
        """
        Pool presets:
          {action: list}
          {action: save, name, from_live?:true, pools?}
          {action: update, id, name?, pools?, from_live?}
          {action: delete, id}
          {action: apply, id}  — write to ASIC
          {action: get, id}
        """
        try:
            req = self._read_json_body() or {}
            if not isinstance(req, dict):
                raise ValueError("expected JSON object")
            action = str(req.get("action") or "list").strip().lower()
            if action in ("list", "ls"):
                self._json_response(200, list_pool_presets())
                return
            if action in ("get", "one"):
                p = get_pool_preset(str(req.get("id") or ""))
                if not p:
                    raise ValueError("preset not found")
                self._json_response(200, {"ok": True, "preset": p})
                return
            if action in ("save", "create", "add"):
                name = req.get("name")
                from_live = bool(req.get("from_live") or req.get("use_current"))
                pools = req.get("pools") if isinstance(req.get("pools"), list) else None
                out = save_pool_preset(
                    str(name or ""),
                    pools,
                    preset_id=None,
                    from_live=from_live or pools is None,
                )
                self._json_response(200, out)
                return
            if action in ("update", "edit", "rename"):
                pid = str(req.get("id") or "").strip()
                if not pid:
                    raise ValueError("id required")
                existing = get_pool_preset(pid)
                if not existing:
                    raise ValueError("preset not found")
                name = req.get("name")
                if name is None or str(name).strip() == "":
                    name = existing.get("name")
                from_live = bool(req.get("from_live") or req.get("use_current"))
                pools = req.get("pools") if isinstance(req.get("pools"), list) else None
                out = save_pool_preset(
                    str(name),
                    pools,
                    preset_id=pid,
                    from_live=from_live,
                )
                self._json_response(200, out)
                return
            if action in ("delete", "remove", "del"):
                out = delete_pool_preset(str(req.get("id") or ""))
                self._json_response(200, out)
                return
            if action in ("apply", "load", "select"):
                pw = req.get("password") or req.get("api_password")
                out = apply_pool_preset(
                    str(req.get("id") or ""),
                    password=str(pw) if pw is not None else None,
                )
                self._json_response(200, out)
                return
            raise ValueError(f"unknown action: {action}")
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_filtration_post(self) -> None:
        try:
            req = self._read_json_body() or {}
            cfg = apply_filtration_cfg(req if isinstance(req, dict) else {})
            self._json_response(200, {"ok": True, "config": cfg, **get_filtration_status()})
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_filtration_set(self) -> None:
        try:
            req = self._read_json_body() or {}
            if not isinstance(req, dict):
                req = {}
            if "on" in req:
                on = bool(req["on"])
            else:
                # path-based
                path = urlparse(self.path).path.rstrip("/")
                if path.endswith("/off"):
                    on = False
                elif path.endswith("/on"):
                    on = True
                else:
                    raise ValueError("need {on: true|false}")
            out = filtration_set(on, source=str(req.get("source") or "manual"), force=False)
            code = 200 if out.get("ok") else 400
            self._json_response(code, out)
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_filtration_test(self) -> None:
        out = filtration_test()
        self._json_response(200 if out.get("ok") else 400, out)

    def _api_zone_presets_post(self) -> None:
        """
        Zone map presets:
          {action: list}
          {action: save, name, config?}          — create (config defaults to current map)
          {action: update, id, name?, config?}   — rename and/or overwrite config
          {action: delete, id}
          {action: apply, id}                    — load preset into active map
          {action: get, id}                      — full preset with config
        """
        try:
            req = self._read_json_body() or {}
            if not isinstance(req, dict):
                raise ValueError("expected JSON object")
            action = str(req.get("action") or "list").strip().lower()
            if action in ("list", "ls"):
                self._json_response(200, list_zone_presets())
                return
            if action in ("get", "one"):
                p = get_zone_preset(str(req.get("id") or ""))
                if not p:
                    raise ValueError("preset not found")
                self._json_response(200, {"ok": True, "preset": p})
                return
            if action in ("save", "create", "add"):
                name = req.get("name")
                cfg = req.get("config") if isinstance(req.get("config"), dict) else None
                out = save_zone_preset(str(name or ""), cfg, preset_id=None)
                self._json_response(200, out)
                return
            if action in ("update", "edit", "rename"):
                pid = str(req.get("id") or "").strip()
                if not pid:
                    raise ValueError("id required")
                existing = get_zone_preset(pid)
                if not existing:
                    raise ValueError("preset not found")
                name = req.get("name")
                if name is None or str(name).strip() == "":
                    name = existing.get("name")
                cfg = (
                    req.get("config")
                    if isinstance(req.get("config"), dict)
                    else existing.get("config")
                )
                # if update_config_from_form: true → use current map
                if req.get("from_current") or req.get("use_current"):
                    cfg = get_zone_cfg()
                out = save_zone_preset(str(name), cfg, preset_id=pid)
                self._json_response(200, out)
                return
            if action in ("delete", "remove", "del"):
                out = delete_zone_preset(str(req.get("id") or ""))
                self._json_response(200, out)
                return
            if action in ("apply", "load", "select"):
                out = apply_zone_preset(str(req.get("id") or ""))
                self._json_response(200, out)
                return
            raise ValueError(f"unknown action: {action}")
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_miner_config_get(self) -> None:
        cfg = get_miner_settings()
        # never echo password in clear if you prefer — UI already has field; keep for form fill
        self._json_response(200, {"ok": True, "config": cfg})

    def _api_miner_config_post(self) -> None:
        try:
            req = self._read_json_body()
            host = req.get("miner_host")
            port = req.get("miner_port")
            password = req.get("api_password")
            poll = req.get("poll_interval_sec")
            dry = req.get("dry_run") if "dry_run" in req else None
            proj = req.get("project_name") if "project_name" in req else None
            if (
                host is None
                and port is None
                and password is None
                and poll is None
                and dry is None
                and proj is None
            ):
                raise ValueError("nothing to save")
            settings = apply_miner_settings(
                host=str(host) if host is not None else None,
                port=int(port) if port is not None and str(port) != "" else None,
                password=str(password) if password is not None else None,
                poll_interval_sec=int(poll) if poll is not None and str(poll) != "" else None,
                dry_run=bool(dry) if dry is not None else None,
                project_name=str(proj) if proj is not None else None,
                persist=True,
            )
            self._json_response(200, {"ok": True, "config": settings})
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_policy_post(self) -> None:
        """Update dry_run / enable / override server policy."""
        try:
            req = self._read_json_body()
            if "dry_run" in req:
                apply_miner_settings(dry_run=bool(req["dry_run"]), persist=True)
            if "enabled" in req:
                with _policy_lock:
                    _policy_ctrl["enabled"] = bool(req["enabled"])
            # Override 30m (or N min): pause zone auto, Safety still on
            if "override_clear" in req and req["override_clear"]:
                self._json_response(200, set_policy_override(clear=True))
                return
            if "override_min" in req and req["override_min"] is not None:
                self._json_response(
                    200, set_policy_override(minutes=float(req["override_min"]))
                )
                return
            if "override" in req:
                if req["override"] in (False, 0, "0", "false", "off"):
                    self._json_response(200, set_policy_override(clear=True))
                    return
                mins = 30.0
                if isinstance(req["override"], (int, float)) and float(req["override"]) > 0:
                    mins = float(req["override"])
                self._json_response(200, set_policy_override(minutes=mins))
                return
            # Force Stop (emergency sticky Suspend)
            if "force_stop" in req:
                self._json_response(
                    200, set_force_stop(bool(req["force_stop"]), apply_now=True)
                )
                return
            self._json_response(200, get_policy_status())
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_miner_test(self) -> None:
        """Probe TCP summary on given host/port (or current settings). Does not persist."""
        try:
            req = self._read_json_body() if self.headers.get("Content-Length") else {}
        except Exception:
            req = {}
        try:
            host = str(req.get("miner_host") or HOST_MINER).strip()
            port = int(req.get("miner_port") or PORT_MINER)
            if not host:
                raise ValueError("miner_host empty")
            payload = (json.dumps({"cmd": "summary"}, separators=(",", ":")) + "\n").encode()
            with socket.create_connection((host, port), timeout=4) as sock:
                sock.sendall(payload)
                raw = _recv_json(sock, timeout=4)
            msg = raw.get("Msg") if isinstance(raw, dict) else None
            mode = None
            power = None
            if isinstance(msg, dict):
                mode = msg.get("Power Mode")
                power = msg.get("Power")
            self._json_response(
                200,
                {
                    "ok": True,
                    "host": f"{host}:{port}",
                    "mode": mode,
                    "power": power,
                },
            )
        except Exception as e:
            self._json_response(
                502,
                {
                    "ok": False,
                    "error": str(e),
                    "host": f"{req.get('miner_host') or HOST_MINER}:{req.get('miner_port') or PORT_MINER}",
                },
            )


def main() -> None:
    init_db()
    # backfill error journal miner/component columns from current ASIC Info
    try:
        n = backfill_miner_error_log_identity()
        if n:
            print(f"error log backfill: {n} rows")
    except Exception as e:
        print(f"error log backfill: {e}")
    # warm Info cache (non-blocking-ish: may take a few seconds)
    def _warm_info() -> None:
        try:
            collect_system_info()
        except Exception as e:
            print(f"info cache warm: {e}")
        # rewrite config.json meta (version, router, paths) after identity warm
        try:
            m = refresh_config_meta(include_router=True)
            print(
                f"config meta: v{m.get('app_version')} · "
                f"{(m.get('router') or {}).get('model_code') or (m.get('router') or {}).get('model') or 'router?'} · "
                f"{(m.get('miner') or {}).get('host')}"
            )
        except Exception as e:
            print(f"config meta: {e}")

    # quick meta write first (no miner wait), then full refresh after info warm
    try:
        refresh_config_meta(include_router=True)
    except Exception as e:
        print(f"config meta (boot): {e}")
    threading.Thread(target=_warm_info, name="info-warm", daemon=True).start()
    t = threading.Thread(target=collector_loop, name="history-collector", daemon=True)
    t.start()
    tp = threading.Thread(target=policy_loop, name="policy-control", daemon=True)
    tp.start()
    tt = threading.Thread(target=telegram_loop, name="telegram-bot", daemon=True)
    tt.start()
    tc = threading.Thread(target=chipmap_loop, name="chipmap-poll", daemon=True)
    tc.start()

    # After OTA restart: tell the initiating TG chat that we're back online
    def _boot_update_notify() -> None:
        # wait for telegram_loop to load token / getMe
        for delay in (3.0, 8.0, 15.0):
            time.sleep(delay)
            try:
                if not UPDATE_NOTIFY_FILE.is_file():
                    return
                _flush_update_restart_notify()
                if not UPDATE_NOTIFY_FILE.is_file():
                    return
            except Exception as e:
                print(f"[update] boot notify: {e}")

    threading.Thread(
        target=_boot_update_notify, name="update-restart-notify", daemon=True
    ).start()

    server = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), Handler)
    print(f"poolheat UI:       http://{HTTP_BIND}:{HTTP_PORT}/")
    print(f"www:               {ROOT}")
    print(f"data:              {DATA}")
    print(f"live API:          GET  /api/live")
    print(f"set API:           POST /api/set")
    print(f"history:           GET  /api/history?hours=24")
    print(f"weather:           GET  /api/weather")
    print(f"pool:              GET  /api/pool/config")
    print(f"zone map:          GET/POST /api/zone/config")
    print(f"zone presets:      GET/POST /api/zone/presets")
    print(f"pool presets:      GET/POST /api/miner/pools/presets")
    print(f"filtration:        GET/POST /api/filtration · /set · /test")
    print(f"chipmap:           GET /api/chipmap · POST /api/chipmap/config · refresh")
    try:
        lp = get_luci_proxy_cfg()
        lpc = lp.get("config") or {}
        lps = lp.get("status") or {}
        print(
            f"luci proxy:        "
            f"{'ON' if lpc.get('enabled') else 'off'} · "
            f":{lpc.get('listen_port') or 8788} → "
            f"{(lps.get('target') or (HOST_MINER + '/'))} · "
            f"{'running' if lps.get('running') else 'stopped'}"
        )
    except Exception as e:
        print(f"luci proxy:        n/a ({e})")
    print(f"policy:            GET/POST /api/policy · server-side control")
    print(f"system info:       GET  /api/system/info")
    print(f"version/update:    GET  /api/version · /api/update/check · POST /api/update/apply")
    print(f"app version:       {get_app_version()} · repo {GITHUB_REPO}")
    print(f"miner:             {HOST_MINER}:{PORT_MINER}")
    print(f"miner config:      GET/POST /api/miner/config · {_miner_config_path()}")
    print(f"config backup:     GET  /api/config/backup · POST /api/config/restore")
    print(f"miner pools:       GET  /api/miner/pools")
    print(f"telegram:          GET/POST /api/telegram/config · getUpdates")
    print(f"telegram timing:   GET  /api/telegram/timing  (ring {_TG_TIMING_MAX})")
    with _tg_cfg_lock:
        tg_on = bool(_tg_cfg.get("enabled") and _tg_cfg.get("bot_token"))
    print(f"telegram bot:      {'enabled' if tg_on else 'disabled'}")
    print(f"dry_run:           {DRY_RUN}")
    print(f"policy poll:       every {POLL_INTERVAL_SEC}s")
    print(f"db:                {DB_FILE}")
    print(
        f"collector:         every {_hist_cfg['sample_interval_sec']}s · "
        f"keep {_hist_cfg['retention_days']}d"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _collector_stop.set()
        _policy_stop.set()
        _tg_stop.set()
        print("\nstop")


if __name__ == "__main__":
    main()
