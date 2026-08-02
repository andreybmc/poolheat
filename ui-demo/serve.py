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
import os
import sqlite3
import socket
import threading
import time
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
DEFAULT_API_PASSWORD = _APP["api_password"]

DEFAULT_HISTORY_CFG = {
    "enabled": True,
    "retention_days": 7,
    "sample_interval_sec": 30,
    "prune_every_samples": 20,
}

_cache: dict | None = None
_cache_ts = 0.0
_cache_lock = threading.Lock()
CACHE_TTL = 2.0

_state_lock = threading.Lock()
_state: dict = {
    "power_pct_cmd": None,
    "power_limit_cmd": None,
    "mode_cmd": None,
    "last_write": None,
    "last_write_result": None,
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


_load_state()
_load_hist_cfg()


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
                    hash_stable INTEGER
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
            # migrate older DBs
            cols = {r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
            if "hashrate_th" not in cols:
                conn.execute("ALTER TABLE samples ADD COLUMN hashrate_th REAL")
            conn.commit()
        finally:
            conn.close()


def insert_sample(row: dict) -> None:
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO samples (
                    ts, ts_iso, liquid, env, chip_min, chip_avg, chip_max,
                    board0, board1, board2, board3,
                    power, power_limit, power_limit_set, power_pct_cmd,
                    freq, hashrate_th, mode, hash_stable
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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


def fetch_live() -> dict:
    summary = miner_cmd({"cmd": "summary"})["Msg"]
    status = miner_cmd({"cmd": "status"})["Msg"]
    devs = miner_cmd({"cmd": "devs"}).get("DEVS", [])

    boards: list[float] = []
    upfreq: list[int] = []
    for i in range(4):
        if i < len(devs):
            boards.append(float(devs[i].get("Temperature", 0)))
            upfreq.append(int(devs[i].get("Upfreq Complete", 0)))
        else:
            boards.append(0.0)
            upfreq.append(0)

    mode = summary.get("Power Mode") or status.get("power_mode")
    mode_norm = mode.strip().lower() if isinstance(mode, str) else str(mode)

    with _state_lock:
        pct_cmd = _state.get("power_pct_cmd")
        lim_cmd = _state.get("power_limit_cmd")
        mode_cmd = _state.get("mode_cmd")
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
    }


def live_to_sample(live: dict) -> dict:
    boards = live.get("boards") or [None, None, None, None]
    lim_set = live.get("power_limit_set")
    try:
        lim_set_f = float(lim_set) if lim_set not in (None, "") else None
    except (TypeError, ValueError):
        lim_set_f = None
    now = time.time()
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
    }


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def apply_set(action: str, value, password: str) -> dict:
    action = (action or "").strip().lower()
    password = password or DEFAULT_API_PASSWORD

    if action == "mode":
        v = str(value).strip().lower()
        cmd_map = {
            "low": "set_low_power",
            "normal": "set_normal_power",
            "high": "set_high_power",
        }
        if v not in cmd_map:
            raise ValueError("mode must be low|normal|high")
        resp = privileged_cmd({"cmd": cmd_map[v]}, password)
        with _state_lock:
            _state["mode_cmd"] = v
            _state["last_write"] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": "mode",
                "value": v,
            }
            _state["last_write_result"] = resp
            _save_state()
        _invalidate_cache()
        return {"ok": True, "action": "mode", "value": v, "response": resp}

    if action == "power_pct":
        pct = int(value)
        if not 0 <= pct <= 100:
            raise ValueError("power_pct must be 0..100")
        resp = privileged_cmd({"cmd": "set_power_pct", "percent": str(pct)}, password)
        with _state_lock:
            _state["power_pct_cmd"] = pct
            _state["last_write"] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": "power_pct",
                "value": pct,
            }
            _state["last_write_result"] = resp
            _save_state()
        _invalidate_cache()
        return {"ok": True, "action": "power_pct", "value": pct, "response": resp}

    if action in ("power_limit", "set_power_limit", "adjust_power_limit"):
        watts = int(value)
        if watts < 0 or watts > 20000:
            raise ValueError("power_limit out of range")
        resp = privileged_cmd(
            {"cmd": "adjust_power_limit", "power_limit": str(watts)}, password
        )
        with _state_lock:
            _state["power_limit_cmd"] = watts
            _state["last_write"] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": "power_limit",
                "value": watts,
            }
            _state["last_write_result"] = resp
            _save_state()
        _invalidate_cache()
        return {
            "ok": True,
            "action": "power_limit",
            "value": watts,
            "response": resp,
            "warning": "adjust_power_limit may reboot / restart mining",
        }

    raise ValueError(f"unknown action: {action}")


def _invalidate_cache() -> None:
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


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
                # keep going
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
        if path == "/api/history/prune":
            with _hist_cfg_lock:
                days = int(_hist_cfg.get("retention_days", 7))
            n = prune_old(days)
            self._json_response(200, {"ok": True, "deleted": n, "retention_days": days})
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
            body["last_write"] = _state.get("last_write")
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
            self._json_response(400, {"ok": False, "error": str(e)})

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


def main() -> None:
    init_db()
    t = threading.Thread(target=collector_loop, name="history-collector", daemon=True)
    t.start()

    server = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), Handler)
    print(f"poolheat UI:       http://{HTTP_BIND}:{HTTP_PORT}/")
    print(f"www:               {ROOT}")
    print(f"data:              {DATA}")
    print(f"live API:          GET  /api/live")
    print(f"set API:           POST /api/set")
    print(f"history:           GET  /api/history?hours=24")
    print(f"miner:             {HOST_MINER}:{PORT_MINER}")
    print(f"db:                {DB_FILE}")
    print(
        f"collector:         every {_hist_cfg['sample_interval_sec']}s · "
        f"keep {_hist_cfg['retention_days']}d"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _collector_stop.set()
        print("\nstop")


if __name__ == "__main__":
    main()
