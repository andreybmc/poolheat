#!/bin/sh
# Build a dual-arch poolheat release (ALWAYS aarch64 + mipsel).
#
# Usage:
#   sh packaging/entware/release.sh              # build packages only → dist/
#   sh packaging/entware/release.sh --publish    # build + gh release upload
#   VERSION=0.6.61 sh packaging/entware/release.sh --publish
#
# Produces:
#   dist/poolheat_${VER}-1_aarch64-3.10.ipk
#   dist/poolheat_${VER}-1_mipselsf-3.4.ipk
#   dist/poolheat-${VER}-opt-aarch64-3.10.tar.gz
#   dist/poolheat-${VER}-opt-mipselsf-3.4.tar.gz
#   dist/poolheat-${VER}-opt.tar.gz   (legacy alias = last arch, prefer arch-specific)
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PUBLISH=0
for a in "$@"; do
  case "$a" in
    --publish|-p) PUBLISH=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
  esac
done

VER=$(cat VERSION 2>/dev/null | head -1 | tr -d ' \r\n' || echo "")
if [ -z "$VER" ]; then
  echo "VERSION file missing" >&2
  exit 1
fi

# Keep CONTROL Version in sync (X.Y.Z-1)
CTRL="$ROOT/packaging/entware/CONTROL/control"
if [ -f "$CTRL" ]; then
  # portable sed in-place
  tmp=$(mktemp)
  sed "s/^Version:.*/Version: ${VER}-1/" "$CTRL" >"$tmp"
  mv "$tmp" "$CTRL"
  # Architecture line is rewritten per-ipk by build-ipk.sh
fi

echo "════════════════════════════════════════"
echo " poolheat release $VER — dual arch"
echo "════════════════════════════════════════"

echo ""
echo "→ Go pollers (aarch64 + mipsle softfloat)"
make -C edge build-all-arch

echo ""
echo "→ Entware packages (both arches)"
# default single-arch call is wrong; always force "all"
sh packaging/entware/build-ipk.sh all

echo ""
echo "→ Verify ELF machines inside ipk"
python3 - <<'PY'
import struct, io, tarfile
from pathlib import Path

def check(ipk: Path, want: int):
    data = ipk.read_bytes()
    assert data[:8] == b"!<arch>\n", ipk
    off = 8
    while off + 60 <= len(data):
        hdr = data[off : off + 60]
        off += 60
        name = hdr[0:16].decode("ascii", "replace").strip()
        size = int(hdr[48:58].decode().strip())
        blob = data[off : off + size]
        off += size + (size % 2)
        if not name.startswith("data.tar"):
            continue
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            m = t.getmember("opt/bin/poolheat-devices-poller")
            b = t.extractfile(m).read(20)
            em = struct.unpack_from("<H", b, 18)[0]
            if em != want:
                raise SystemExit(f"FAIL {ipk.name}: ELF machine={em} want={want}")
            print(f"OK  {ipk.name}: devices-poller machine={em}")
        return
    raise SystemExit(f"no data.tar in {ipk}")

dist = Path("dist")
ver = Path("VERSION").read_text().strip().split("-")[0]
for arch, em in (("aarch64-3.10", 183), ("mipselsf-3.4", 8)):
    ipks = sorted(dist.glob(f"poolheat_*_{arch}.ipk"))
    if not ipks:
        raise SystemExit(f"missing ipk for {arch}")
    check(ipks[-1], em)
    tgz = dist / f"poolheat-{ver}-opt-{arch}.tar.gz"
    if not tgz.is_file():
        raise SystemExit(f"missing {tgz.name}")
    print(f"OK  {tgz.name} ({tgz.stat().st_size} bytes)")
print("all packages verified")
PY

echo ""
echo "Artifacts:"
ls -la dist/poolheat_${VER}-1_*.ipk dist/poolheat-${VER}-opt-*.tar.gz 2>/dev/null || \
  ls -la dist/poolheat_*_*.ipk dist/poolheat-*-opt-*.tar.gz

if [ "$PUBLISH" -eq 1 ]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "gh CLI not found — cannot --publish" >&2
    exit 1
  fi
  TAG="$VER"
  echo ""
  echo "→ GitHub release $TAG (create if missing, upload assets)"
  if ! gh release view "$TAG" --repo andreybmc/poolheat >/dev/null 2>&1; then
    gh release create "$TAG" \
      --repo andreybmc/poolheat \
      --title "poolheat $TAG" \
      --notes "Dual-arch release (aarch64-3.10 + mipselsf-3.4).

### Packages
| File | Router |
|------|--------|
| \`poolheat_${VER}-1_aarch64-3.10.ipk\` | Peak / Hero / Titan |
| \`poolheat_${VER}-1_mipselsf-3.4.ipk\` | Giant / Giga / Ultra |
| \`poolheat-${VER}-opt-aarch64-3.10.tar.gz\` | Peak opt tree |
| \`poolheat-${VER}-opt-mipselsf-3.4.tar.gz\` | Giant opt tree |

OTA picks the package matching host arch automatically.
"
  fi
  gh release upload "$TAG" --repo andreybmc/poolheat --clobber \
    dist/poolheat_${VER}-1_aarch64-3.10.ipk \
    dist/poolheat_${VER}-1_mipselsf-3.4.ipk \
    dist/poolheat-${VER}-opt-aarch64-3.10.tar.gz \
    dist/poolheat-${VER}-opt-mipselsf-3.4.tar.gz \
    dist/poolheat-${VER}-opt.tar.gz 2>/dev/null || \
  gh release upload "$TAG" --repo andreybmc/poolheat --clobber \
    dist/poolheat_*_aarch64-3.10.ipk \
    dist/poolheat_*_mipselsf-3.4.ipk \
    dist/poolheat-*-opt-aarch64-3.10.tar.gz \
    dist/poolheat-*-opt-mipselsf-3.4.tar.gz
  echo "Published: https://github.com/andreybmc/poolheat/releases/tag/$TAG"
else
  echo ""
  echo "Build only. To publish: sh packaging/entware/release.sh --publish"
fi

echo "Done."
