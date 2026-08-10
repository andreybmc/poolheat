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

	log.Printf("[miner-poller] live loop start")
	for {
		if ctx.Err() != nil {
			break
		}
		t0 := time.Now()
		// reload host/interval each tick (config may change via UI)
		s = LoadSettings()
		hostLabel = fmt.Sprintf("%s:%d", s.Host, s.Port)

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
				log.Printf("[miner-poller] ok power=%v th=%.1f work=%s",
					live["power"], asF(live["hashrate_th"]), work)
			}
		}

		interval := time.Duration(s.PollIntervalSec) * time.Second
		if interval < 2*time.Second {
			interval = 2 * time.Second
		}
		spent := time.Since(t0)
		wait := interval - spent
		if wait < time.Second {
			wait = time.Second
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
		if ctx.Err() != nil {
			break
		}
	}
	log.Printf("[miner-poller] live loop stop")
	return nil
}
