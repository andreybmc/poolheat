# poolheat_WM

Thermal / power controller for **Whatsminer** (pool heating) with Web UI.  
Runs on a PC (dev) or **Keenetic Peak / Entware** router next to the miner.

**Version:** `0.3.10` · **Repo:** https://github.com/andreybmc/poolheat

## Features

- Live temps: liquid, env, PCB boards, chip min/avg/max
- Zone map Z0–Z3 + Safety Critical (chip)
- Power Mode / Power Limit / Suspend–Resume
- History (SQLite) + charts
- Dry Run, Override, warmup / anti-thrash policy
- **Check & install updates from GitHub** (Info tab)

---

## Install on Keenetic router (from GitHub)

Full runbook: **[KEENETIC.md](./KEENETIC.md)**.

### Requirements

1. Keenetic with **OPKG / Entware** (Peak aarch64, USB or internal storage)
2. Router can reach the internet (for `opkg` + `git clone` / updates)
3. LAN access to miner API (`:4028`, default `192.168.1.10`)

### First install

SSH into the router (Entware shell), then:

```sh
opkg update
opkg install git git-http ca-certificates python3 python3-pip

cd /opt
git clone https://github.com/andreybmc/poolheat.git
cd poolheat
sh packaging/entware/install-from-git.sh
```

Open UI:

```text
http://<router-ip>:8787/
```

Edit miner address if needed:

```sh
vi /opt/etc/poolheat/config.json
/opt/etc/init.d/S99poolheat-standalone restart
```

Example config:

```json
{
  "bind": "0.0.0.0",
  "http_port": 8787,
  "miner_host": "192.168.1.10",
  "miner_port": 4028,
  "api_password": "admin"
}
```

### Update from GitHub (SSH)

```sh
cd /opt/poolheat
git pull
sh packaging/entware/install-from-git.sh
```

### Update from Web UI (no SSH)

1. Open **Инфо** (Info) tab
2. **Проверить обновления** — compares local `VERSION` with GitHub (`releases` / `tags` / `VERSION` on `main`)
3. If update is available → **Установить** (downloads archive from GitHub and restarts the service)

Config under `/opt/etc/poolheat/` is **not** overwritten.

### Paths on the router

| Path | Purpose |
|------|---------|
| `/opt/lib/poolheat/serve.py` | backend |
| `/opt/share/poolheat/www/index.html` | UI |
| `/opt/lib/poolheat/VERSION` | installed version |
| `/opt/etc/poolheat/config.json` | miner / bind / password |
| `/opt/var/poolheat/` | history.db, logs, zone config |
| `:8787` | Web UI (LAN) |

---

## Dev on Mac / Linux

```bash
cd ui-demo
python3 -m pip install pycryptodome passlib
python3 serve.py
```

Open http://127.0.0.1:8787/

## Layout

```text
ui-demo/                 # serve.py + index.html (source of truth)
packaging/entware/       # OPKG tree + install-from-git.sh + build-ipk.sh
VERSION                  # release version (used by update check)
KEENETIC.md              # detailed Keenetic install runbook
dist/                    # optional built .ipk
```

## License

Private / use at your own risk. Third-party Entware software on Keenetic is unsupported by Keenetic.
