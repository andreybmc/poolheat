#!/bin/sh
# Clean single-stream :8889 capture on Keenetic Peak (Entware).
#
# Why: previous capture used «-i any» (LINUX_SLL2 multipath) → each packet
# appeared several times and TCP reassembly was noisy. This script:
#   · binds one bridge iface (default br0 — LAN path to miner)
#   · filters only miner IP + port 8889
#   · optionally pauses poolheat (stops get_token / 4028 spam)
#
# Usage on Peak:
#   sh capture-8889-clean.sh start
#   # → operator: WhatsMinerTool one login + ONE write (e.g. Suspend)
#   sh capture-8889-clean.sh stop
#   sh capture-8889-clean.sh status
#
# Env:
#   MINER=192.168.1.10  IFACE=br0  OUT=/tmp/wmt8889.pcap  PAUSE_POOLHEAT=1

set -e
MINER="${MINER:-192.168.1.10}"
IFACE="${IFACE:-br0}"
OUT="${OUT:-/tmp/wmt8889.pcap}"
PIDF="${PIDF:-/tmp/wmt8889-tcpdump.pid}"
LOGF="${LOGF:-/tmp/wmt8889-tcpdump.log}"
PAUSE_POOLHEAT="${PAUSE_POOLHEAT:-1}"

TCPDUMP="${TCPDUMP:-/opt/bin/tcpdump}"
# snaplen 0 = full frames; -U packet-buffered
FILTER="host ${MINER} and tcp port 8889"

cmd="${1:-}"

_poolheat_stop() {
  if [ "$PAUSE_POOLHEAT" != "1" ]; then return 0; fi
  if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
    /opt/etc/init.d/S99poolheat-standalone stop 2>/dev/null || true
  fi
  # only serve.py — do not killall python3
  for p in $(ps w 2>/dev/null | grep "[s]erve.py" | awk "{print \$1}"); do
    kill "$p" 2>/dev/null || true
  done
  echo "poolheat paused" >>"$LOGF"
}

_poolheat_start() {
  if [ "$PAUSE_POOLHEAT" != "1" ]; then return 0; fi
  if [ -x /opt/etc/init.d/S99poolheat-standalone ]; then
    /opt/etc/init.d/S99poolheat-standalone start 2>/dev/null || true
  fi
  echo "poolheat resumed" >>"$LOGF"
}

case "$cmd" in
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "already running pid=$(cat "$PIDF") out=$OUT"
      exit 0
    fi
    rm -f "$OUT" "$LOGF" "$PIDF"
    _poolheat_stop
    sleep 1
    # single iface — no multipath duplicates
    # shellcheck disable=SC2086
    "$TCPDUMP" -i "$IFACE" -s 0 -U -w "$OUT" $FILTER >"$LOGF" 2>&1 &
    echo $! >"$PIDF"
    sleep 1
    if ! kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "tcpdump failed to start — see $LOGF"
      cat "$LOGF" 2>/dev/null || true
      _poolheat_start
      exit 1
    fi
    echo "CAPTURE STARTED"
    echo "  iface=$IFACE miner=$MINER filter='$FILTER'"
    echo "  out=$OUT pid=$(cat "$PIDF")"
    echo "  poolheat paused=$PAUSE_POOLHEAT"
    echo ""
    echo "Operator sequence (WhatsMinerTool → $MINER, NOT a proxy):"
    echo "  1) Close any old Tools session to this miner"
    echo "  2) Connect / refresh so :8889 auth happens once"
    echo "  3) ONE write only, e.g. Mining Control → Suspend"
    echo "  4) Wait ~3 s, then: sh $0 stop"
    ;;
  stop)
    if [ -f "$PIDF" ]; then
      pid=$(cat "$PIDF")
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$PIDF"
    fi
    # also stray
    killall tcpdump 2>/dev/null || true
    _poolheat_start
    if [ -f "$OUT" ]; then
      sz=$(wc -c <"$OUT" | tr -d ' ')
      echo "CAPTURE STOPPED  $OUT  bytes=$sz"
      "$TCPDUMP" -r "$OUT" -nn 2>/dev/null | wc -l | awk '{print "  frames≈",$1}'
    else
      echo "no pcap at $OUT"
    fi
    ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "running pid=$(cat "$PIDF")"
    else
      echo "not running"
    fi
    if [ -f "$OUT" ]; then
      ls -la "$OUT"
    fi
    tail -5 "$LOGF" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 start|stop|status"
    exit 1
    ;;
esac
