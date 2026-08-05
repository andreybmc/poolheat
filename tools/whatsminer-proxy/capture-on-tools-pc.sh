#!/bin/sh
# Capture :8889 on the machine that runs WhatsMinerTool (Mac).
# Peak br0 often misses LAN↔LAN (hardware switch) — client-side capture is reliable.
#
# Usage:
#   sudo sh capture-on-tools-pc.sh
#   # WhatsMinerTool → 192.168.1.10 → connect → ONE Suspend
#   # Ctrl+C when done
#
# Then:
#   python3 analyze_8889_stream.py /tmp/wmt8889-client.pcap --out-dir pcap/stream-out

set -e
MINER="${MINER:-192.168.1.10}"
OUT="${OUT:-/tmp/wmt8889-client.pcap}"

# pick iface that reaches miner
IFACE="${IFACE:-}"
if [ -z "$IFACE" ]; then
  IFACE=$(route -n get "$MINER" 2>/dev/null | awk '/interface:/{print $2; exit}')
fi
IFACE="${IFACE:-en0}"

echo "Capturing on iface=$IFACE miner=$MINER → $OUT"
echo "1) Start WhatsMinerTool → $MINER"
echo "2) Connect / refresh"
echo "3) ONE write: Remote Ctrl → Mining Control → Suspend"
echo "4) Wait 3s, Ctrl+C"
echo ""

rm -f "$OUT"
tcpdump -i "$IFACE" -s 0 -U -w "$OUT" "host ${MINER} and tcp port 8889"
echo ""
ls -la "$OUT"
echo "Analyze:"
echo "  python3 $(dirname "$0")/analyze_8889_stream.py $OUT --out-dir $(dirname "$0")/pcap/stream-out"
