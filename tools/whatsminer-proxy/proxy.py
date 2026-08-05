#!/usr/bin/env python3
"""
whatsminer-proxy — local TCP proxy that emulates ASIC ports on the laptop
and forwards to the real miner, while logging everything Tools / poolheat send.

Purpose
-------
Study how WhatsMinerTools controls an ASIC when «Miner API Switch» / public
write API is OFF: capture :8889 remote, :4028/:4433, and HTTP(S) tunnels.

Usage
-----
  cd tools/whatsminer-proxy
  cp config.example.json config.json   # edit target_host if needed
  python3 proxy.py                     # or: python3 proxy.py -c config.json

Point WhatsMinerTools / poolheat at this Mac's LAN IP (e.g. 192.168.1.34)
instead of 192.168.1.10. Leave the same port numbers (4028, 4433, 8889…).

Logs
----
  logs/sessions.jsonl     — one JSON line per framed message (parsed when possible)
  logs/raw/CONN-*.log     — full duplex hex+ascii dump per TCP connection
  stdout                  — short human summary

Safety
------
This is a transparent forwarder for YOUR miner on the LAN. It does not
implement privileged crypto; it only observes and relays.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import struct
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ─── defaults ────────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "listen_host": "0.0.0.0",
    "target_host": "192.168.1.10",
    "log_dir": "logs",
    "console_verbose": True,
    "hex_preview_bytes": 256,
    "ports": [
        {"name": "api_v2", "listen": 4028, "target": 4028, "parse": "json_line", "enabled": True},
        {"name": "api_v3", "listen": 4433, "target": 4433, "parse": "json_len_le", "enabled": True},
        {"name": "tools_remote", "listen": 8889, "target": 8889, "parse": "tools_8889", "enabled": True},
        {"name": "https_tcp", "listen": 8443, "target": 443, "parse": "raw", "enabled": True},
        {"name": "http", "listen": 8080, "target": 80, "parse": "http", "enabled": True},
    ],
}

_conn_seq = 0
_conn_lock = threading.Lock()
_session_lock = threading.Lock()


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _local_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _next_conn_id() -> int:
    global _conn_seq
    with _conn_lock:
        _conn_seq += 1
        return _conn_seq


# ─── formatting ──────────────────────────────────────────────────────────────


def hexdump(data: bytes, width: int = 16, limit: int = 512) -> str:
    if not data:
        return ""
    chunk = data[:limit]
    lines = []
    for i in range(0, len(chunk), width):
        part = chunk[i : i + width]
        hx = " ".join(f"{b:02x}" for b in part)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in part)
        lines.append(f"  {i:04x}  {hx:<{width * 3}}  |{asc}|")
    if len(data) > limit:
        lines.append(f"  … +{len(data) - limit} bytes")
    return "\n".join(lines)


def try_json(text: str) -> Any | None:
    text = text.strip().replace("\x00", "")
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except Exception:
        # first object only
        try:
            obj, _ = json.JSONDecoder().raw_decode(text)
            return obj
        except Exception:
            return None


def summarize_json(obj: Any) -> str:
    if not isinstance(obj, dict):
        return repr(obj)[:120]
    cmd = obj.get("cmd") or obj.get("command") or ""
    enc = obj.get("enc")
    code = obj.get("code") if "code" in obj else obj.get("Code")
    status = obj.get("STATUS") or obj.get("status")
    msg = obj.get("Msg") if "Msg" in obj else obj.get("msg")
    bits = []
    if cmd:
        bits.append(f"cmd={cmd}")
    if enc is not None:
        bits.append(f"enc={enc}")
    if status is not None:
        bits.append(f"STATUS={status}")
    if code is not None:
        bits.append(f"code={code}")
    if isinstance(msg, dict):
        keys = list(msg.keys())[:8]
        bits.append(f"Msg.{{keys={keys}}}")
    elif msg is not None:
        bits.append(f"Msg={str(msg)[:60]!r}")
    # token / data blobs
    for k in ("token", "data", "param", "salt"):
        if k in obj and obj[k] not in (None, ""):
            v = obj[k]
            if isinstance(v, str) and len(v) > 24:
                bits.append(f"{k}=<{len(v)}ch>")
            else:
                bits.append(f"{k}={v!r}"[:40])
    return " ".join(bits) if bits else str(obj)[:100]


# ─── stream parsers (client→target and target→client independently) ──────────


class StreamParser:
    """Incremental framer; returns list of (kind, payload_bytes, meta_dict)."""

    def __init__(self, parse: str, direction: str):
        self.parse = parse or "raw"
        self.direction = direction  # c2s | s2c
        self.buf = bytearray()
        self.msg_n = 0
        self._tools_phase = 0  # 0 first pkt, 1 after

    def feed(self, data: bytes) -> list[tuple[str, bytes, dict]]:
        if not data:
            return []
        self.buf.extend(data)
        out: list[tuple[str, bytes, dict]] = []
        if self.parse == "json_line":
            out.extend(self._json_line())
        elif self.parse == "json_len_le":
            out.extend(self._json_len_le())
        elif self.parse == "http":
            out.extend(self._http())
        elif self.parse == "tools_8889":
            out.extend(self._tools_8889())
        else:
            # raw: emit whole buffer as one chunk each feed (stream dump)
            chunk = bytes(self.buf)
            self.buf.clear()
            self.msg_n += 1
            out.append(("raw", chunk, {"n": self.msg_n, "len": len(chunk)}))
        return out

    def _json_line(self) -> list[tuple[str, bytes, dict]]:
        out = []
        while True:
            # Whatsminer often null-terminates or newline
            nl = self.buf.find(b"\n")
            nul = self.buf.find(b"\x00")
            cut = -1
            if nl >= 0 and nul >= 0:
                cut = min(nl, nul)
            elif nl >= 0:
                cut = nl
            elif nul >= 0:
                cut = nul
            if cut < 0:
                # complete JSON object without terminator?
                if self.buf[:1] == b"{" and self.buf.count(b"{") <= self.buf.count(b"}"):
                    try:
                        text = bytes(self.buf).decode("utf-8", "replace")
                        obj, end = json.JSONDecoder().raw_decode(text.lstrip())
                        # end is char index — approx use full buffer if decode ok
                        raw = bytes(self.buf)
                        self.buf.clear()
                        self.msg_n += 1
                        meta = {"n": self.msg_n, "json": obj, "summary": summarize_json(obj)}
                        out.append(("json", raw, meta))
                        continue
                    except Exception:
                        pass
                break
            raw = bytes(self.buf[: cut + 1])
            del self.buf[: cut + 1]
            self.msg_n += 1
            text = raw.replace(b"\x00", b"").decode("utf-8", "replace")
            obj = try_json(text)
            meta: dict = {"n": self.msg_n, "len": len(raw)}
            if obj is not None:
                meta["json"] = obj
                meta["summary"] = summarize_json(obj)
            else:
                meta["summary"] = text.strip()[:100]
            out.append(("json_line", raw, meta))
        return out

    def _json_len_le(self) -> list[tuple[str, bytes, dict]]:
        out = []
        while len(self.buf) >= 4:
            (n,) = struct.unpack_from("<I", self.buf, 0)
            if n <= 0 or n > 2_000_000:
                # resync: drop one byte
                del self.buf[0]
                continue
            if len(self.buf) < 4 + n:
                break
            body = bytes(self.buf[4 : 4 + n])
            del self.buf[: 4 + n]
            self.msg_n += 1
            text = body.decode("utf-8", "replace")
            obj = try_json(text)
            meta: dict = {"n": self.msg_n, "len": n, "frame": "le32+json"}
            if obj is not None:
                meta["json"] = obj
                meta["summary"] = summarize_json(obj)
            else:
                meta["summary"] = text[:100]
            out.append(("json_v3", body, meta))
        return out

    def _http(self) -> list[tuple[str, bytes, dict]]:
        out = []
        # emit complete headers+body when Content-Length known, else on double CRLF for reqs without body
        while True:
            sep = self.buf.find(b"\r\n\r\n")
            if sep < 0:
                break
            header_blob = bytes(self.buf[:sep])
            headers_text = header_blob.decode("latin-1", "replace")
            clen = 0
            for line in headers_text.split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    try:
                        clen = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        clen = 0
            total = sep + 4 + clen
            if len(self.buf) < total:
                break
            raw = bytes(self.buf[:total])
            del self.buf[:total]
            self.msg_n += 1
            first = headers_text.split("\r\n", 1)[0]
            meta = {
                "n": self.msg_n,
                "len": len(raw),
                "summary": first[:160],
                "http_start": first[:160],
            }
            # form body preview
            body = raw[sep + 4 :]
            if body:
                try:
                    bt = body.decode("utf-8", "replace")
                    if "cbid." in bt or "open_by_api" in bt or "apiswitch" in bt:
                        meta["hint"] = "LuCI form / open_by_api / apiswitch?"
                    meta["body_preview"] = bt[:300]
                except Exception:
                    pass
            out.append(("http", raw, meta))
        return out

    def _tools_8889(self) -> list[tuple[str, bytes, dict]]:
        """
        Proprietary Remote Ctrl.
        Observed pattern: early packets often fixed 16-byte challenge / response.
        We emit each TCP read as a framed event + mark length/hex head.
        """
        if not self.buf:
            return []
        raw = bytes(self.buf)
        self.buf.clear()
        self.msg_n += 1
        meta: dict = {
            "n": self.msg_n,
            "len": len(raw),
            "phase": self._tools_phase,
            "head16_hex": raw[:16].hex() if raw else "",
        }
        if self._tools_phase == 0 and len(raw) == 16:
            meta["summary"] = f"16B challenge/response? {raw.hex()}"
            meta["likely"] = "fixed_16"
        elif self._tools_phase == 0 and len(raw) < 64:
            meta["summary"] = f"short pkt {len(raw)}B head={raw[:32].hex()}"
        else:
            # try find embedded JSON
            obj = None
            for i, b in enumerate(raw):
                if b == ord("{"):
                    obj = try_json(raw[i:].decode("utf-8", "replace"))
                    if obj is not None:
                        break
            if obj is not None:
                meta["json"] = obj
                meta["summary"] = "json-in-blob " + summarize_json(obj)
            else:
                meta["summary"] = f"blob {len(raw)}B head={raw[:24].hex()}"
        self._tools_phase = min(self._tools_phase + 1, 9)
        return [("tools_8889", raw, meta)]


# ─── proxy connection ────────────────────────────────────────────────────────


class ProxyConn(threading.Thread):
    daemon = True

    def __init__(
        self,
        client: socket.socket,
        client_addr: tuple,
        *,
        target_host: str,
        target_port: int,
        port_name: str,
        parse: str,
        log_dir: Path,
        session_path: Path,
        console_verbose: bool,
        hex_preview: int,
    ):
        super().__init__(name=f"proxy-{port_name}")
        self.client = client
        self.client_addr = client_addr
        self.target_host = target_host
        self.target_port = target_port
        self.port_name = port_name
        self.parse = parse
        self.log_dir = log_dir
        self.session_path = session_path
        self.console_verbose = console_verbose
        self.hex_preview = hex_preview
        self.conn_id = _next_conn_id()
        self.raw_path = log_dir / "raw" / (
            f"{_local_ts().replace(':','').replace(' ','_')}"
            f"_{port_name}_{self.conn_id}.log"
        )
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_lock = threading.Lock()

    def _raw_write(self, text: str) -> None:
        with self._raw_lock:
            with open(self.raw_path, "a", encoding="utf-8") as f:
                f.write(text)
                if not text.endswith("\n"):
                    f.write("\n")

    def _session_write(self, rec: dict) -> None:
        rec = dict(rec)
        rec.setdefault("ts", _utc_ts())
        rec.setdefault("conn", self.conn_id)
        rec.setdefault("port", self.port_name)
        rec.setdefault("target", f"{self.target_host}:{self.target_port}")
        with _session_lock:
            with open(self.session_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def _note(
        self,
        direction: str,
        kind: str,
        payload: bytes,
        meta: dict,
    ) -> None:
        summary = meta.get("summary") or f"{kind} {len(payload)}B"
        arrow = "→" if direction == "c2s" else "←"
        line = (
            f"[{_local_ts()}] #{self.conn_id} {self.port_name} "
            f"{arrow} {direction} {summary}"
        )
        if self.console_verbose:
            print(line, flush=True)
        self._raw_write(
            f"\n=== {direction} {kind} n={meta.get('n')} len={len(payload)} ===\n"
            f"{summary}\n"
            f"{hexdump(payload, limit=self.hex_preview)}\n"
        )
        # session jsonl — strip huge binary
        rec = {
            "dir": direction,
            "kind": kind,
            "summary": summary,
            "len": len(payload),
            "n": meta.get("n"),
        }
        if "json" in meta:
            rec["json"] = meta["json"]
        if "http_start" in meta:
            rec["http_start"] = meta["http_start"]
        if "body_preview" in meta:
            rec["body_preview"] = meta["body_preview"]
        if "hint" in meta:
            rec["hint"] = meta["hint"]
        if "head16_hex" in meta:
            rec["head16_hex"] = meta["head16_hex"]
        if "likely" in meta:
            rec["likely"] = meta["likely"]
        # small hex head always
        rec["hex_head"] = payload[:64].hex()
        self._session_write(rec)

    def run(self) -> None:
        peer = f"{self.client_addr[0]}:{self.client_addr[1]}"
        self._raw_write(
            f"# conn={self.conn_id} port={self.port_name} peer={peer} "
            f"→ {self.target_host}:{self.target_port} parse={self.parse}\n"
            f"# started {_utc_ts()}\n"
        )
        self._session_write(
            {
                "event": "connect",
                "peer": peer,
                "parse": self.parse,
            }
        )
        print(
            f"[{_local_ts()}] #{self.conn_id} OPEN {self.port_name} "
            f"{peer} → {self.target_host}:{self.target_port}",
            flush=True,
        )
        upstream = None
        try:
            upstream = socket.create_connection(
                (self.target_host, self.target_port), timeout=15
            )
            upstream.settimeout(None)
            self.client.settimeout(None)
            # disable Nagle for snappier interactive Tools
            try:
                self.client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

            p_c2s = StreamParser(self.parse, "c2s")
            p_s2c = StreamParser(self.parse, "s2c")

            sockets = [self.client, upstream]
            idle_deadline = time.time() + 600  # 10 min idle hard stop
            while True:
                r, _, x = select.select(sockets, [], sockets, 1.0)
                if x:
                    break
                if not r:
                    if time.time() > idle_deadline:
                        break
                    continue
                idle_deadline = time.time() + 600
                for s in r:
                    try:
                        data = s.recv(65536)
                    except OSError:
                        data = b""
                    if not data:
                        sockets = []
                        break
                    if s is self.client:
                        # client → ASIC
                        for kind, payload, meta in p_c2s.feed(data):
                            self._note("c2s", kind, payload, meta)
                        try:
                            upstream.sendall(data)
                        except OSError:
                            sockets = []
                            break
                    else:
                        # ASIC → client
                        for kind, payload, meta in p_s2c.feed(data):
                            self._note("s2c", kind, payload, meta)
                        try:
                            self.client.sendall(data)
                        except OSError:
                            sockets = []
                            break
                if not sockets:
                    break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(
                f"[{_local_ts()}] #{self.conn_id} ERR {self.port_name} {err}",
                flush=True,
            )
            self._raw_write(f"# ERROR {err}\n{traceback.format_exc()}\n")
            self._session_write({"event": "error", "error": err})
        finally:
            for s in (self.client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
            self._session_write({"event": "close"})
            self._raw_write(f"# closed {_utc_ts()}\n")
            print(
                f"[{_local_ts()}] #{self.conn_id} CLOSE {self.port_name}",
                flush=True,
            )


# ─── listeners ───────────────────────────────────────────────────────────────


class PortListener(threading.Thread):
    daemon = True

    def __init__(self, cfg_port: dict, global_cfg: dict, log_dir: Path, session_path: Path):
        super().__init__(name=f"listen-{cfg_port.get('name')}")
        self.cfg_port = cfg_port
        self.g = global_cfg
        self.log_dir = log_dir
        self.session_path = session_path
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def run(self) -> None:
        name = self.cfg_port.get("name") or "port"
        listen_host = self.g.get("listen_host") or "0.0.0.0"
        listen_port = int(self.cfg_port["listen"])
        target_host = str(self.g.get("target_host") or "192.168.1.10")
        target_port = int(self.cfg_port.get("target") or listen_port)
        parse = str(self.cfg_port.get("parse") or "raw")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((listen_host, listen_port))
        except OSError as e:
            print(
                f"[!] cannot bind {listen_host}:{listen_port} ({name}): {e}\n"
                f"    tip: ports <1024 need sudo; or disable this entry in config",
                flush=True,
            )
            return
        sock.listen(50)
        sock.settimeout(1.0)
        self._sock = sock
        print(
            f"[+] {name:14} listen {listen_host}:{listen_port}  "
            f"→ {target_host}:{target_port}  parse={parse}",
            flush=True,
        )
        while not self._stop.is_set():
            try:
                client, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ProxyConn(
                client,
                addr,
                target_host=target_host,
                target_port=target_port,
                port_name=name,
                parse=parse,
                log_dir=self.log_dir,
                session_path=self.session_path,
                console_verbose=bool(self.g.get("console_verbose", True)),
                hex_preview=int(self.g.get("hex_preview_bytes") or 256),
            ).start()
        try:
            sock.close()
        except OSError:
            pass


# ─── main ────────────────────────────────────────────────────────────────────


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))  # deep-ish copy
    if path and path.is_file():
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise SystemExit("config must be a JSON object")
        for k, v in user.items():
            if k == "ports" and isinstance(v, list):
                cfg["ports"] = v
            else:
                cfg[k] = v
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Whatsminer Tools capture proxy")
    ap.add_argument(
        "-c",
        "--config",
        default=None,
        help="config.json (default: ./config.json if exists, else built-in)",
    )
    ap.add_argument(
        "--target",
        default=None,
        help="override target_host (real ASIC IP)",
    )
    ap.add_argument(
        "--list-only",
        action="store_true",
        help="print ports and exit",
    )
    args = ap.parse_args(argv)

    here = Path(__file__).resolve().parent
    cfg_path = Path(args.config) if args.config else here / "config.json"
    if not cfg_path.is_file() and not args.config:
        ex = here / "config.example.json"
        if ex.is_file():
            print(f"[i] no config.json — using defaults (see {ex.name})", flush=True)
            cfg_path = None
        else:
            cfg_path = None
    elif not cfg_path.is_file():
        raise SystemExit(f"config not found: {cfg_path}")

    cfg = load_config(cfg_path if cfg_path and cfg_path.is_file() else None)
    if args.target:
        cfg["target_host"] = args.target

    log_dir = Path(cfg.get("log_dir") or "logs")
    if not log_dir.is_absolute():
        log_dir = here / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "raw").mkdir(parents=True, exist_ok=True)
    session_path = log_dir / "sessions.jsonl"

    ports = [p for p in (cfg.get("ports") or []) if p.get("enabled", True)]
    print("=" * 64, flush=True)
    print("  Whatsminer capture proxy", flush=True)
    print(f"  target ASIC : {cfg.get('target_host')}", flush=True)
    print(f"  logs        : {log_dir}", flush=True)
    print(f"  sessions    : {session_path}", flush=True)
    print("=" * 64, flush=True)
    print(
        "Point WhatsMinerTools / poolheat miner host at THIS machine's LAN IP.\n"
        "Then operate Tools (Enable API, Mining Control, Power Mode, Limit)\n"
        "with API switch OFF — watch :8889 / :4028 / :4433 / HTTP traffic.\n",
        flush=True,
    )

    if args.list_only:
        for p in ports:
            print(
                f"  {p.get('name')}: listen {p.get('listen')} → "
                f"{cfg.get('target_host')}:{p.get('target')} parse={p.get('parse')}"
            )
        return 0

    listeners: list[PortListener] = []
    for p in ports:
        if not p.get("listen"):
            continue
        ln = PortListener(p, cfg, log_dir, session_path)
        ln.start()
        listeners.append(ln)

    if not listeners:
        print("[!] no ports enabled", flush=True)
        return 1

    print("[i] running — Ctrl+C to stop\n", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[i] stopping…", flush=True)
        for ln in listeners:
            ln.stop()
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
