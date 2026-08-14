#!/bin/sh
# Build Entware .ipk for Keenetic (arch-specific).
#
# Usage:
#   sh packaging/entware/build-ipk.sh                  # aarch64-3.10 (default)
#   sh packaging/entware/build-ipk.sh aarch64-3.10
#   sh packaging/entware/build-ipk.sh mipselsf-3.4
#   sh packaging/entware/build-ipk.sh all              # both arches
#
# Poller binaries must already exist:
#   dist/bin/poolheat-*-poller-linux-arm64   (make -C edge build-arm64)
#   dist/bin/poolheat-*-poller-linux-mipsle  (make -C edge build-mipsel)
# or packaging/entware/opt/bin/ for the target arch (staged by make stage-*).
#
# Also writes:
#   dist/poolheat-${VERSION}-opt-${ARCH_TAG}.tar.gz
#   dist/poolheat-${VERSION}-opt.tar.gz  (same as last built arch — legacy name)
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
OUT_DIR="$REPO/dist"
CONTROL_IN="$ROOT/CONTROL/control"
OPT_DIR="$ROOT/opt"
VERSION_FILE="$REPO/VERSION"
CORE_VER=$(cat "$VERSION_FILE" 2>/dev/null | head -1 | tr -d ' \r' || echo "0.0.0")
PKG_VER=$(grep '^Version:' "$CONTROL_IN" | awk '{print $2}')
[ -n "$PKG_VER" ] || PKG_VER="${CORE_VER}-1"

# Default is BOTH arches — never ship a single-arch release by accident.
TARGET="${1:-all}"

build_one() {
  ARCH="$1"
  case "$ARCH" in
    aarch64-3.10|aarch64)
      ARCH="aarch64-3.10"
      BIN_DEVICES="$REPO/dist/bin/poolheat-devices-poller-linux-arm64"
      BIN_MINER="$REPO/dist/bin/poolheat-miner-poller-linux-arm64"
      ARCH_TAG="aarch64-3.10"
      ELF_EXPECT=183
      ;;
    mipselsf-3.4|mipsel|mipselsf|mips)
      ARCH="mipselsf-3.4"
      BIN_DEVICES="$REPO/dist/bin/poolheat-devices-poller-linux-mipsle"
      BIN_MINER="$REPO/dist/bin/poolheat-miner-poller-linux-mipsle"
      ARCH_TAG="mipselsf-3.4"
      ELF_EXPECT=8
      ;;
    *)
      echo "Unknown arch: $ARCH" >&2
      echo "Use: aarch64-3.10 | mipselsf-3.4 | all" >&2
      exit 1
      ;;
  esac

  # fallback to staged packaging/opt/bin if dist/bin missing
  if [ ! -f "$BIN_DEVICES" ] && [ -f "$OPT_DIR/bin/poolheat-devices-poller" ]; then
    BIN_DEVICES="$OPT_DIR/bin/poolheat-devices-poller"
  fi
  if [ ! -f "$BIN_MINER" ] && [ -f "$OPT_DIR/bin/poolheat-miner-poller" ]; then
    BIN_MINER="$OPT_DIR/bin/poolheat-miner-poller"
  fi
  if [ ! -f "$BIN_DEVICES" ] || [ ! -f "$BIN_MINER" ]; then
    echo "Missing poller binaries for $ARCH" >&2
    echo "  expected: $BIN_DEVICES" >&2
    echo "  expected: $BIN_MINER" >&2
    echo "Build first: make -C edge build-arm64  OR  make -C edge build-mipsel" >&2
    exit 1
  fi

  # verify ELF machine (portable python)
  python3 - "$BIN_DEVICES" "$BIN_MINER" "$ELF_EXPECT" <<'PY'
import struct, sys
expect = int(sys.argv[3])
for p in sys.argv[1:3]:
    b = open(p, "rb").read(20)
    if b[:4] != b"\x7fELF":
        raise SystemExit(f"not ELF: {p}")
    em = struct.unpack_from("<H", b, 18)[0]
    if em != expect:
        raise SystemExit(f"wrong ELF machine {em} (want {expect}) in {p}")
    print(f"OK {p} machine={em}")
PY

  STAGE=$(mktemp -d "${TMPDIR:-/tmp}/poolheat-ipk.XXXXXX")
  cleanup() { rm -rf "$STAGE"; }
  trap cleanup EXIT

  mkdir -p "$STAGE/CONTROL" "$STAGE/opt/bin" "$STAGE/opt/lib/poolheat" \
           "$STAGE/opt/share/poolheat/www" "$STAGE/opt/etc/poolheat" \
           "$STAGE/opt/etc/init.d" "$STAGE/opt/var/poolheat"

  # control with correct Architecture
  sed "s/^Architecture:.*/Architecture: $ARCH/" "$CONTROL_IN" >"$STAGE/CONTROL/control"
  # keep Version from control file
  if ! grep -q '^Version:' "$STAGE/CONTROL/control"; then
    echo "Version: $PKG_VER" >>"$STAGE/CONTROL/control"
  fi
  for f in postinst postrm prerm; do
    [ -f "$ROOT/CONTROL/$f" ] && cp -f "$ROOT/CONTROL/$f" "$STAGE/CONTROL/$f"
  done

  # binaries (arch-correct)
  cp -f "$BIN_DEVICES" "$STAGE/opt/bin/poolheat-devices-poller"
  cp -f "$BIN_MINER" "$STAGE/opt/bin/poolheat-miner-poller"
  cp -f "$OPT_DIR/bin/poolheatd" "$STAGE/opt/bin/poolheatd" 2>/dev/null || \
    cp -f "$REPO/packaging/entware/opt/bin/poolheatd" "$STAGE/opt/bin/poolheatd"
  chmod 755 "$STAGE/opt/bin/"*

  # init
  cp -f "$OPT_DIR/etc/init.d/S99poolheat" "$STAGE/opt/etc/init.d/" 2>/dev/null || true
  cp -f "$OPT_DIR/etc/init.d/S99poolheat-standalone" "$STAGE/opt/etc/init.d/" 2>/dev/null || true
  chmod 755 "$STAGE/opt/etc/init.d/"* 2>/dev/null || true

  # config example only (do not ship live secrets)
  if [ -f "$OPT_DIR/etc/poolheat/config.example.json" ]; then
    cp -f "$OPT_DIR/etc/poolheat/config.example.json" "$STAGE/opt/etc/poolheat/"
  elif [ -f "$OPT_DIR/etc/poolheat/config.json" ]; then
    cp -f "$OPT_DIR/etc/poolheat/config.json" "$STAGE/opt/etc/poolheat/config.example.json"
  fi

  # app files from ui/ (source of truth)
  UI="$REPO/ui"
  for f in serve.py luci_proxy.py miner_models.py tuya_mobile.py xiaomi_miio.py \
           ewelink_lan.py tuya_lan_ctl.py chipmap_skus.json miner_vendors.json \
           miner_model_profiles.json; do
    [ -f "$UI/$f" ] && cp -f "$UI/$f" "$STAGE/opt/lib/poolheat/"
  done
  [ -f "$REPO/packaging/entware/migrate-layout.sh" ] && \
    cp -f "$REPO/packaging/entware/migrate-layout.sh" "$STAGE/opt/lib/poolheat/"
  if [ -d "$UI/whatsminer" ]; then
    rm -rf "$STAGE/opt/lib/poolheat/whatsminer"
    cp -a "$UI/whatsminer" "$STAGE/opt/lib/poolheat/whatsminer"
  fi
  cp -f "$UI/index.html" "$STAGE/opt/share/poolheat/www/"
  if [ -d "$UI/icons" ]; then
    rm -rf "$STAGE/opt/share/poolheat/www/icons"
    cp -a "$UI/icons" "$STAGE/opt/share/poolheat/www/icons"
  fi
  [ -f "$UI/miner_vendors.json" ] && \
    cp -f "$UI/miner_vendors.json" "$STAGE/opt/share/poolheat/www/"
  echo "$CORE_VER" >"$STAGE/opt/lib/poolheat/VERSION"
  echo "$CORE_VER" >"$STAGE/opt/share/poolheat/VERSION"

  mkdir -p "$OUT_DIR"
  PKG_NAME="poolheat_${PKG_VER}_${ARCH}.ipk"
  python3 - "$STAGE" "$OUT_DIR/$PKG_NAME" "$CORE_VER" "$ARCH_TAG" <<'PY'
import io, sys, tarfile
from pathlib import Path

root = Path(sys.argv[1])
out = Path(sys.argv[2])
core = sys.argv[3]
arch_tag = sys.argv[4]

def make_data_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        tar.add(str(root / "opt"), arcname="opt", recursive=True)
    return buf.getvalue()

def make_control_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for p in sorted((root / "CONTROL").iterdir()):
            tar.add(str(p), arcname=p.name)
    return buf.getvalue()

def ar_member(name, data):
    name_f = (name + " " * 16)[:16].encode("ascii")
    header = (
        name_f
        + b"0".ljust(12)
        + b"0".ljust(6)
        + b"0".ljust(6)
        + b"100644".ljust(8)
        + str(len(data)).encode("ascii").ljust(10)
        + b"`\n"
    )
    assert len(header) == 60
    pad = b"\n" if (len(data) % 2) else b""
    return header + data + pad

data = make_data_tar()
control = make_control_tar()
debian = b"2.0\n"
blob = (
    b"!<arch>\n"
    + ar_member("debian-binary", debian)
    + ar_member("control.tar.gz", control)
    + ar_member("data.tar.gz", data)
)
out.write_bytes(blob)
# arch-specific opt tarball for OTA
tgz_arch = out.parent / ("poolheat-%s-opt-%s.tar.gz" % (core, arch_tag))
tgz_arch.write_bytes(data)
# legacy name (last built) — OTA prefers arch-specific name
tgz = out.parent / ("poolheat-%s-opt.tar.gz" % core)
tgz.write_bytes(data)
print("Built:", out, "(%d bytes)" % out.stat().st_size)
print("Also:", tgz_arch)
print("Also:", tgz)
PY

  trap - EXIT
  rm -rf "$STAGE"
  echo "OK $ARCH → $OUT_DIR/$PKG_NAME"
}

if [ "$TARGET" = "all" ]; then
  build_one aarch64-3.10
  build_one mipselsf-3.4
else
  build_one "$TARGET"
fi
