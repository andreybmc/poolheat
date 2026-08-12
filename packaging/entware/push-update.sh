#!/bin/sh
# Push poolheat update from this Mac/dev host to a Keenetic Entware router.
#
# Usage:
#   sh packaging/entware/push-update.sh
#   KEENETIC_HOST=192.168.1.1 KEENETIC_PASS=keenetic sh packaging/entware/push-update.sh
#   KEENETIC_SSH_PORT=222 KEENETIC_USER=root sh packaging/entware/push-update.sh
#
# Steps on router:
#   1) stop service
#   2) scp serve/UI/icons/pollers/init + migrate-layout.sh
#   3) run migrate-layout.sh (moves ui-demo→ui if needed, fixes ports, seeds heat_pools)
#   4) start service
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="${KEENETIC_HOST:-192.168.1.1}"
PORT="${KEENETIC_SSH_PORT:-222}"
USER="${KEENETIC_USER:-root}"
PASS="${KEENETIC_PASS:-keenetic}"

SSH_BASE="ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no -o ConnectTimeout=15 -p $PORT"
SCP_BASE="scp -O -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no -o ConnectTimeout=15 -P $PORT"

if command -v sshpass >/dev/null 2>&1 && [ -n "$PASS" ]; then
  export SSHPASS="$PASS"
  SSH="sshpass -e $SSH_BASE"
  SCP="sshpass -e $SCP_BASE"
else
  SSH="$SSH_BASE"
  SCP="$SCP_BASE"
fi

REMOTE="${USER}@${HOST}"
echo "poolheat push → $REMOTE (port $PORT)"
echo "  from $ROOT"

$SSH "$REMOTE" 'export PATH=/opt/bin:/opt/sbin:$PATH
mkdir -p /opt/lib/poolheat /opt/share/poolheat/www/icons/vendors /opt/var/poolheat /opt/etc/poolheat /opt/bin /opt/etc/init.d
if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
  /opt/etc/init.d/S99poolheat-standalone stop 2>/dev/null || true
elif [ -x /opt/etc/init.d/S99poolheat ]; then
  /opt/etc/init.d/S99poolheat stop 2>/dev/null || true
fi
'

# Core Python + UI
$SCP \
  "$ROOT/ui/serve.py" \
  "$ROOT/ui/luci_proxy.py" \
  "$ROOT/ui/miner_models.py" \
  "$ROOT/ui/miner_vendors.json" \
  "$ROOT/ui/chipmap_skus.json" \
  "$ROOT/ui/tuya_mobile.py" \
  "$ROOT/ui/xiaomi_miio.py" \
  "$ROOT/VERSION" \
  "$ROOT/packaging/entware/migrate-layout.sh" \
  "$REMOTE:/opt/lib/poolheat/"

$SCP \
  "$ROOT/ui/index.html" \
  "$ROOT/ui/miner_vendors.json" \
  "$REMOTE:/opt/share/poolheat/www/"

$SCP \
  "$ROOT/VERSION" \
  "$REMOTE:/opt/share/poolheat/VERSION"

# Icons (full tree)
$SSH "$REMOTE" 'rm -rf /opt/share/poolheat/www/icons'
$SCP -r "$ROOT/ui/icons" "$REMOTE:/opt/share/poolheat/www/icons"

# whatsminer package
$SSH "$REMOTE" 'rm -rf /opt/lib/poolheat/whatsminer'
$SCP -r "$ROOT/ui/whatsminer" "$REMOTE:/opt/lib/poolheat/whatsminer"

# Binaries + init
$SCP \
  "$ROOT/packaging/entware/opt/bin/poolheatd" \
  "$ROOT/packaging/entware/opt/bin/poolheat-miner-poller" \
  "$ROOT/packaging/entware/opt/bin/poolheat-devices-poller" \
  "$REMOTE:/opt/bin/"

$SCP \
  "$ROOT/packaging/entware/opt/etc/init.d/S99poolheat" \
  "$ROOT/packaging/entware/opt/etc/init.d/S99poolheat-standalone" \
  "$REMOTE:/opt/etc/init.d/"

# migrate + start
$SSH "$REMOTE" 'export PATH=/opt/bin:/opt/sbin:$PATH
chmod 755 /opt/lib/poolheat/serve.py /opt/lib/poolheat/migrate-layout.sh \
  /opt/bin/poolheatd /opt/bin/poolheat-miner-poller /opt/bin/poolheat-devices-poller \
  /opt/etc/init.d/S99poolheat /opt/etc/init.d/S99poolheat-standalone
# strip pycache
find /opt/lib/poolheat -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
sh /opt/lib/poolheat/migrate-layout.sh || true
if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
  /opt/etc/init.d/S99poolheat-standalone start
elif [ -x /opt/etc/init.d/S99poolheat ]; then
  /opt/etc/init.d/S99poolheat start
fi
sleep 2
ps w | grep -E "poolheat|serve.py" | grep -v grep || true
cat /opt/lib/poolheat/VERSION 2>/dev/null
echo OK
'

echo ""
echo "Pushed. UI: http://$HOST:8787/"
echo "Version: $(cat "$ROOT/VERSION")"
