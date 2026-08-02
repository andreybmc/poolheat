#!/bin/sh
# Build Entware .ipk for Keenetic Peak (aarch64-3.10)
# Works on macOS (native ar is broken for non-mach-o members).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$(cd "$ROOT/../.." && pwd)/dist"
VERSION=$(grep '^Version:' "$ROOT/CONTROL/control" | awk '{print $2}')
ARCH=$(grep '^Architecture:' "$ROOT/CONTROL/control" | awk '{print $2}')
PKG_NAME="poolheat_${VERSION}_${ARCH}.ipk"
mkdir -p "$OUT_DIR"

python3 - "$ROOT" "$OUT_DIR/$PKG_NAME" <<'PY'
import io, sys, tarfile
from pathlib import Path

root = Path(sys.argv[1])
out = Path(sys.argv[2])

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
# also opt tarball for manual install
(out.parent / "poolheat-0.1.0-opt.tar.gz").write_bytes(data)
print("Built:", out, "(%d bytes)" % out.stat().st_size)
print("Also:", out.parent / "poolheat-0.1.0-opt.tar.gz")
PY
