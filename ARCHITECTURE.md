# PoolHeat architecture — edge (router) vs app (cloud)

| Field | Value |
|-------|--------|
| **Status** | Target direction (in progress) |
| **Date** | 2026-08-08 |
| **Today** | All-in-one on Keenetic (pollers + proxy + UI + bot + policy) |
| **Later** | Router = edge agent only; UI + Telegram on external server |

---

## Goal

Move the **control plane and UX** off the router. The Keenetic stays a thin **edge agent** next to the ASIC and LAN devices:

| On router (**edge**) forever | May leave the router (**app** / cloud) |
|------------------------------|----------------------------------------|
| ASIC poller (temps, power, pools, write path) | Web UI (`index.html` + SPA) |
| Devices poller (Tapo / Tuya / Shelly / HA / …) | Telegram bot |
| LuCI / miner web reverse proxy (`:8788`) | Higher-level analytics UI, multi-site |
| Local history samples (optional, lightweight) | Heavy reporting / multi-tenant |
| Authenticated **edge API** for cloud ↔ LAN | Policy orchestration (optional cloud) |

**Now:** UI and bot **still run on the router**. Roles are already named so they can be turned off without a rewrite.

---

## Planes

```
┌─────────────────────────────────────────────────────────────┐
│  APP PLANE (today: same process; later: internet VPS)       │
│  · Web UI  · Telegram bot  · (future multi-site dashboard)  │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTPS / WSS / long-poll
                            │ Edge API + optional token
┌───────────────────────────▼─────────────────────────────────┐
│  EDGE PLANE (Keenetic Entware · LAN)                        │
│  · miner poller + write (4028 / 4433 / 8889 / LuCI)         │
│  · devices poller + actuators                               │
│  · luci_proxy → ASIC web UI                                 │
│  · policy loop (today; may stay edge for low latency)       │
│  · history.db samples (edge or ship upstream later)         │
└───────┬───────────────────────────────┬─────────────────────┘
        │                               │
   ASIC LAN                        plugs / sensors
```

### Edge responsibilities (must stay near the miner)

1. **Miner I/O** — read/write Whatsminer (API + NetPacket + LuCI fallback).  
2. **Devices I/O** — LAN plugs/switches; mining-linked auto on/off.  
3. **LuCI proxy** — browser reaches miner web UI via router, not raw miner port.  
4. **Stable LAN polling** — short intervals without WAN RTT.

### App responsibilities (portable)

1. **UI** — dashboards, zone map, settings screens.  
2. **Bot** — Telegram commands / notifications.  
3. **Optional** multi-site aggregation, user accounts, remote access UX.

Policy (heat zones, dry_run, auto apply) can stay on edge for safety latency **or** move to cloud and only call `POST /api/set`. Default today: **edge_policy = true**.

---

## Runtime roles (`config.json` → `roles`)

| Role | Plane | Default | Meaning |
|------|-------|---------|---------|
| `edge_miner_poller` | edge | on | Live fetch, history collector, chipmap scrape |
| `edge_device_poller` | edge | on | Devices sync with mining + probes |
| `edge_luci_proxy` | edge | on | Start/manage LuCI reverse proxy module |
| `edge_policy` | edge | on | Zone / safety policy loop |
| `edge_history` | edge | on | SQLite history writer |
| `app_ui` | app | on | Serve `index.html` SPA |
| `app_bot` | app | on | Telegram long-poll loop |

**Deployment presets** (`roles.deployment`):

| Value | Effect |
|-------|--------|
| `all-in-one` | Everything on (default, current Peak install) |
| `edge` | Pollers + proxy + API; **UI and bot off** (cloud UI later) |
| `app` | UI/bot oriented (expects remote edge API — future) |

Env override: `POOLHEAT_DEPLOYMENT=edge` or per-role `POOLHEAT_ROLE_APP_UI=0`.

---

## API surface (stable contract)

### Edge-facing (router must keep these)

- `GET /api/live`, `/api/status` — snapshot  
- `POST /api/set` — writes (mode / limit / suspend / …)  
- `GET/POST /api/miner/*` — pools, write-status, identity  
- `GET/POST /api/devices/*` — actuators  
- `GET/POST /api/policy/*`, zone map when policy is edge  
- LuCI proxy port (default **8788**), not the public WAN  

### App-facing (move with UI/bot)

- Static `/`, `/index.html`, SPA routes  
- Telegram (outbound to `api.telegram.org`)  
- OTA UX, backup UI, energy charts presentation  

Cloud later authenticates to edge with a **shared token** (TBD: `edge_token` in config). Do **not** expose `:8787` raw on WAN.

---

## Process layout (evolution)

| Phase | Process |
|-------|---------|
| **0 (now)** | Single `serve.py` / `poolheatd` · all roles on |
| **1** | Same binary · `deployment=edge` on router · app on VPS talking to edge API |
| **2** | Split packages: `poolheat-edge` (light) + `poolheat-app` (UI/bot) |
| **3** | Optional: policy only on app; edge is pure I/O + proxy |

Code today is still monolithic (`serve.py` ~25k LOC). New work should:

- Prefer **clear role gates** at thread start / static serve  
- Avoid putting new **cloud-only** logic into miner write path  
- Keep **whatsminer/**, **luci_proxy**, device backends as edge libraries  

---

## Data residency

| Data | Today | Edge-only later |
|------|-------|-----------------|
| `history.db` | router USB | router or push series upstream |
| `devices_config.json` | router | router (secrets stay LAN) |
| `telegram_config.json` | router | **app** server |
| Zone map / presets | router | app (sync down) or edge |
| Miner passwords | router | **edge only** |

---

## Security notes

- Edge API is LAN-trusted today; before internet app→edge, add **token + TLS** (or tunnel: Tailscale / Keenetic remote, not open 8787).  
- LuCI proxy remains LAN or authenticated tunnel only.  
- Device local keys never leave edge in plain form to public UI without auth.

---

## Related files

- Roles implementation: `ui-demo/serve.py` (`DEFAULT_ROLES`, `get_roles()`, `main()` gates)  
- Config example: `ui-demo/config.example.json`  
- LuCI proxy: `ui-demo/luci_proxy.py`  
- Miner stack: `ui-demo/whatsminer/`  
- Operator install: `KEENETIC.md`, `README.md`  
