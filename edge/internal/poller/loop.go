package poller

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/andreybmc/poolheat/edge/internal/device"
	"github.com/andreybmc/poolheat/edge/internal/mining"
	"github.com/andreybmc/poolheat/edge/internal/paths"
	"github.com/andreybmc/poolheat/edge/internal/state"
)

// Run is the main devices-poller loop (blocks until signal).
func Run(dataDir string) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// pidfile
	pidPath := paths.PidFile(dataDir)
	_ = os.WriteFile(pidPath, []byte(itoa(os.Getpid())+"\n"), 0o644)
	defer func() { _ = os.Remove(pidPath) }()

	log.Printf("[devices-poller] go pid=%d data=%s", os.Getpid(), dataDir)

	// load deadlines once
	deadlines, syncTS, _ := state.LoadDeadlines(paths.Deadlines(dataDir))
	byID, _ := state.Load(paths.DevicesState(dataDir))
	store := device.NewStore(byID, deadlines, syncTS)

	log.Printf("[devices-poller] loop start")
	for {
		if ctx.Err() != nil {
			break
		}
		t0 := time.Now()
		pol := config.DefaultPoller()
		nDev := 0

		func() {
			defer func() {
				if r := recover(); r != nil {
					log.Printf("[devices-poller] tick panic: %v", r)
				}
			}()

			cfgFile, err := config.Load(paths.DevicesConfig(dataDir))
			if err != nil {
				log.Printf("[devices-poller] load config: %v", err)
				return
			}
			pol = cfgFile.Poller
			nDev = len(cfgFile.Devices)

			// refresh state from disk each tick (API may have written desired)
			if st, err := state.Load(paths.DevicesState(dataDir)); err == nil {
				// merge: keep our online/last_on if newer? Prefer disk for desired, keep last probe if disk empty
				for id, diskRT := range st {
					cur := store.ByID[id]
					// desired always from disk (UI writes)
					if diskRT.DesiredOn != nil {
						cur.DesiredOn = diskRT.DesiredOn
					}
					// if we have no last_on yet, take disk
					if cur.LastOn == nil && diskRT.LastOn != nil {
						cur.LastOn = diskRT.LastOn
					}
					// last_error from disk if we have none
					if cur.LastError == nil && diskRT.LastError != nil {
						cur.LastError = diskRT.LastError
						cur.LastAction = diskRT.LastAction
					}
					store.ByID[id] = cur
				}
			}

			if !pol.Enabled {
				log.Printf("[devices-poller] disabled in config — idle")
			} else {
				probed, errs := store.PollAll(ctx, cfgFile.Devices, pol)
				if probed > 0 || errs > 0 {
					log.Printf("[devices-poller] status ok=%d err=%d cfg=%d", probed, errs, nDev)
				}
				work := mining.Read(paths.MiningWork(dataDir), float64(pol.MiningWorkMaxAgeSec))
				if work != "" {
					store.SyncWithMining(ctx, cfgFile.Devices, work, pol)
				} else {
					// throttle log ~60s
					if time.Now().Unix()%60 < int64(max(3, pol.IntervalSec)) {
						log.Printf("[devices-poller] no mining_work (snapshot stale/missing, cfg=%d)", nDev)
					}
				}
			}

			// persist
			if err := state.Save(paths.DevicesState(dataDir), store.ByID); err != nil {
				log.Printf("[devices-poller] save state: %v", err)
			}
			if err := state.SaveDeadlines(paths.Deadlines(dataDir), store.Deadlines, store.SyncTS); err != nil {
				log.Printf("[devices-poller] save deadlines: %v", err)
			}
		}()

		interval := pol.IntervalSec
		if interval < 3 {
			interval = 5
		}
		if interval > 120 {
			interval = 120
		}
		spent := time.Since(t0)
		wait := time.Duration(interval)*time.Second - spent
		if wait < time.Second {
			wait = time.Second
		}
		select {
		case <-ctx.Done():
			break
		case <-time.After(wait):
		}
		if ctx.Err() != nil {
			break
		}
	}

	// final save
	_ = state.Save(paths.DevicesState(dataDir), store.ByID)
	_ = state.SaveDeadlines(paths.Deadlines(dataDir), store.Deadlines, store.SyncTS)
	log.Printf("[devices-poller] loop stop")
	return nil
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
