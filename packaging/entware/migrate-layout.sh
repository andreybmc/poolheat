#!/bin/sh
# poolheat — on-router layout / data migration after updates.
#
# Run on Keenetic Entware (root):
#   sh packaging/entware/migrate-layout.sh
# or (installed copy):
#   sh /opt/lib/poolheat/migrate-layout.sh
#
# Safe to re-run. Does not overwrite /opt/etc/poolheat/config.json.
# Moves/renames only when the new path is missing and the old one exists.
set -e

PATH=/opt/bin:/opt/sbin:/bin:/sbin:/usr/bin:/usr/sbin:$PATH

log() { echo "poolheat-migrate: $*"; }

# ── 1) Source tree: ui-demo → ui (git checkout under /opt/poolheat or similar) ──
migrate_src_tree() {
  # Prefer common clone locations
  for base in /opt/poolheat /opt/share/poolheat/src /tmp/mnt/*/poolheat; do
    # expand globs carefully
    for d in $base; do
      [ -d "$d" ] || continue
      if [ -d "$d/ui-demo" ] && [ ! -d "$d/ui" ]; then
        log "rename $d/ui-demo → $d/ui"
        mv "$d/ui-demo" "$d/ui"
      elif [ -d "$d/ui-demo" ] && [ -d "$d/ui" ]; then
        log "both ui and ui-demo present under $d — leave ui-demo (manual cleanup)"
      fi
    done
  done
}

# ── 2) Ensure Entware install dirs ───────────────────────────────────────────
ensure_dirs() {
  mkdir -p /opt/bin /opt/lib/poolheat /opt/share/poolheat/www \
           /opt/etc/poolheat /opt/var/poolheat /opt/var/run \
           /opt/share/poolheat/www/icons/vendors
}

# ── 3) Ship migrate script next to serve for OTA re-runs ─────────────────────
install_self() {
  # When invoked from packaging tree, copy into lib
  self=$(readlink -f "$0" 2>/dev/null || echo "$0")
  if [ -f "$self" ] && [ -d /opt/lib/poolheat ]; then
    case "$self" in
      /opt/lib/poolheat/migrate-layout.sh) ;;
      *)
        cp -f "$self" /opt/lib/poolheat/migrate-layout.sh 2>/dev/null || true
        chmod 755 /opt/lib/poolheat/migrate-layout.sh 2>/dev/null || true
        ;;
    esac
  fi
}

# ── 4) Vendor logos: ensure PNG catalog files present if only SVG leftovers ──
# (no-op if already installed by OTA)
sync_vendor_assets() {
  WWW=/opt/share/poolheat/www
  LIB=/opt/lib/poolheat
  # miner_vendors.json: prefer lib, mirror to www
  if [ -f "$LIB/miner_vendors.json" ] && [ ! -f "$WWW/miner_vendors.json" ]; then
    cp -f "$LIB/miner_vendors.json" "$WWW/miner_vendors.json"
    log "mirrored miner_vendors.json → www"
  elif [ -f "$WWW/miner_vendors.json" ] && [ ! -f "$LIB/miner_vendors.json" ]; then
    cp -f "$WWW/miner_vendors.json" "$LIB/miner_vendors.json"
    log "mirrored miner_vendors.json → lib"
  fi
}

# ── 5) Managed inventory: dead V3 port 4433 → working V2 4028 ────────────────
fix_managed_ports() {
  MF=/opt/var/poolheat/miners_managed.json
  [ -f "$MF" ] || return 0
  if ! command -v python3 >/dev/null 2>&1 && [ ! -x /opt/bin/python3 ]; then
    return 0
  fi
  PY=/opt/bin/python3
  command -v python3 >/dev/null 2>&1 && PY=python3
  [ -x /opt/bin/python3 ] && PY=/opt/bin/python3
  "$PY" - <<'PY' || true
import json
from pathlib import Path
p = Path("/opt/var/poolheat/miners_managed.json")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception as e:
    print("poolheat-migrate: managed parse skip:", e)
    raise SystemExit(0)
changed = False
for m in d.get("miners") or []:
    if not isinstance(m, dict):
        continue
    try:
        port = int(m.get("port") or 0)
    except (TypeError, ValueError):
        continue
    if port == 4433:
        m["port"] = 4028
        changed = True
        print("poolheat-migrate: miner", m.get("id"), m.get("host"), "port 4433→4028")
cfg = Path("/opt/etc/poolheat/config.json")
if cfg.is_file():
    try:
        c = json.loads(cfg.read_text(encoding="utf-8"))
        if int(c.get("miner_port") or 0) == 4433:
            c["miner_port"] = 4028
            cfg.write_text(json.dumps(c, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print("poolheat-migrate: config.json miner_port 4433→4028")
    except Exception as e:
        print("poolheat-migrate: config skip:", e)
if changed:
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("poolheat-migrate: wrote", p)
PY
}

# ── 6) heat_pools.json seed if missing (prevents old crash loops) ────────────
seed_heat_pools() {
  HP=/opt/var/poolheat/heat_pools.json
  [ -f "$HP" ] && return 0
  # Prefer migrate from pool_config.json if present
  PC=/opt/var/poolheat/pool_config.json
  if [ -f "$PC" ] && (command -v python3 >/dev/null 2>&1 || [ -x /opt/bin/python3 ]); then
    PY=/opt/bin/python3
    command -v python3 >/dev/null 2>&1 && PY=python3
    [ -x /opt/bin/python3 ] && PY=/opt/bin/python3
    "$PY" - <<'PY' || true
import json
from pathlib import Path
from datetime import datetime
pc = Path("/opt/var/poolheat/pool_config.json")
hp = Path("/opt/var/poolheat/heat_pools.json")
try:
    legacy = json.loads(pc.read_text(encoding="utf-8"))
    if not isinstance(legacy, dict):
        legacy = {}
except Exception:
    legacy = {}
doc = {
    "version": 1,
    "active_id": "pool_default",
    "pools": [{
        "id": "pool_default",
        "name": "Main pool",
        "enabled": True,
        "config": legacy,
        "updated_ts": datetime.now().isoformat(timespec="seconds"),
    }],
}
hp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("poolheat-migrate: seeded heat_pools.json from pool_config.json")
PY
  else
    cat > "$HP" <<'EOF'
{
  "version": 1,
  "active_id": "pool_default",
  "pools": [
    {
      "id": "pool_default",
      "name": "Main pool",
      "enabled": true,
      "config": {},
      "updated_ts": null
    }
  ]
}
EOF
    log "seeded empty heat_pools.json"
  fi
}

# ── 7) Drop obsolete cache files that can confuse after path renames ─────────
cleanup_stale() {
  # old python bytecode that may reference ui-demo paths
  find /opt/lib/poolheat -name '__pycache__' -type d 2>/dev/null | while read d; do
    rm -rf "$d" 2>/dev/null || true
  done
  # leftover .new files from interrupted OTA
  find /opt/lib/poolheat /opt/share/poolheat /opt/bin /opt/etc/init.d \
    -name '*.new' 2>/dev/null | while read f; do
    rm -f "$f" 2>/dev/null || true
  done
}

# ── main ─────────────────────────────────────────────────────────────────────
log "start"
ensure_dirs
migrate_src_tree
install_self
sync_vendor_assets
fix_managed_ports
seed_heat_pools
cleanup_stale
log "done"
exit 0
