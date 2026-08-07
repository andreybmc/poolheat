#!/bin/sh
# Move poolheat install + data from internal flash (/opt UBI) to USB.
# Keeps tiny launchers on flash; /opt/{lib,share,var,etc}/poolheat become symlinks.
#
# Run on Keenetic as root (Entware SSH):
#   sh migrate-to-usb.sh
# Optional: USB=/tmp/mnt/<uuid> sh migrate-to-usb.sh
set -e

PATH=/opt/bin:/opt/sbin:/bin:/sbin:/usr/bin:/usr/sbin:$PATH

pick_usb() {
  if [ -n "$USB" ] && [ -d "$USB" ]; then
    echo "$USB"
    return 0
  fi
  best=""
  bestfree=0
  for d in /tmp/mnt/*/; do
    [ -d "$d" ] || continue
    line=$(df -P "$d" 2>/dev/null | tail -1) || continue
    echo "$line" | grep -q ubi && continue
    echo "$line" | grep -q tmpfs && continue
    free=$(echo "$line" | awk '{print $4}')
    free=${free:-0}
    if [ "$free" -gt "$bestfree" ] 2>/dev/null; then
      bestfree=$free
      best="${d%/}"
    fi
  done
  [ -n "$best" ] || return 1
  echo "$best"
}

USB_MNT=$(pick_usb) || {
  echo "ERROR: no USB under /tmp/mnt (plug in and mount storage in Keenetic UI)"
  exit 1
}
ROOT="${USB_MNT}/poolheat"
echo "USB=$USB_MNT"
echo "ROOT=$ROOT"

stop_svc() {
  if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
    /opt/etc/init.d/S99poolheat-standalone stop 2>/dev/null || true
  fi
  if [ -x /opt/etc/init.d/S99poolheat ]; then
    /opt/etc/init.d/S99poolheat stop 2>/dev/null || true
  fi
  for p in $(ps w 2>/dev/null | grep '[s]erve.py' | awk '{print $1}'); do
    kill "$p" 2>/dev/null || true
  done
  sleep 1
}

copy_tree() {
  src=$1
  dst=$2
  if [ -L "$src" ]; then
    echo "already symlink: $src -> $(readlink "$src")"
    return 0
  fi
  [ -e "$src" ] || {
    echo "skip missing $src"
    return 0
  }
  echo "copy $src -> $dst"
  mkdir -p "$dst"
  if [ -d "$src" ]; then
    cp -a "$src"/. "$dst"/
  else
    cp -a "$src" "$dst"
  fi
}

replace_link() {
  path=$1
  target=$2
  if [ -L "$path" ] || [ -e "$path" ]; then
    rm -rf "$path"
  fi
  ln -s "$target" "$path"
  echo "link $path -> $target"
}

stop_svc
mkdir -p "$ROOT/lib" "$ROOT/share" "$ROOT/var" "$ROOT/etc" "$ROOT/log"

copy_tree /opt/lib/poolheat "$ROOT/lib"
copy_tree /opt/share/poolheat "$ROOT/share"
copy_tree /opt/var/poolheat "$ROOT/var"
copy_tree /opt/etc/poolheat "$ROOT/etc"

[ -f "$ROOT/lib/serve.py" ] || {
  echo "ERROR: serve.py not found after copy"
  exit 1
}

replace_link /opt/lib/poolheat "$ROOT/lib"
replace_link /opt/share/poolheat "$ROOT/share"
replace_link /opt/var/poolheat "$ROOT/var"
replace_link /opt/etc/poolheat "$ROOT/etc"

echo "$ROOT" >"$ROOT/etc/USB_ROOT"
echo "$USB_MNT" >"$ROOT/etc/USB_MOUNT"
date >"$ROOT/etc/USB_MIGRATED" 2>/dev/null || true

# Keep packaged poolheatd (USB wait + auto-respawn). Do not overwrite with a bare exec.
if [ -x /opt/bin/poolheatd ]; then
  chmod 755 /opt/bin/poolheatd
fi

echo ""
echo "=== done ==="
df -h /opt "$USB_MNT" | sed 's/^/  /'
du -sh "$ROOT" | sed 's/^/  poolheat on USB: /'
ls -la /opt/lib/poolheat /opt/var/poolheat /opt/etc/poolheat 2>/dev/null | sed 's/^/  /'

if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
  /opt/etc/init.d/S99poolheat-standalone start || true
elif [ -x /opt/etc/init.d/S99poolheat ]; then
  /opt/etc/init.d/S99poolheat start || true
fi

echo ""
echo "Open http://$(hostname -i 2>/dev/null || echo '<router-ip>'):8787/"
echo "Data/logs now on USB: $ROOT"
echo "Do not remove the USB stick while poolheat is running."
