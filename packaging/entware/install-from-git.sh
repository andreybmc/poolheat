#!/bin/sh
# Install poolheat onto Keenetic Entware from a git checkout.
# Run on the router:
#   cd /opt/poolheat && sh packaging/entware/install-from-git.sh
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "poolheat root: $ROOT"

mkdir -p /opt/bin /opt/lib/poolheat /opt/share/poolheat/www \
         /opt/etc/poolheat /opt/var/poolheat /opt/var/run \
         /opt/etc/init.d

# app files
cp -f "$ROOT/ui-demo/serve.py" /opt/lib/poolheat/serve.py
# model catalog (manufacturer · cooling · chip layout)
if [ -f "$ROOT/ui-demo/miner_models.py" ]; then
  cp -f "$ROOT/ui-demo/miner_models.py" /opt/lib/poolheat/miner_models.py
fi
if [ -f "$ROOT/ui-demo/chipmap_skus.json" ]; then
  cp -f "$ROOT/ui-demo/chipmap_skus.json" /opt/lib/poolheat/chipmap_skus.json
fi
# Tuya / Smart Life mobile API (local_key fetch) — must use POST (CloudFront blocks long GET)
if [ -f "$ROOT/ui-demo/tuya_mobile.py" ]; then
  cp -f "$ROOT/ui-demo/tuya_mobile.py" /opt/lib/poolheat/tuya_mobile.py
fi
# Xiaomi / Mi Home miIO LAN client (UDP 54321 · token)
if [ -f "$ROOT/ui-demo/xiaomi_miio.py" ]; then
  cp -f "$ROOT/ui-demo/xiaomi_miio.py" /opt/lib/poolheat/xiaomi_miio.py
fi
# whatsminer-lib (vendored package)
if [ -d "$ROOT/ui-demo/whatsminer" ]; then
  rm -rf /opt/lib/poolheat/whatsminer
  cp -a "$ROOT/ui-demo/whatsminer" /opt/lib/poolheat/whatsminer
fi
# remove legacy monolith if present
rm -f /opt/lib/poolheat/whatsminer_driver.py
cp -f "$ROOT/ui-demo/index.html" /opt/share/poolheat/www/index.html
if [ -d "$ROOT/ui-demo/icons" ]; then
  rm -rf /opt/share/poolheat/www/icons
  cp -a "$ROOT/ui-demo/icons" /opt/share/poolheat/www/icons
fi
if [ -f "$ROOT/VERSION" ]; then
  cp -f "$ROOT/VERSION" /opt/lib/poolheat/VERSION
  cp -f "$ROOT/VERSION" /opt/share/poolheat/VERSION
fi
chmod 755 /opt/lib/poolheat/serve.py

# launcher
cp -f "$ROOT/packaging/entware/opt/bin/poolheatd" /opt/bin/poolheatd
chmod 755 /opt/bin/poolheatd

# init
cp -f "$ROOT/packaging/entware/opt/etc/init.d/S99poolheat" /opt/etc/init.d/S99poolheat
cp -f "$ROOT/packaging/entware/opt/etc/init.d/S99poolheat-standalone" /opt/etc/init.d/S99poolheat-standalone
chmod 755 /opt/etc/init.d/S99poolheat /opt/etc/init.d/S99poolheat-standalone

# config (do not overwrite existing)
if [ ! -f /opt/etc/poolheat/config.json ]; then
  cp -f "$ROOT/packaging/entware/opt/etc/poolheat/config.json" /opt/etc/poolheat/config.json
  echo "created /opt/etc/poolheat/config.json — edit miner_host if needed"
else
  echo "keep existing /opt/etc/poolheat/config.json"
fi

# deps
if ! command -v python3 >/dev/null 2>&1 && [ ! -x /opt/bin/python3 ]; then
  echo "Install python3 first: opkg install python3 python3-pip"
  exit 1
fi

if [ -x /opt/bin/pip3 ] || [ -x /opt/bin/pip ]; then
  PIP=/opt/bin/pip3
  [ -x "$PIP" ] || PIP=/opt/bin/pip
  echo "Installing Python deps (pycryptodome passlib)..."
  "$PIP" install pycryptodome passlib || true
fi

# restart
if [ -f /opt/etc/init.d/rc.func ]; then
  /opt/etc/init.d/S99poolheat stop 2>/dev/null || true
  /opt/etc/init.d/S99poolheat start 2>/dev/null || \
    /opt/etc/init.d/S99poolheat-standalone restart
else
  /opt/etc/init.d/S99poolheat-standalone restart
fi

echo ""
echo "OK. Open: http://<keenetic-ip>:8787/"
echo "Config:  /opt/etc/poolheat/config.json"
echo "Log:     /opt/var/poolheat/poolheat.log"
echo "Update:  cd $ROOT && git pull && sh packaging/entware/install-from-git.sh"
if [ -L /opt/lib/poolheat ]; then
  echo "USB:     /opt/lib/poolheat -> $(readlink /opt/lib/poolheat)"
  echo "         (install writes through symlink onto USB — OK)"
else
  echo "Flash:   package on internal /opt (to move: sh packaging/entware/migrate-to-usb.sh)"
fi
echo ""
