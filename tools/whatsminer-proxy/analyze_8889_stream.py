#!/usr/bin/env python3
"""
Rebuild TCP streams for Whatsminer :8889 from a clean pcap and dump crypto-relevant frames.

Designed for capture-8889-clean.sh output (single iface, host X and port 8889).

Usage:
  python3 analyze_8889_stream.py pcap/wmt8889.pcap
  python3 analyze_8889_stream.py pcap/wmt8889.pcap --out-dir pcap/stream-out

Emits:
  · per-stream timeline (c2s/s2c lengths + head hex + tags)
  · unique 64/80/128 B client blobs
  · server ZZ frames (reject vs MinerInfo)
  · JSON summary for further reverse
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path


MAGIC = bytes.fromhex("5a5a7f7f")
ERR_TAIL = bytes.fromhex("0000000002000000ffff0000")


# ── minimal pcap reader (classic + nanosecond, linktypes 1 / 113 / 276) ─────


def _u32(b: bytes, o: int, le: bool) -> int:
    return struct.unpack_from("<I" if le else ">I", b, o)[0]


def iter_pcap_packets(path: Path):
    raw = path.read_bytes()
    if len(raw) < 24:
        raise ValueError("pcap too short")
    magic = raw[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
        le = magic == b"\xd4\xc3\xb2\xa1"
        ns = False
    elif magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        le = magic == b"\x4d\x3c\xb2\xa1"
        ns = True
    else:
        raise ValueError(f"unsupported pcap magic {magic.hex()}")
    linktype = _u32(raw, 20, le)
    off = 24
    n = 0
    while off + 16 <= len(raw):
        ts_sec = _u32(raw, off, le)
        ts_usec = _u32(raw, off + 4, le)
        incl = _u32(raw, off + 8, le)
        # orig = _u32(raw, off + 12, le)
        pkt = raw[off + 16 : off + 16 + incl]
        off += 16 + incl
        n += 1
        ts = float(ts_sec) + (ts_usec * 1e-9 if ns else ts_usec * 1e-6)
        yield n, ts, linktype, pkt


def _ipv4_tcp_payload(pkt: bytes, linktype: int):
    """Return (sip, dip, sport, dport, payload, tcp_flags) or None."""
    if linktype == 1:  # Ethernet
        if len(pkt) < 14:
            return None
        ethertype = struct.unpack(">H", pkt[12:14])[0]
        if ethertype == 0x8100 and len(pkt) >= 18:  # VLAN
            ethertype = struct.unpack(">H", pkt[16:18])[0]
            ip = pkt[18:]
        elif ethertype == 0x0800:
            ip = pkt[14:]
        else:
            return None
    elif linktype == 113:  # Linux SLL
        if len(pkt) < 16:
            return None
        proto = struct.unpack(">H", pkt[14:16])[0]
        if proto != 0x0800:
            return None
        ip = pkt[16:]
    elif linktype == 276:  # Linux SLL2
        if len(pkt) < 20:
            return None
        proto = struct.unpack(">H", pkt[0:2])[0]
        if proto != 0x0800:
            return None
        ip = pkt[20:]
    else:
        # try ethernet offset 0
        if len(pkt) >= 14 and struct.unpack(">H", pkt[12:14])[0] == 0x0800:
            ip = pkt[14:]
        else:
            return None

    if len(ip) < 20 or (ip[0] >> 4) != 4:
        return None
    ihl = (ip[0] & 0xF) * 4
    if ip[9] != 6:
        return None
    total_len = struct.unpack(">H", ip[2:4])[0]
    ip_body = ip[:total_len] if total_len <= len(ip) else ip
    src = ".".join(str(b) for b in ip_body[12:16])
    dst = ".".join(str(b) for b in ip_body[16:20])
    tcp = ip_body[ihl:]
    if len(tcp) < 20:
        return None
    sport, dport = struct.unpack(">HH", tcp[0:4])
    seq = struct.unpack(">I", tcp[4:8])[0]
    doff = ((tcp[12] >> 4) & 0xF) * 4
    flags = tcp[13]
    payload = tcp[doff:]
    return src, dst, sport, dport, payload, flags, seq


def stream_key(sip, dip, sport, dport, miner_port=8889):
    """Canonical key: (client_ip, client_port, miner_ip) for miner:8889."""
    if dport == miner_port:
        return (sip, sport, dip, "c2s")
    if sport == miner_port:
        return (dip, dport, sip, "s2c")
    return None


def tag_payload(direction: str, data: bytes) -> str:
    if not data:
        return "empty"
    if direction == "c2s":
        if len(data) == 64:
            return (
                f"AUTH64 mid={data[16:32].hex()[:16]}… "
                f"tail={data[48:64].hex()[:16]}…"
            )
        if len(data) == 80:
            return f"WRITE80 mid={data[16:32].hex()[:16]}…"
        if len(data) == 128:
            return f"WRITE128 mid={data[16:32].hex()[:16]}…"
        if len(data) == 4:
            return f"SHORT4 {data.hex()}"
        return f"C2S_{len(data)} head={data[:16].hex()}"
    # s2c
    if data[:4] == MAGIC:
        if len(data) == 16 and data[4:16] == ERR_TAIL:
            return "ZZ_REJECT"
        typ = int.from_bytes(data[4:8], "little") if len(data) >= 8 else -1
        body = data[16:] if len(data) > 16 else b""
        if b"[MinerInfo]" in body or b"MinerType" in body:
            return f"ZZ_MINERINFO type={typ} body={len(body)}"
        if b"[PowerInfo]" in body:
            return f"ZZ_POWERINFO type={typ} body={len(body)}"
        return f"ZZ_type={typ} len={len(data)}"
    if data == b"\x00" * len(data) and len(data) <= 8:
        return f"KEEPALIVE_{len(data)}"
    # ASCII date / CSV telemetry without ZZ
    if data[:1].isalpha() or (len(data) > 4 and 48 <= data[0] <= 57):
        sample = data[:40].decode("utf-8", "replace").replace("\n", "\\n")
        return f"ASCII len={len(data)} {sample!r}"
    return f"S2C_{len(data)} head={data[:16].hex()}"


def rebuild_streams(path: Path, miner_port: int = 8889):
    """
    Group by (client_ip, client_port, miner_ip).
    Reassemble payload by seq (simple, ignore retransmit dups).
    """
    # events: list of ordered segments per stream
    streams: dict[tuple, list] = defaultdict(list)
    meta = {"packets": 0, "tcp_8889": 0, "linktype": None}

    for n, ts, linktype, pkt in iter_pcap_packets(path):
        meta["packets"] += 1
        meta["linktype"] = linktype
        parsed = _ipv4_tcp_payload(pkt, linktype)
        if not parsed:
            continue
        sip, dip, sport, dport, payload, flags, seq = parsed
        if sport != miner_port and dport != miner_port:
            continue
        meta["tcp_8889"] += 1
        if dport == miner_port:
            key = (sip, sport, dip)
            direction = "c2s"
            client = sip
        else:
            key = (dip, dport, sip)
            direction = "s2c"
            client = dip
        streams[key].append(
            {
                "n": n,
                "ts": ts,
                "dir": direction,
                "seq": seq,
                "flags": flags,
                "payload": payload,
                "client": client,
            }
        )

    # sort each stream by packet number (capture order) — better than seq alone
    # for multipath-free captures; also dedupe identical (seq, dir, payload)
    out = []
    for key, segs in streams.items():
        segs = sorted(segs, key=lambda s: (s["ts"], s["n"]))
        deduped = []
        seen = set()
        for s in segs:
            sig = (s["dir"], s["seq"], s["payload"])
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append(s)
        # merge consecutive same-dir payloads into messages (simple)
        messages = []
        buf = b""
        buf_dir = None
        buf_ts = None
        for s in deduped:
            if not s["payload"]:
                # SYN/ACK etc — record empty with flags if SYN
                if s["flags"] & 0x02:  # SYN
                    messages.append(
                        {
                            "ts": s["ts"],
                            "dir": s["dir"],
                            "data": b"",
                            "tag": "TCP_SYN" if s["dir"] == "c2s" else "TCP_SYNACK",
                            "flags": s["flags"],
                        }
                    )
                continue
            if buf_dir is None:
                buf_dir = s["dir"]
                buf_ts = s["ts"]
                buf = s["payload"]
            elif s["dir"] == buf_dir:
                buf += s["payload"]
            else:
                messages.append(
                    {
                        "ts": buf_ts,
                        "dir": buf_dir,
                        "data": buf,
                        "tag": tag_payload(buf_dir, buf),
                    }
                )
                buf_dir = s["dir"]
                buf_ts = s["ts"]
                buf = s["payload"]
        if buf_dir is not None and buf:
            messages.append(
                {
                    "ts": buf_ts,
                    "dir": buf_dir,
                    "data": buf,
                    "tag": tag_payload(buf_dir, buf),
                }
            )

        c2s_lens = [len(m["data"]) for m in messages if m["dir"] == "c2s" and m["data"]]
        s2c_lens = [len(m["data"]) for m in messages if m["dir"] == "s2c" and m["data"]]
        out.append(
            {
                "client": f"{key[0]}:{key[1]}",
                "miner": key[2],
                "n_messages": len(messages),
                "c2s_lens": c2s_lens,
                "s2c_lens": s2c_lens,
                "has_write80": 80 in c2s_lens,
                "has_write128": 128 in c2s_lens,
                "has_auth64": 64 in c2s_lens,
                "messages": messages,
            }
        )

    # prefer streams with writes, then most messages
    out.sort(
        key=lambda s: (
            not s["has_write128"],
            not s["has_write80"],
            not s["has_auth64"],
            -s["n_messages"],
        )
    )
    return meta, out


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze clean :8889 pcap streams")
    ap.add_argument("pcap", type=Path)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--port", type=int, default=8889)
    ap.add_argument("--max-streams", type=int, default=10)
    args = ap.parse_args()

    if not args.pcap.is_file():
        print(f"missing {args.pcap}", file=sys.stderr)
        return 1

    meta, streams = rebuild_streams(args.pcap, miner_port=args.port)
    print(f"pcap: {args.pcap}")
    print(f"packets={meta['packets']} tcp_8889={meta['tcp_8889']} linktype={meta['linktype']}")
    print(f"streams={len(streams)}")

    out_dir = args.out_dir
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "pcap": str(args.pcap),
        "meta": meta,
        "streams": [],
    }

    for i, st in enumerate(streams[: args.max_streams]):
        print()
        print(
            f"=== stream[{i}] {st['client']} → {st['miner']}:8889  "
            f"msgs={st['n_messages']} write80={st['has_write80']} "
            f"write128={st['has_write128']} ==="
        )
        t0 = st["messages"][0]["ts"] if st["messages"] else 0
        for j, m in enumerate(st["messages"]):
            dt = m["ts"] - t0
            arrow = "→" if m["dir"] == "c2s" else "←"
            print(f"  {j:3d} +{dt:7.3f}s {arrow} {m['tag']}")

        # export blobs
        st_sum = {
            "client": st["client"],
            "miner": st["miner"],
            "c2s_lens": st["c2s_lens"],
            "s2c_lens": st["s2c_lens"],
            "has_write80": st["has_write80"],
            "has_write128": st["has_write128"],
            "timeline": [],
            "auth64_hex": [],
            "write80_hex": [],
            "write128_hex": [],
        }
        for m in st["messages"]:
            st_sum["timeline"].append(
                {
                    "dir": m["dir"],
                    "len": len(m["data"]),
                    "tag": m["tag"],
                    "head32_hex": m["data"][:32].hex() if m["data"] else "",
                }
            )
            if m["dir"] == "c2s" and len(m["data"]) == 64:
                st_sum["auth64_hex"].append(m["data"].hex())
            if m["dir"] == "c2s" and len(m["data"]) == 80:
                st_sum["write80_hex"].append(m["data"].hex())
            if m["dir"] == "c2s" and len(m["data"]) == 128:
                st_sum["write128_hex"].append(m["data"].hex())

        summary["streams"].append(st_sum)

        if out_dir:
            base = out_dir / f"stream{i}_{st['client'].replace(':', '_')}"
            base.mkdir(parents=True, exist_ok=True)
            for j, m in enumerate(st["messages"]):
                if not m["data"]:
                    continue
                side = "c2s" if m["dir"] == "c2s" else "s2c"
                (base / f"{j:03d}_{side}_{len(m['data'])}b.bin").write_bytes(m["data"])
            (base / "timeline.txt").write_text(
                "\n".join(
                    f"{k:3d} {m['dir']:3s} {len(m['data']):5d} {m['tag']}"
                    for k, m in enumerate(st["messages"])
                )
                + "\n",
                encoding="utf-8",
            )

    if out_dir:
        (out_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {out_dir}/summary.json + stream blobs")

    # hint
    interesting = [s for s in streams if s["has_write80"] or s["has_write128"]]
    if interesting:
        print(f"\n★ {len(interesting)} stream(s) contain WRITE 80/128 — reverse those first")
    else:
        print(
            "\n· no 80/128 B writes found — capture may be poll-only; "
            "repeat with one Remote Ctrl action after connect"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
