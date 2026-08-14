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
- History samples may stay in `serve.py` (read `live_cache.json` only)
- Chipmap is written by miner-poller to `chipmap_cache.json` (serve/UI read-only)
- Target RSS: ~5–15 MiB (vs ~90 MiB Python)

### Build (host → arm64 static)

```bash
cd edge
# wm-lib via replace in go.mod → /Users/…/projects/mining/wm-lib
# Always build BOTH arches for a release:
make build-all-arch
# Or single arch:
make build-arm64    # Peak aarch64
make build-mipsel   # Giant mipsel softfloat

# Full dual-arch packages + optional GitHub publish:
#   sh packaging/entware/release.sh
#   sh packaging/entware/release.sh --publish
# → packaging/entware/opt/bin/poolheat-{devices,miner}-poller
```

### Run

```bash
POOLHEAT_DATA=/opt/var/poolheat \
POOLHEAT_MINER_HOST=192.168.1.10 \
  /opt/bin/poolheat-miner-poller
```

`serve.py` auto-prefers Go binaries when present; Python fallback otherwise.
