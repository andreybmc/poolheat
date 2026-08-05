#!/usr/bin/env python3
"""
Try to extract /usr/bin/remote-daemon from Whatsminer via LuCI (read-only).
Run from Peak or any host that can reach the miner (Mac may get LuCI 403).
"""
from __future__ import annotations

import argparse
import http.cookiejar
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def login(host: str, user: str, password: str, timeout: float = 15.0):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )
    body = urllib.parse.urlencode(
        {"luci_username": user, "luci_password": password}
    ).encode()
    req = urllib.request.Request(
        f"https://{host}/cgi-bin/luci",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "poolheat-extract/1.0",
        },
    )
    with op.open(req, timeout=timeout) as resp:
        _ = resp.read(512)
    cookies = list(jar)
    if not cookies:
        raise RuntimeError("LuCI login: no session cookie")
    return op


def get(op, host: str, path: str, timeout: float = 15.0) -> tuple[int, str]:
    url = f"https://{host}{path}"
    try:
        with op.open(url, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read().decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return int(e.code), raw.decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.10")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--out-dir", default="/tmp/m63-extract")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"login {args.host} …")
    op = login(args.host, args.user, args.password)
    print("login ok")

    # map interesting pages
    pages = [
        "/cgi-bin/luci/admin/status/overview",
        "/cgi-bin/luci/admin/system",
        "/cgi-bin/luci/admin/system/admin",
        "/cgi-bin/luci/admin/system/startup",
        "/cgi-bin/luci/admin/system/processes",
        "/cgi-bin/luci/admin/system/flashops",
        "/cgi-bin/luci/admin/system/packages",
        "/cgi-bin/luci/admin/system/commands",
        "/cgi-bin/luci/admin/system/filebrowser",
        "/cgi-bin/luci/admin/services",
        "/cgi-bin/luci/admin/services/dropbear",
        "/cgi-bin/luci/admin/network",
        "/cgi-bin/luci/admin/network/btminer",
        "/cgi-bin/luci/admin/network/btminer/power",
    ]
    found_links: set[str] = set()
    for path in pages:
        st, html = get(op, args.host, path)
        title_m = re.search(r"<title>([^<]+)", html)
        title = title_m.group(1).strip() if title_m else "?"
        print(f"{st:3} {path}  ({title})  len={len(html)}")
        (out / (path.strip("/").replace("/", "_") + ".html")).write_text(
            html, encoding="utf-8"
        )
        for L in re.findall(r'href="(/cgi-bin/luci/admin[^"]+)"', html):
            found_links.add(L.split("?")[0])

    print("\nunique admin links from crawled pages:")
    for L in sorted(found_links):
        print(" ", L)

    # process list may show remote-daemon path
    st, html = get(op, args.host, "/cgi-bin/luci/admin/system/processes")
    if st == 200:
        for line in html.splitlines():
            if re.search(r"remote|btminer|dropbear|sshd|8889", line, re.I):
                print("PROC:", line.strip()[:160])

    # try luci file browser query styles used by some firmwares
    fb_candidates = [
        "/cgi-bin/luci/admin/system/filebrowser?path=/usr/bin",
        "/cgi-bin/luci/admin/system/filebrowser?path=/usr/bin/remote-daemon",
        "/cgi-bin/luci/admin/system/filebrowser/download?path=/usr/bin/remote-daemon",
        "/cgi-bin/cgi-backup",  # unlikely
    ]
    for path in fb_candidates:
        st, html = get(op, args.host, path)
        print(f"FB {st} {path} len={len(html)}")
        if st == 200 and ("remote-daemon" in html or len(html) > 10000):
            print("  interesting!")

    print(f"\nHTML dumps in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
