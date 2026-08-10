# poolheat edge (Go)

Low-RAM edge workers for Keenetic Entware.

## `poolheat-devices-poller`

Drop-in replacement for `python serve.py --devices-poller`.

- Reads / writes the same JSON files under `POOLHEAT_DATA` (`/opt/var/poolheat`)
- Backends: **tapo** (KLAP), **tuya** (LAN 3.1/3.4), shelly, ewelink, webhook, homeassistant
- Target RSS: ~5–15 MiB (vs ~90 MiB Python)

### Build (host → arm64 static)

```bash
cd edge
make build-arm64          # → packaging/entware/opt/bin/poolheat-devices-poller
```

### Run

```bash
POOLHEAT_DATA=/opt/var/poolheat /opt/bin/poolheat-devices-poller
```

`serve.py` auto-prefers this binary when present; falls back to Python otherwise.
