# poolheat

Thermal / power controller UI for **Whatsminer M63** (pool heating) with optional deploy on **Keenetic Peak** via **Entware OPKG**.

## Features

- Live temps: liquid, env, PCB boards, chip min/avg/max
- Power Mode / Power Limit / power_pct (write API)
- Zone map thresholds (T_low / T_high / T_crit…)
- History (SQLite) + charts (zoom / brush / scroll)
- Tooltips + toast notifications
- Entware package for Keenetic (`packaging/entware`)

## Quick start (Mac / Linux)

```bash
cd ui-demo
python3 -m pip install pycryptodome passlib
python3 serve.py
```

Open http://127.0.0.1:8787/

Default miner: `192.168.1.10:4028` (edit `config.example.json` → `config.json` or env).

## Deploy to Keenetic Peak from this repo

See **[KEENETIC.md](./KEENETIC.md)**.

Short version (router with Entware + git):

```sh
opkg update
opkg install git git-http python3 python3-pip
cd /opt
git clone https://github.com/andreybmc/poolheat.git
cd poolheat
sh packaging/entware/install-from-git.sh
```

Or build `.ipk` on a PC and `opkg install` the package from `dist/`.

## Layout

```text
ui-demo/                 # serve.py + index.html (dev UI)
packaging/entware/       # OPKG tree + build-ipk.sh + install-from-git.sh
dist/                    # built .ipk (optional)
KEENETIC.md              # router install runbook
design-doc-*.md          # design notes
```

## License

Private / use at your own risk. Third-party Entware software on Keenetic is unsupported by Keenetic.
