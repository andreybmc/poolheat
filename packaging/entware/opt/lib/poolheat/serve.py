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
import subprocess
import threading
import time
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
STATE_FILE = DATA / "last_commands.json"
DB_FILE = DATA / "history.db"
CONFIG_FILE = DATA / "history_config.json"
WEATHER_CFG_FILE = DATA / "weather_config.json"
POOL_CFG_FILE = DATA / "pool_config.json"
ZONE_CFG_FILE = DATA / "zone_map_config.json"
DEFAULT_API_PASSWORD = _APP["api_password"]
# Live / control poll of miner API (UI + future policy loop). Not history sample interval.
POLL_INTERVAL_SEC = int(
    os.environ.get("POOLHEAT_POLL_INTERVAL")
    or _APP.get("file_cfg", {}).get("poll_interval_sec")
    or 5
)
POLL_INTERVAL_SEC = max(2, min(300, POLL_INTERVAL_SEC))
# Dry Run: block Z0–Z2 auto writes; Safety Critical still applies
_fc0 = _APP.get("file_cfg") or {}
DRY_RUN = bool(_fc0["dry_run"]) if "dry_run" in _fc0 else True

# Software version + GitHub updates
_DEFAULT_APP_VERSION = "0.2.0"
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
    "heat_zone": None,  # z0|z1|z2 sticky
    "safety_sticky": False,
    "last_key": None,
    "streak_key": None,
    "streak_count": 0,
    "last_apply_ts": 0.0,
    "last_event": None,
    "events": [],  # last ~40 events for UI
    "enabled": True,
}

DEFAULT_ZONE_CFG: dict = {
    "t0": 26.0,
    "t1": 28.0,
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
    "zones": {
        "z0": {
            "mode_en": True,
            "mode": "normal",
            "work_en": True,
            "work": "resume",
            "lim_en": False,
            "lim": 6000,
            "pct_en": False,
            "pct": 100,
        },
        "z1": {
            "mode_en": True,
            "mode": "low",
            "work_en": True,
            "work": "resume",
            "lim_en": True,
            "lim": 4000,
            "pct_en": False,
            "pct": 70,
        },
        "z2": {
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
                "lim": 4000,
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
            "h",
            "t_crit",
            "t_crit_clear",
            "dwell_sec",
            "settle_sec",
            "streak",
            "min_write_interval_sec",
            "limit_tol_w",
        ):
            if key in raw and raw[key] is not None:
                try:
                    if key in (
                        "dwell_sec",
                        "settle_sec",
                        "streak",
                        "min_write_interval_sec",
                        "limit_tol_w",
                    ):
                        cfg[key] = int(float(raw[key]))
                    else:
                        cfg[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
        cfg["min_write_interval_sec"] = max(
            10, min(3600, int(cfg.get("min_write_interval_sec", 60) or 60))
        )
        cfg["limit_tol_w"] = max(10, min(2000, int(cfg.get("limit_tol_w", 100) or 100)))
        # clamps
        cfg["t0"] = float(cfg["t0"])
        cfg["t1"] = float(cfg["t1"])
        cfg["h"] = max(0.2, min(5.0, float(cfg.get("h", 0.5))))
        cfg["t_crit"] = float(cfg["t_crit"])
        cfg["t_crit_clear"] = float(cfg["t_crit_clear"])
        if cfg["t0"] >= cfg["t1"]:
            cfg["t1"] = cfg["t0"] + max(cfg["h"], 0.5)
        zones_in = raw.get("zones") if isinstance(raw.get("zones"), dict) else {}
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


def _save_zone_cfg() -> None:
    with _zone_cfg_lock:
        _save_json(ZONE_CFG_FILE, _zone_cfg)


def get_zone_cfg() -> dict:
    with _zone_cfg_lock:
        return json.loads(json.dumps(_zone_cfg))  # deep copy


def _miner_config_path() -> Path:
    """Where to persist miner host/port/password."""
    cfg = _APP.get("cfg_path") or ""
    if cfg:
        return Path(cfg)
    # Entware default
    if Path("/opt/etc/poolheat").is_dir():
        return Path("/opt/etc/poolheat/config.json")
    return DATA / "config.json"


def get_miner_settings() -> dict:
    with _miner_cfg_lock:
        return {
            "miner_host": HOST_MINER,
            "miner_port": int(PORT_MINER),
            "api_password": DEFAULT_API_PASSWORD,
            "poll_interval_sec": int(POLL_INTERVAL_SEC),
            "dry_run": bool(DRY_RUN),
            "host": f"{HOST_MINER}:{PORT_MINER}",
            "config_path": str(_miner_config_path()),
        }


def apply_miner_settings(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    poll_interval_sec: int | None = None,
    dry_run: bool | None = None,
    *,
    persist: bool = True,
) -> dict:
    """Update runtime miner target; optionally write config.json."""
    global HOST_MINER, PORT_MINER, DEFAULT_API_PASSWORD, POLL_INTERVAL_SEC, DRY_RUN, _cache, _cache_ts

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

        settings = {
            "miner_host": HOST_MINER,
            "miner_port": int(PORT_MINER),
            "api_password": DEFAULT_API_PASSWORD,
            "poll_interval_sec": int(POLL_INTERVAL_SEC),
            "dry_run": bool(DRY_RUN),
            "host": f"{HOST_MINER}:{PORT_MINER}",
        }

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
        else:
            settings["saved"] = False

    # drop live cache so next poll hits new host
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0
    return settings

# Rectangular pool geometry + circulation through heat exchanger
DEFAULT_POOL_CFG = {
    "length_m": 8.0,
    "width_m": 4.0,
    "depth_m": 1.5,
    # circulation flow through heat exchanger, m³/h
    "flow_m3h": 12.0,
    # ΔT heat exchanger (water in − water out), °C
    "hex_delta_c": 5.0,
    # optional notes
    "shape": "rect",
    "comment": "",
}

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
WEATHER_CACHE_TTL = 600.0  # 10 min

_pool_cfg_lock = threading.Lock()
_pool_cfg: dict = dict(DEFAULT_POOL_CFG)

_cache: dict | None = None
_cache_ts = 0.0
_cache_lock = threading.Lock()
CACHE_TTL = 2.0

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
    with _weather_cache_lock:
        if (
            not force
            and _weather_cache is not None
            and (now - _weather_cache_ts) < WEATHER_CACHE_TTL
            and _weather_cache.get("ok")
        ):
            return dict(_weather_cache)

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
                    UNIQUE(code, miner_ts)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_miner_err_seen ON miner_error_log(seen_ts DESC)"
            )
            conn.commit()
        finally:
            conn.close()


MINER_ERROR_LOG_MAX = 100


def log_miner_errors(entries: list[dict]) -> int:
    """
    Append new miner errors to journal (dedupe by code+miner_ts).
    Keep only last MINER_ERROR_LOG_MAX rows. Returns number inserted.
    """
    if not entries:
        return 0
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
                try:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO miner_error_log
                            (seen_ts, seen_iso, code, cause, miner_ts)
                        VALUES (?,?,?,?,?)
                        """,
                        (now, iso, code, str(cause), miner_ts_s),
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
                SELECT id, seen_ts, seen_iso, code, cause, miner_ts
                FROM miner_error_log
                ORDER BY seen_ts DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = []
            for r in cur.fetchall():
                code = r["code"]
                rows.append(
                    {
                        "id": r["id"],
                        "seen_ts": r["seen_ts"],
                        "seen_iso": r["seen_iso"],
                        "code": code,
                        "cause": _display_cause(code, r["cause"]),
                        "miner_ts": r["miner_ts"] or None,
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
                    upfreq_ok
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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


def _recv_json(sock: socket.socket, timeout: float = 5.0) -> dict:
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    while True:
        try:
            chunk = sock.recv(16384)
            if not chunk:
                break
            chunks.append(chunk)
            text = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
            try:
                if text:
                    return json.loads(text)
            except json.JSONDecodeError:
                pass
        except socket.timeout:
            break
    raw = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise TimeoutError("empty response from miner")
    return json.loads(raw)


def miner_cmd(cmd: dict, timeout: float = 5.0) -> dict:
    payload = (json.dumps(cmd, separators=(",", ":")) + "\n").encode()
    with socket.create_connection((HOST_MINER, PORT_MINER), timeout=timeout) as sock:
        sock.sendall(payload)
        return _recv_json(sock, timeout=timeout)


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


def _collect_miner_identity() -> dict:
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
        try:
            devs = miner_cmd({"cmd": "devs"}, timeout=3).get("DEVS") or []
            boards = []
            for d in devs if isinstance(devs, list) else []:
                if not isinstance(d, dict):
                    continue
                boards.append(
                    {
                        "slot": d.get("Slot"),
                        "pcb_sn": d.get("PCB SN") or d.get("pcb_sn"),
                        "chip_data": d.get("Chip Data") or d.get("chip_data"),
                        "temp_c": _f(d.get("Temperature")),
                        "effective_chips": d.get("Effective Chips"),
                    }
                )
            out["boards"] = boards
        except Exception:
            pass
        out["ok"] = bool(out.get("miner_type") or out.get("mac") or out.get("boards"))
    except Exception as e:
        out["error"] = str(e)
    return out


def collect_system_info() -> dict:
    """Router + miner identity + host resources for Info tab."""
    disks: list[dict] = []
    seen: set[str] = set()
    for path, label in (
        (str(DATA), "poolheat data"),
        ("/opt", "/opt (Entware)"),
        ("/storage", "/storage"),
        ("/tmp", "/tmp"),
        ("/", "/ (rootfs)"),
    ):
        ent = _disk_usage_entry(path, label)
        if not ent:
            continue
        # de-dupe by total+free fingerprint (bind-mounts)
        key = f"{ent['total_b']}:{ent['free_b']}:{ent.get('used_b')}"
        if key in seen and path not in (str(DATA), "/opt"):
            continue
        seen.add(key)
        disks.append(ent)

    db_size = None
    try:
        if DB_FILE.is_file():
            db_size = DB_FILE.stat().st_size
    except Exception:
        pass

    return {
        "ok": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "router": _collect_router_info(),
        "miner": _collect_miner_identity(),
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
                out["notes"] = (rel.get("body") or "")[:800] or None
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
    def _run() -> None:
        time.sleep(1.5)
        candidates = [
            ["/opt/etc/init.d/S99poolheat", "restart"],
            ["/opt/etc/init.d/S99poolheat", "stop"],
            ["/opt/etc/init.d/S99poolheat-standalone", "restart"],
        ]
        # try restart first
        for cmd in candidates:
            try:
                if not Path(cmd[0]).exists() and not Path(cmd[0]).is_file():
                    # still try — may be on PATH scripts
                    pass
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if r.returncode == 0:
                    # if we only stopped, start again
                    if cmd[-1] == "stop":
                        subprocess.run(
                            [cmd[0], "start"],
                            capture_output=True,
                            text=True,
                            timeout=20,
                            check=False,
                        )
                    return
            except Exception:
                continue
        # last resort: kill and re-exec (best-effort)
        try:
            subprocess.run(
                ["sh", "-c", "kill $(ps | grep '[s]erve.py' | awk '{print $1}') 2>/dev/null; "
                 "sleep 1; /opt/bin/poolheatd start 2>/dev/null || "
                 "nohup /opt/bin/python3 /opt/lib/poolheat/serve.py "
                 ">>/opt/var/poolheat/poolheat.log 2>&1 &"],
                timeout=25,
                check=False,
            )
        except Exception:
            pass

    threading.Thread(target=_run, name="poolheat-restart", daemon=True).start()


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


def get_token_data(password: str) -> dict:
    data = miner_cmd({"cmd": "get_token"})
    msg = data["Msg"]
    if msg == "over max connect":
        raise RuntimeError("over max connect")
    pwd_hash = md5_crypt.hash(password, salt=msg["salt"])
    host_passwd_md5 = pwd_hash.split("$")[3]
    tmp = md5_crypt.hash(host_passwd_md5 + msg["time"], salt=msg["newsalt"])
    host_sign = tmp.split("$")[3]
    return {"host_sign": host_sign, "host_passwd_md5": host_passwd_md5}


def privileged_cmd(cmd: dict, password: str) -> dict:
    token = get_token_data(password)
    cmd = dict(cmd)
    cmd["token"] = token["host_sign"]
    aeskey = binascii.unhexlify(
        hashlib.sha256(token["host_passwd_md5"].encode()).hexdigest()
    )
    cipher = AES.new(aeskey, AES.MODE_ECB)
    api_str = json.dumps(cmd, separators=(",", ":"))
    enc = base64.b64encode(cipher.encrypt(_add_to_16(api_str))).decode()
    payload = (json.dumps({"enc": 1, "data": enc}, separators=(",", ":")) + "\n").encode()
    with socket.create_connection((HOST_MINER, PORT_MINER), timeout=8) as sock:
        sock.sendall(payload)
        raw = _recv_json(sock, timeout=8)
    if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], str):
        try:
            pt = cipher.decrypt(base64.b64decode(raw["data"])).split(b"\x00")[0].decode()
            return json.loads(pt)
        except Exception:
            return raw
    return raw


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
    "710": "Control board rebooted as exception",
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
        log_miner_errors(active)
    except Exception as e:
        print(f"[errors] log failed: {e}")

    # drop cleared errors from cache
    cache = {k: v for k, v in cache.items() if k in active_keys}

    with _state_lock:
        _state["miner_error_cache"] = cache
        _save_state()

    return active


def fetch_live() -> dict:
    summary = miner_cmd({"cmd": "summary"})["Msg"]
    status = miner_cmd({"cmd": "status"})["Msg"]
    devs = miner_cmd({"cmd": "devs"}).get("DEVS", [])
    raw_errors = _fetch_miner_errors_raw()

    # PSU: temp0 (°C), fan_speed (rpm), optional electricals — never fail whole live poll
    psu_temp = None
    psu_fan = None
    psu_pin = None
    psu_model = None
    try:
        psu = miner_cmd({"cmd": "get_psu"}, timeout=3).get("Msg") or {}
        if isinstance(psu, dict):
            psu_temp = _f(psu.get("temp0"))
            psu_fan = _f(psu.get("fan_speed"))
            psu_pin = _f(psu.get("pin"))  # often watts as string
            psu_model = psu.get("model") or psu.get("name")
    except Exception as e:
        print(f"[psu] get_psu failed: {e}")

    boards: list[float] = []
    upfreq: list[int] = []
    factory_parts: list[float] = []
    for i in range(4):
        if i < len(devs):
            boards.append(float(devs[i].get("Temperature", 0)))
            upfreq.append(int(devs[i].get("Upfreq Complete", 0)))
            try:
                fg = float(devs[i].get("Factory GHS") or 0)
                if fg > 0:
                    factory_parts.append(fg)
            except (TypeError, ValueError):
                pass
        else:
            boards.append(0.0)
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

    return {
        "ok": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "host": f"{HOST_MINER}:{PORT_MINER}",
        "liquid": status.get("liquid_temp"),
        "env": summary.get("Env Temp"),
        "chip_min": summary.get("Chip Temp Min"),
        "chip_avg": summary.get("Chip Temp Avg"),
        "chip_max": summary.get("Chip Temp Max"),
        "boards": boards,
        "upfreq": upfreq,
        "power": summary.get("Power"),
        "mode": mode,
        "mode_norm": mode_norm,
        "power_limit": summary.get("Power Limit"),
        "power_limit_set": status.get("power_limit_set"),
        "power_pct_reported": hash_pct_num,
        "power_pct_cmd": pct_cmd,
        "power_limit_cmd": lim_cmd,
        "mode_cmd": mode_cmd,
        "work_cmd": work_cmd,
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
        "psu_model": psu_model,
        "miner_errors": miner_errors,
        "dry_run": bool(DRY_RUN),
        "policy": get_policy_status(),
    }


def _infer_work_state(live: dict) -> str:
    """resume | sleep — Mining Control for history bands."""
    wc = live.get("work_cmd")
    if wc in ("sleep", "suspend"):
        return "sleep"
    if wc == "resume":
        return "resume"
    mode = str(live.get("mode_norm") or live.get("mode") or "").lower()
    if "sleep" in mode or mode in ("off", "power_off"):
        return "sleep"
    p = _f(live.get("power"))
    h = _f(live.get("hashrate_th"))
    if p is not None and p < 50 and (h is None or h < 1):
        return "sleep"
    return "resume"


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


def live_to_sample(live: dict) -> dict:
    boards = live.get("boards") or [None, None, None, None]
    lim_set = live.get("power_limit_set")
    try:
        lim_set_f = float(lim_set) if lim_set not in (None, "") else None
    except (TypeError, ValueError):
        lim_set_f = None
    now = time.time()
    online = 1 if live.get("ok") is not False else 0
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
        "power": _f(live.get("power")),
        "power_limit": _f(live.get("power_limit")),
        "power_limit_set": lim_set_f,
        "power_pct_cmd": _f(live.get("power_pct_cmd")),
        "freq": _f(live.get("freq_avg")),
        "hashrate_th": _f(live.get("hashrate_th")),
        "mode": live.get("mode"),
        "hash_stable": live.get("hash_stable_i", 0),
        "online": online,
        "work_state": _infer_work_state(live) if online else None,
        "upfreq_ok": _infer_upfreq_ok(live) if online else None,
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
    _invalidate_cache()
    if not ok:
        raise RuntimeError(entry["error"])
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


def apply_set(action: str, value, password: str) -> dict:
    action = (action or "").strip().lower()
    password = password or DEFAULT_API_PASSWORD

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
        resp = privileged_cmd({"cmd": cmd_map[v]}, password)
        out = _record_write("mode", v, resp)
        out["cmd"] = cmd_map[v]
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
        resp = privileged_cmd({"cmd": miner_cmd_name}, password)
        out = _record_write("working", stored, resp)
        out["cmd"] = miner_cmd_name
        return out

    if action == "power_pct":
        pct = int(value)
        if not 0 <= pct <= 100:
            raise ValueError("power_pct must be 0..100")
        resp = privileged_cmd({"cmd": "set_power_pct", "percent": str(pct)}, password)
        return _record_write("power_pct", pct, resp)

    if action in ("power_limit", "set_power_limit", "adjust_power_limit"):
        watts = int(value)
        if watts < 0 or watts > 20000:
            raise ValueError("power_limit out of range")
        resp = privileged_cmd(
            {"cmd": "adjust_power_limit", "power_limit": str(watts)}, password
        )
        return _record_write(
            "power_limit",
            watts,
            resp,
            warning="adjust_power_limit may reboot / restart mining",
        )

    raise ValueError(f"unknown action: {action}")


def _invalidate_cache() -> None:
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


# ─── server-side policy control ───────────────────────────────────────────────


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
        _policy_ctrl["events"] = events[:40]
    print(f"[policy] {ev['ts']} {kind}: {msg}")


def _evaluate_heat_zone(liquid: float | None, sticky: str | None, t0: float, t1: float, h: float) -> str:
    """Z0 Normal · Z1 Reduced · Z2 No heat with hysteresis (mirrors UI)."""
    if liquid is None or not isinstance(liquid, (int, float)):
        return sticky if sticky in ("z0", "z1", "z2") else "z1"
    liq = float(liquid)
    h = max(0.2, float(h))
    if sticky not in ("z0", "z1", "z2"):
        if liq <= t0:
            return "z0"
        if liq >= t1:
            return "z2"
        return "z1"
    if sticky == "z0":
        if liq >= t0 + h:
            return "z2" if liq >= t1 else "z1"
        return "z0"
    if sticky == "z2":
        if liq <= t1 - h:
            return "z0" if liq <= t0 else "z1"
        return "z2"
    # z1
    if liq <= t0:
        return "z0"
    if liq >= t1:
        return "z2"
    return "z1"


def _upfreq_block(live: dict) -> bool:
    up = live.get("upfreq") or []
    try:
        return any(int(u) != 1 for u in up)
    except (TypeError, ValueError):
        return False


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
    """resume | suspend from measured miner state."""
    return "suspend" if _infer_work_state(live) == "sleep" else "resume"


def _live_mode(live: dict) -> str | None:
    m = str(live.get("mode_norm") or live.get("mode") or "").strip().lower()
    for k in ("low", "normal", "high"):
        if k in m:
            return k
    return None


def _live_limit_w(live: dict) -> float | None:
    return _f(live.get("power_limit")) or _f(live.get("power_limit_set"))


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
        try:
            apply_set(action, value, DEFAULT_API_PASSWORD)
            _policy_log("ok", f"APPLY {action}={value}", source=source)
        except Exception as e:
            ok_all = False
            _policy_log("err", f"FAIL {action}={value}: {e}", source=source)
            break
    _invalidate_cache()
    return ok_all


def get_policy_status() -> dict:
    with _policy_lock:
        ctrl = dict(_policy_ctrl)
        events = list(ctrl.get("events") or [])
    with _miner_cfg_lock:
        dry = bool(DRY_RUN)
        poll = int(POLL_INTERVAL_SEC)
    zc = get_zone_cfg()
    return {
        "ok": True,
        "server_side": True,
        "enabled": bool(ctrl.get("enabled", True)),
        "dry_run": dry,
        "poll_interval_sec": poll,
        "heat_zone": ctrl.get("heat_zone"),
        "safety_sticky": bool(ctrl.get("safety_sticky")),
        "last_key": ctrl.get("last_key"),
        "streak_key": ctrl.get("streak_key"),
        "streak_count": int(ctrl.get("streak_count") or 0),
        "last_apply_ts": ctrl.get("last_apply_ts"),
        "last_event": ctrl.get("last_event"),
        "events": events[:20],
        "thresholds": {
            "t0": zc.get("t0"),
            "t1": zc.get("t1"),
            "h": zc.get("h"),
            "t_crit": zc.get("t_crit"),
            "t_crit_clear": zc.get("t_crit_clear"),
            "dwell_sec": zc.get("dwell_sec"),
            "settle_sec": zc.get("settle_sec"),
            "streak": zc.get("streak"),
            "min_write_interval_sec": zc.get("min_write_interval_sec"),
            "limit_tol_w": zc.get("limit_tol_w"),
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
    except Exception as e:
        _policy_log("warn", f"live poll fail: {e}")
        return

    liquid = _f(live.get("liquid"))
    chip_max = _f(live.get("chip_max"))
    upfreq_block = _upfreq_block(live)

    zc = get_zone_cfg()
    t0 = float(zc.get("t0", 26))
    t1 = float(zc.get("t1", 28))
    h = float(zc.get("h", 0.5))
    t_crit = float(zc.get("t_crit", 70))
    t_crit_clear = float(zc.get("t_crit_clear", 65))
    streak_need = max(1, int(zc.get("streak", 3) or 3))
    dwell_sec = max(0, int(zc.get("dwell_sec", 600) or 0))
    settle_sec = max(0, int(zc.get("settle_sec", 300) or 0))
    min_write = max(10, int(zc.get("min_write_interval_sec", 60) or 60))
    limit_tol = float(zc.get("limit_tol_w", 100) or 100)
    zones = zc.get("zones") or {}

    # --- safety sticky ---
    was_safety = safety_sticky
    if chip_max is not None:
        if chip_max >= t_crit:
            safety_sticky = True
        elif chip_max <= t_crit_clear:
            safety_sticky = False

    heat_zone = _evaluate_heat_zone(liquid, heat_sticky, t0, t1, h)

    with _miner_cfg_lock:
        dry = bool(DRY_RUN)

    desired: str | None = None
    profile: dict | None = None
    is_safety = False

    if safety_sticky:
        desired = "safety:on_crit"
        crit = zones.get("critical") or {}
        profile = crit.get("on_crit") if isinstance(crit, dict) else None
        is_safety = True
    elif was_safety and not safety_sticky:
        desired = "safety:on_clear"
        crit = zones.get("critical") or {}
        profile = crit.get("on_clear") if isinstance(crit, dict) else None
        is_safety = True
    elif not dry:
        if heat_zone == "z0" and upfreq_block:
            desired = None
            # log once
            if last_key != "block:upfreq":
                _policy_log("info", "Z0 needed but upfreq blocks upward")
                with _policy_lock:
                    _policy_ctrl["last_key"] = "block:upfreq"
        else:
            desired = heat_zone
            profile = zones.get(heat_zone)
    else:
        # dry run: zone would — only if desired cmds would differ from live
        dry_tag = "dry:" + str(heat_zone)
        if last_key != dry_tag:
            would = _diff_commands_vs_live(
                _zone_entry_commands(zones.get(heat_zone)),
                live,
                limit_tol_w=limit_tol,
            )
            if would:
                _policy_log(
                    "info",
                    f"DRY_RUN would {heat_zone}: "
                    + ", ".join(f"{a}={v}" for a, v in would),
                    heat_zone=heat_zone,
                )
            # else silent — zone OK and miner already matches
            with _policy_lock:
                _policy_ctrl["last_key"] = dry_tag
            last_key = dry_tag

    # persist sticky state
    with _policy_lock:
        _policy_ctrl["heat_zone"] = heat_zone
        _policy_ctrl["safety_sticky"] = safety_sticky

    if not desired:
        return

    # Desired profile for this tick
    desired_cmds = _zone_entry_commands(profile)
    # Diff vs measured — heart of "don't spam if already correct"
    need_cmds = _diff_commands_vs_live(desired_cmds, live, limit_tol_w=limit_tol)

    if not need_cmds:
        # Already matches miner — no write, no notification
        with _policy_lock:
            _policy_ctrl["last_key"] = desired
            _policy_ctrl["streak_key"] = desired
            _policy_ctrl["streak_count"] = 0
        return

    # streak only for pending mismatches
    mismatch_sig = desired + "|" + ",".join(f"{a}={v}" for a, v in need_cmds)
    if streak_key == mismatch_sig:
        streak_count += 1
    else:
        streak_key = mismatch_sig
        streak_count = 1
    need = min(2, streak_need) if is_safety else streak_need
    with _policy_lock:
        _policy_ctrl["streak_key"] = streak_key
        _policy_ctrl["streak_count"] = streak_count

    if streak_count < need:
        return

    now = time.time()
    # After write: wait settle (miner ramping) before next reconcile write
    if last_apply_ts and (now - last_apply_ts) < settle_sec:
        return
    # Absolute min gap between any auto writes
    if last_apply_ts and (now - last_apply_ts) < min_write:
        return
    # Dwell only when *changing* zone profile (z1→z2), not when re-fixing same zone
    same_profile = last_key == desired
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
        + f" (have mode={_live_mode(live)} work={_live_work(live)} lim={_live_limit_w(live)})",
        dry_run=dry,
        liquid=liquid,
        chip_max=chip_max,
    )
    ok = _policy_apply_commands(need_cmds, source=desired)
    with _policy_lock:
        _policy_ctrl["last_key"] = None if desired == "safety:on_clear" else desired
        if ok:
            _policy_ctrl["last_apply_ts"] = time.time()
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
        if path == "/api/pool/config":
            self._api_pool_config_get()
            return
        if path == "/api/zone/config":
            self._api_zone_config_get()
            return
        if path in ("/api/miner/errors", "/api/errors/log"):
            self._api_miner_error_log()
            return
        if path == "/api/miner/config":
            self._api_miner_config_get()
            return
        if path in ("/api/policy", "/api/policy/status"):
            self._json_response(200, get_policy_status())
            return
        if path in ("/api/system/info", "/api/info"):
            try:
                self._json_response(200, collect_system_info())
            except Exception as e:
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
        if path == "/":
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
        if path == "/api/pool/config":
            self._api_pool_config_post()
            return
        if path == "/api/zone/config":
            self._api_zone_config_post()
            return
        if path == "/api/miner/config":
            self._api_miner_config_post()
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
            with _zone_cfg_lock:
                cfg = dict(_zone_cfg)
                for key in (
                    "t0",
                    "t1",
                    "h",
                    "t_crit",
                    "t_crit_clear",
                    "dwell_sec",
                    "settle_sec",
                    "streak",
                    "min_write_interval_sec",
                    "limit_tol_w",
                ):
                    if key in req and req[key] is not None:
                        try:
                            if key in (
                                "dwell_sec",
                                "settle_sec",
                                "streak",
                                "min_write_interval_sec",
                                "limit_tol_w",
                            ):
                                cfg[key] = int(float(req[key]))
                            else:
                                cfg[key] = float(req[key])
                        except (TypeError, ValueError):
                            pass
                cfg["min_write_interval_sec"] = max(
                    10, min(3600, int(cfg.get("min_write_interval_sec", 60) or 60))
                )
                cfg["limit_tol_w"] = max(
                    10, min(2000, int(cfg.get("limit_tol_w", 100) or 100))
                )
                cfg["h"] = max(0.2, min(5.0, float(cfg.get("h", 0.5))))
                if float(cfg["t0"]) >= float(cfg["t1"]):
                    raise ValueError("need t0 < t1")
                if float(cfg["t_crit_clear"]) >= float(cfg["t_crit"]):
                    raise ValueError("need t_crit_clear < t_crit")
                zones_in = req.get("zones") if isinstance(req.get("zones"), dict) else {}
                zones_out = dict(cfg.get("zones") or {})
                for name, default in DEFAULT_ZONE_CFG["zones"].items():
                    base = zones_out.get(name) or default
                    zin = zones_in.get(name, base)
                    if name == "critical":
                        if not isinstance(base, dict):
                            base = default
                        if isinstance(zin, dict) and (
                            "on_crit" in zin or "on_clear" in zin
                        ):
                            zones_out[name] = {
                                "on_crit": _normalize_zone_entry(
                                    zin.get("on_crit", base.get("on_crit")),
                                    default["on_crit"],
                                ),
                                "on_clear": _normalize_zone_entry(
                                    zin.get("on_clear", base.get("on_clear")),
                                    default["on_clear"],
                                ),
                            }
                        else:
                            zones_out[name] = {
                                "on_crit": _normalize_zone_entry(
                                    zin if isinstance(zin, dict) else base.get("on_crit"),
                                    default["on_crit"],
                                ),
                                "on_clear": _normalize_zone_entry(
                                    base.get("on_clear")
                                    if isinstance(base, dict)
                                    else None,
                                    default["on_clear"],
                                ),
                            }
                    else:
                        zones_out[name] = _normalize_zone_entry(zin, default)
                cfg["zones"] = zones_out
                _zone_cfg.clear()
                _zone_cfg.update(cfg)
            _save_zone_cfg()
            self._json_response(200, {"ok": True, "config": get_zone_cfg()})
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
            if (
                host is None
                and port is None
                and password is None
                and poll is None
                and dry is None
            ):
                raise ValueError("nothing to save")
            settings = apply_miner_settings(
                host=str(host) if host is not None else None,
                port=int(port) if port is not None and str(port) != "" else None,
                password=str(password) if password is not None else None,
                poll_interval_sec=int(poll) if poll is not None and str(poll) != "" else None,
                dry_run=bool(dry) if dry is not None else None,
                persist=True,
            )
            self._json_response(200, {"ok": True, "config": settings})
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})

    def _api_policy_post(self) -> None:
        """Update dry_run / enable server policy."""
        try:
            req = self._read_json_body()
            if "dry_run" in req:
                apply_miner_settings(dry_run=bool(req["dry_run"]), persist=True)
            if "enabled" in req:
                with _policy_lock:
                    _policy_ctrl["enabled"] = bool(req["enabled"])
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
    t = threading.Thread(target=collector_loop, name="history-collector", daemon=True)
    t.start()
    tp = threading.Thread(target=policy_loop, name="policy-control", daemon=True)
    tp.start()

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
    print(f"policy:            GET/POST /api/policy · server-side control")
    print(f"system info:       GET  /api/system/info")
    print(f"version/update:    GET  /api/version · /api/update/check · POST /api/update/apply")
    print(f"app version:       {get_app_version()} · repo {GITHUB_REPO}")
    print(f"miner:             {HOST_MINER}:{PORT_MINER}")
    print(f"miner config:      GET/POST /api/miner/config · {_miner_config_path()}")
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
        print("\nstop")


if __name__ == "__main__":
    main()
