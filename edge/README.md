# poolheat edge (Go)

Low-RAM edge workers for Keenetic Entware.

## Binaries

| Binary | Replaces | Uses |
|--------|----------|------|
| `poolheat-devices-poller` | `serve.py --devices-poller` | Tapo / Tuya / Shelly / … |
| `poolheat-miner-poller` | `serve.py --miner-poller` | **wm-lib** (`github.com/andreybmc/wm-lib`) public API :4028 |

### miner-poller

- Polls summary / status / devs / get_psu via wm-lib
- Writes `live_cache.json` + `mining_work.json`
- History samples + chipmap stay in `serve.py` (read cache, no :4028)
- Target RSS: ~5–15 MiB (vs ~90 MiB Python)

### Build (host → arm64 static)

```bash
cd edge
# wm-lib via replace in go.mod → /Users/…/projects/mining/wm-lib
make build-arm64
# → packaging/entware/opt/bin/poolheat-{devices,miner}-poller
```

### Run

```bash
POOLHEAT_DATA=/opt/var/poolheat \
POOLHEAT_MINER_HOST=192.168.1.10 \
  /opt/bin/poolheat-miner-poller
```

`serve.py` auto-prefers Go binaries when present; Python fallback otherwise.
