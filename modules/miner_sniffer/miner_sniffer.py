#!/usr/bin/env python3
"""
Miner traffic sniffer module for poolheat (optional).

Runs tcpdump on the router (Entware) capturing traffic to/from the miner.
Installed on demand from GitHub — not part of the base package.

Lifecycle (called from serve.py):
  configure(...) / apply() / start() / stop() / status() / uninstall_runtime()

Does NOT stop poolheat (unlike lab capture scripts). Tracks only its own pid.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

MODULE_VERSION = "1.0.0"
MODULE_ID = "miner_sniffer"

DEFAULT_IFACE = "br0"
DEFAULT_PCAP_NAME = "poolheat-sniffer.pcap"
DEFAULT_SNAPLEN = 0  # full packets

# Prefer Entware binary on Keenetic Peak
_TCPDUMP_CANDIDATES = (
    "/opt/bin/tcpdump",
    "/usr/sbin/tcpdump",
    "/usr/bin/tcpdump",
    "tcpdump",
)

_lock = threading.RLock()
_cfg: dict[str, Any] = {
    "enabled": False,
    "iface": DEFAULT_IFACE,
    "filter": "",  # empty → host <miner>
    "pcap_dir": "/tmp/poolheat-sniffer",
    "snaplen": DEFAULT_SNAPLEN,
    "miner_host": "",
}
_pid: int | None = None
_started_at: float | None = None
_last_error: str | None = None
_host_resolver: Callable[[], str] | None = None
_install_dir: Path | None = None  # set by serve after install


def set_host_resolver(fn: Callable[[], str] | None) -> None:
    global _host_resolver
    with _lock:
        _host_resolver = fn


def set_install_dir(path: str | Path | None) -> None:
    global _install_dir
    with _lock:
        _install_dir = Path(path) if path else None


def get_config() -> dict[str, Any]:
    with _lock:
        return dict(_cfg)


def configure(
    *,
    enabled: bool | None = None,
    iface: str | None = None,
    filter: str | None = None,
    pcap_dir: str | None = None,
    snaplen: int | None = None,
    miner_host: str | None = None,
) -> dict[str, Any]:
    """Update config fields (does not start/stop — call apply())."""
    with _lock:
        if enabled is not None:
            _cfg["enabled"] = bool(enabled)
        if iface is not None and str(iface).strip():
            _cfg["iface"] = str(iface).strip()
        if filter is not None:
            _cfg["filter"] = str(filter).strip()
        if pcap_dir is not None and str(pcap_dir).strip():
            _cfg["pcap_dir"] = str(pcap_dir).strip()
        if snaplen is not None:
            try:
                _cfg["snaplen"] = max(0, min(65535, int(snaplen)))
            except (TypeError, ValueError):
                _cfg["snaplen"] = DEFAULT_SNAPLEN
        if miner_host is not None and str(miner_host).strip():
            _cfg["miner_host"] = str(miner_host).strip()
        return dict(_cfg)


def _resolve_miner_host() -> str:
    with _lock:
        fn = _host_resolver
        fallback = str(_cfg.get("miner_host") or "").strip()
    if fn:
        try:
            h = str(fn() or "").strip()
            if h:
                return h
        except Exception:
            pass
    return fallback


def _build_filter() -> str:
    with _lock:
        custom = str(_cfg.get("filter") or "").strip()
    if custom:
        return custom
    host = _resolve_miner_host()
    if host:
        return f"host {host}"
    return "ip"  # fallback — capture all IP (operator should set miner host)


def find_tcpdump() -> str | None:
    for c in _TCPDUMP_CANDIDATES:
        if c == "tcpdump":
            p = shutil.which("tcpdump")
            if p:
                return p
            continue
        if Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


def _paths() -> dict[str, Path]:
    with _lock:
        pdir = Path(str(_cfg.get("pcap_dir") or "/tmp/poolheat-sniffer"))
    pdir.mkdir(parents=True, exist_ok=True)
    return {
        "dir": pdir,
        "pcap": pdir / DEFAULT_PCAP_NAME,
        "pid": pdir / "sniffer.pid",
        "log": pdir / "sniffer.log",
    }


def _read_pid_file() -> int | None:
    paths = _paths()
    try:
        raw = paths["pid"].read_text(encoding="utf-8").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal freely
    except OSError:
        return False


def _is_our_tcpdump(pid: int) -> bool:
    """Best-effort: cmdline contains tcpdump and our pcap path."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace"
        )
    except Exception:
        # macOS / no proc — trust pid file
        return True
    pcap = str(_paths()["pcap"])
    return "tcpdump" in cmd and (pcap in cmd or "poolheat-sniffer" in cmd)


def _sync_pid_from_disk() -> None:
    global _pid, _started_at
    pid = _read_pid_file()
    if pid and _pid_alive(pid) and _is_our_tcpdump(pid):
        _pid = pid
        if _started_at is None:
            try:
                _started_at = _paths()["pid"].stat().st_mtime
            except Exception:
                _started_at = time.time()
    else:
        if _pid and not _pid_alive(_pid):
            _pid = None
            _started_at = None
        if pid and not _pid_alive(pid):
            try:
                _paths()["pid"].unlink(missing_ok=True)
            except Exception:
                pass


def is_running() -> bool:
    with _lock:
        _sync_pid_from_disk()
        return bool(_pid and _pid_alive(_pid))


def start() -> dict[str, Any]:
    """Start tcpdump capture. Idempotent if already running."""
    global _pid, _started_at, _last_error
    with _lock:
        _sync_pid_from_disk()
        if _pid and _pid_alive(_pid):
            return {
                "ok": True,
                "already": True,
                "pid": _pid,
                "pcap": str(_paths()["pcap"]),
            }

        tcpdump = find_tcpdump()
        if not tcpdump:
            _last_error = (
                "tcpdump not found — install on Entware: opkg update && opkg install tcpdump"
            )
            return {"ok": False, "error": _last_error}

        paths = _paths()
        filt = _build_filter()
        iface = str(_cfg.get("iface") or DEFAULT_IFACE)
        snap = int(_cfg.get("snaplen") or 0)
        pcap = paths["pcap"]
        logf = paths["log"]

        # rotate previous capture so we don't append silently
        if pcap.is_file() and pcap.stat().st_size > 0:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            try:
                pcap.rename(paths["dir"] / f"poolheat-sniffer.{stamp}.pcap")
            except Exception:
                pass

        cmd = [
            tcpdump,
            "-i",
            iface,
            "-s",
            str(snap),
            "-U",
            "-w",
            str(pcap),
        ]
        # BPF expression as separate argv tokens (tcpdump joins remaining args)
        if filt:
            cmd.extend(filt.split())

        try:
            # Detach: setsid if available so SIGHUP doesn't kill capture
            setsid = shutil.which("setsid")
            if setsid:
                full = [setsid] + cmd
            else:
                full = cmd
            log_fh = open(logf, "ab", buffering=0)
            proc = subprocess.Popen(
                full,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            try:
                log_fh.close()
            except Exception:
                pass
            time.sleep(0.4)
            if proc.poll() is not None:
                # failed immediately
                err_tail = ""
                try:
                    err_tail = logf.read_text(encoding="utf-8", errors="replace")[-400:]
                except Exception:
                    pass
                _last_error = f"tcpdump exited rc={proc.returncode}: {err_tail or 'see sniffer.log'}"
                _pid = None
                _started_at = None
                return {"ok": False, "error": _last_error}

            _pid = proc.pid
            _started_at = time.time()
            _last_error = None
            paths["pid"].write_text(str(_pid) + "\n", encoding="utf-8")
            return {
                "ok": True,
                "pid": _pid,
                "iface": iface,
                "filter": filt,
                "pcap": str(pcap),
                "tcpdump": tcpdump,
            }
        except Exception as e:
            _last_error = str(e)
            _pid = None
            _started_at = None
            return {"ok": False, "error": _last_error}


def stop() -> dict[str, Any]:
    """Stop our tcpdump only (not killall)."""
    global _pid, _started_at, _last_error
    with _lock:
        _sync_pid_from_disk()
        pid = _pid or _read_pid_file()
        stopped = False
        if pid and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                _last_error = f"kill {pid}: {e}"
            # wait up to 2s
            for _ in range(20):
                if not _pid_alive(pid):
                    stopped = True
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
                stopped = not _pid_alive(pid)
            else:
                stopped = True
        else:
            stopped = True  # already down

        _pid = None
        _started_at = None
        try:
            _paths()["pid"].unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": True, "stopped": stopped, "was_pid": pid}


def apply() -> dict[str, Any]:
    """Start if enabled, else stop."""
    with _lock:
        en = bool(_cfg.get("enabled"))
    if en:
        return start()
    return stop()


def capture_files() -> list[dict[str, Any]]:
    """List pcap files in capture dir."""
    paths = _paths()
    out: list[dict[str, Any]] = []
    try:
        for p in sorted(paths["dir"].glob("*.pcap"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                st = p.stat()
                out.append(
                    {
                        "name": p.name,
                        "path": str(p),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
            except Exception:
                continue
    except Exception:
        pass
    return out


def disk_usage_bytes() -> int:
    total = 0
    for f in capture_files():
        total += int(f.get("size") or 0)
    try:
        logf = _paths()["log"]
        if logf.is_file():
            total += logf.stat().st_size
    except Exception:
        pass
    return total


def clear_captures(*, keep_current: bool = False) -> dict[str, Any]:
    """Delete pcap files. If keep_current and running, skip active file."""
    paths = _paths()
    active = paths["pcap"]
    removed: list[str] = []
    for f in capture_files():
        p = Path(f["path"])
        if keep_current and p.resolve() == active.resolve() and is_running():
            continue
        try:
            p.unlink(missing_ok=True)
            removed.append(p.name)
        except Exception:
            pass
    return {"ok": True, "removed": removed, "count": len(removed)}


def uninstall_runtime(*, remove_captures: bool = True) -> dict[str, Any]:
    """Stop capture and optionally wipe capture dir contents (not module files)."""
    st = stop()
    removed_caps: list[str] = []
    if remove_captures:
        cr = clear_captures(keep_current=False)
        removed_caps = list(cr.get("removed") or [])
        # remove empty dir remnants
        try:
            paths = _paths()
            for name in ("sniffer.log", "sniffer.pid"):
                try:
                    (paths["dir"] / name).unlink(missing_ok=True)
                except Exception:
                    pass
            # leave dir itself — serve may rmdir
        except Exception:
            pass
    with _lock:
        _cfg["enabled"] = False
    return {
        "ok": True,
        "stopped": st,
        "captures_removed": removed_caps,
    }


def status() -> dict[str, Any]:
    with _lock:
        _sync_pid_from_disk()
        cfg = dict(_cfg)
        pid = _pid
        started = _started_at
        err = _last_error
        install = str(_install_dir) if _install_dir else None

    paths = _paths()
    pcap = paths["pcap"]
    pcap_size = 0
    try:
        if pcap.is_file():
            pcap_size = pcap.stat().st_size
    except Exception:
        pass

    running = bool(pid and _pid_alive(pid))
    miner = _resolve_miner_host()
    filt = _build_filter()
    tcpdump = find_tcpdump()
    files = capture_files()

    return {
        "module": MODULE_ID,
        "module_version": MODULE_VERSION,
        "enabled": bool(cfg.get("enabled")),
        "running": running,
        "pid": pid if running else None,
        "started_at": started,
        "uptime_sec": (time.time() - started) if (running and started) else None,
        "iface": cfg.get("iface"),
        "filter": filt,
        "filter_custom": cfg.get("filter") or "",
        "miner_host": miner,
        "pcap": str(pcap),
        "pcap_size": pcap_size,
        "pcap_dir": str(paths["dir"]),
        "captures": files[:20],
        "captures_total": len(files),
        "disk_usage": disk_usage_bytes(),
        "tcpdump": tcpdump,
        "tcpdump_ok": bool(tcpdump),
        "install_dir": install,
        "error": err,
    }
