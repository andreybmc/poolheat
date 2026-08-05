#!/usr/bin/env python3
"""Deeper LuCI crawl + try enable dropbear if form exists. Read-mostly."""
from __future__ import annotations

import http.cookiejar
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

HOST = "192.168.1.10"
OUT = Path("/tmp/m63-extract")
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )
    body = urllib.parse.urlencode(
        {"luci_username": "admin", "luci_password": "admin"}
    ).encode()
    req = urllib.request.Request(
        f"https://{HOST}/cgi-bin/luci",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with op.open(req, timeout=15) as r:
        r.read(200)
    print("login ok")

    def get(path: str) -> tuple[int, str]:
        try:
            with op.open(f"https://{HOST}{path}", timeout=20) as r:
                return int(getattr(r, "status", 200) or 200), r.read().decode(
                    "utf-8", "replace"
                )
        except Exception as e:
            return 0, str(e)

    paths = [
        "/cgi-bin/luci/admin/status/processes",
        "/cgi-bin/luci/admin/status/syslog",
        "/cgi-bin/luci/admin/status/minerlog",
        "/cgi-bin/luci/admin/status/btminerstatus",
        "/cgi-bin/luci/admin/status/btminerapi",
        "/cgi-bin/luci/admin/system/system",
        "/cgi-bin/luci/admin/system/admin",
        "/cgi-bin/luci/admin/system/reboot",
    ]
    for path in paths:
        st, html = get(path)
        print(f"\n=== {st} {path} len={len(html)} ===")
        name = path.strip("/").replace("/", "_") + ".html"
        (OUT / name).write_text(html, encoding="utf-8")
        for ln in html.splitlines():
            if re.search(
                r"remote-daemon|dropbear|sshd|/usr/bin|8889|btminer|InterfaceInterface",
                ln,
                re.I,
            ):
                print(" ", ln.strip()[:160])

    st, html = get("/cgi-bin/luci/admin/system/admin")
    print("\n=== admin fields ===")
    for m in re.findall(r'name="([^"]+)"', html):
        print(" name:", m)
    tok = re.search(r'name=["\']token["\']\s+value=["\']([^"\']+)', html)
    print("token", bool(tok))

    # processes page often has command column
    st, html = get("/cgi-bin/luci/admin/status/processes")
    if "remote" in html.lower() or "daemon" in html.lower():
        print("\nprocesses mentions remote/daemon")
    # extract process table rows roughly
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    for row in rows:
        text = re.sub(r"<[^>]+>", " ", row)
        text = re.sub(r"\s+", " ", text).strip()
        if re.search(r"remote|btminer|dropbear|miner", text, re.I):
            print(" ROW:", text[:200])

    print("done ->", OUT)


if __name__ == "__main__":
    main()
