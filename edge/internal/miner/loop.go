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

	hostLabel := fmt.Sprintf("%s:%d", s.Host, s.Port)
	log.Printf("[miner-poller] go pid=%d data=%s miner=%s interval=%ds",
		os.Getpid(), s.DataDir, hostLabel, s.PollIntervalSec)

	log.Printf("[miner-poller] live loop start (writes via %s · chipmap → %s)",
		writeReqFile, chipmapCacheFile)
	var chipSt ChipmapState
	for {
		if ctx.Err() != nil {
			break
		}
		t0 := time.Now()
		// reload host/interval each tick (config may change via UI)
		s = LoadSettings()
		hostLabel = fmt.Sprintf("%s:%d", s.Host, s.Port)

		// Privileged writes first — serve enqueues miner_write_req.json.
		// Keeps write latency low and serializes ASIC TCP in this process only.
		handledWrite := ProcessPendingWrite(s)

		// Long jobs: firmware flash + export log (NetPacket :8889).
		if ProcessPendingFirmwareFlash(s) {
			handledWrite = true
		}
		if ProcessPendingExportLog(s) {
			handledWrite = true
		}

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
		// After a write, re-poll sooner so UI sees new state; also check writes often.
		if handledWrite {
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
			if ProcessPendingWrite(s) {
				// write mid-wait: refresh live next outer iteration soon
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
