package miner

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

// Run is the miner-poller main loop (blocks until signal).
func Run(s Settings) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	pidPath := filepath.Join(s.DataDir, "miner_poller.pid")
	_ = os.WriteFile(pidPath, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o644)
	defer func() { _ = os.Remove(pidPath) }()

	// Seed managed inventory from single-host config if empty.
	EnsureManagedFromSettings(s.DataDir, s)
	ApplyActiveToSettings(s.DataDir, &s)

	hostLabel := fmt.Sprintf("%s:%d", s.Host, s.Port)
	log.Printf("[miner-poller] go pid=%d data=%s miner=%s interval=%ds",
		os.Getpid(), s.DataDir, hostLabel, s.PollIntervalSec)

	log.Printf("[miner-poller] live loop start (writes via %s · chipmap → %s · discovery independent)",
		writeReqFile, chipmapCacheFile)
	var chipSt ChipmapState
	var lastAutoDiscover time.Time
	for {
		if ctx.Err() != nil {
			break
		}
		t0 := time.Now()
		// reload host/interval each tick (config may change via UI)
		s = LoadSettings()
		ApplyActiveToSettings(s.DataDir, &s)
		hostLabel = fmt.Sprintf("%s:%d", s.Host, s.Port)

		// Manual network scan (does not block long-term — runs to completion then continues).
		if ProcessPendingScan(s) {
			// continue to live after scan
		}
		// Scheduled discovery (independent of live poll)
		MaybeAutoDiscovery(s, &lastAutoDiscover)

		// Firmware first (may take minutes) — do not bury under chipmap/live.
		if ProcessPendingFirmwareFlash(s) {
			// after flash, skip chipmap this tick; live may be down while ASIC reboots
			continue
		}
		if ProcessPendingExportLog(s) {
			// short job; continue loop for fresh live
		}

		// Privileged writes — serve enqueues miner_write_req.json.
		handledWrite := ProcessPendingWrite(s)
		// On-demand reads (pools, summary, …) — external UI → poller IPC.
		handledRead := ProcessPendingRead(s)

		// Chipmap: full boards always in chipmap_cache.json (serve/UI read-only).
		ProcessChipmapTick(s, &chipSt)

		live, err := FetchLive(s)
		if err != nil {
			log.Printf("[miner-poller] live: %s %v", time.Now().Format(time.RFC3339), err)
		} else {
			if err := PublishLive(s.DataDir, live, hostLabel, os.Getpid()); err != nil {
				log.Printf("[miner-poller] live-cache: %v", err)
			}
			work := measuredWork(live)
			if work == "sleep" {
				work = "suspend"
			}
			if err := WriteMiningWork(s.DataDir, work, "miner-poller"); err != nil {
				log.Printf("[miner-poller] mining_work: %v", err)
			}
			// light log every ~30s
			if time.Now().Unix()%30 < int64(s.PollIntervalSec) {
				log.Printf("[miner-poller] ok power=%v th=%.1f work=%s liquid=%v",
					live["power"], asF(live["hashrate_th"]), work, live["liquid"])
			}
		}

		interval := time.Duration(s.PollIntervalSec) * time.Second
		if interval < 2*time.Second {
			interval = 2 * time.Second
		}
		// After a write/read, re-poll sooner so UI sees new state; also check IPC often.
		if handledWrite || handledRead {
			interval = 2 * time.Second
		}
		spent := time.Since(t0)
		wait := interval - spent
		if wait < 500*time.Millisecond {
			wait = 500 * time.Millisecond
		}
		// Slice long waits so pending write requests are picked up quickly.
		deadline := time.Now().Add(wait)
		for time.Now().Before(deadline) {
			if ctx.Err() != nil {
				break
			}
			slice := 500 * time.Millisecond
			if rem := time.Until(deadline); rem < slice {
				slice = rem
			}
			if slice < 50*time.Millisecond {
				break
			}
			select {
			case <-ctx.Done():
			case <-time.After(slice):
			}
			if ProcessPendingWrite(s) || ProcessPendingRead(s) {
				// write/read mid-wait: refresh live next outer iteration soon
				break
			}
			if ProcessPendingScan(s) {
				break
			}
			if ProcessPendingFirmwareFlash(s) || ProcessPendingExportLog(s) {
				break
			}
			// On-demand chipmap refresh while waiting
			reqPath := filepath.Join(s.DataDir, chipmapReqFile)
			if _, err := os.Stat(reqPath); err == nil {
				ProcessChipmapTick(s, &chipSt)
			}
		}
		if ctx.Err() != nil {
			break
		}
	}
	log.Printf("[miner-poller] live loop stop")
	return nil
}
