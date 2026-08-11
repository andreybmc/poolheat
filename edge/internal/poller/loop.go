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
	store.DataDir = dataDir // policy_events.json (UI Action log) on auto-restore

	log.Printf("[devices-poller] loop start (device_req IPC + hold)")
	for {
		if ctx.Err() != nil {
			break
		}
		t0 := time.Now()
		pol := config.DefaultPoller()
		nDev := 0

		// UI/API commands first (serve never talks tinytuya)
		for i := 0; i < 4; i++ {
			if !ProcessPendingDeviceCmd(ctx, dataDir, store) {
				break
			}
		}

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

			store.ClearTickFlags()

			// refresh desired_on from disk each tick (UI/API may have written it)
			diskState, _ := state.Load(paths.DevicesState(dataDir))
			store.MergeDesiredFromDisk(diskState)

			// log enforce flags once in a while for diagnosis
			if time.Now().Unix()%120 < int64(max(3, pol.IntervalSec)) {
				for _, d := range cfgFile.Devices {
					if !d.IsEnabled() {
						continue
					}
					rt := store.ByID[d.ID]
					des := "?"
					if rt.DesiredOn != nil {
						if *rt.DesiredOn {
							des = "ON"
						} else {
							des = "OFF"
						}
					}
					rep := "?"
					if rt.LastOn != nil {
						if *rt.LastOn {
							rep = "ON"
						} else {
							rep = "OFF"
						}
					}
					if d.EnforceDesired {
						log.Printf("[devices-poller] hold %s enforce=on desired=%s reported=%s",
							d.Label(), des, rep)
					}
				}
			}

			if !pol.Enabled {
				log.Printf("[devices-poller] disabled in config — idle")
			} else {
				probed, errs, enforced := store.PollAll(ctx, cfgFile.Devices, pol)
				if probed > 0 || errs > 0 || enforced > 0 {
					log.Printf("[devices-poller] status ok=%d err=%d hold=%d cfg=%d",
						probed, errs, enforced, nDev)
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

			// re-merge disk desired for devices we did not touch (UI race)
			if disk2, err := state.Load(paths.DevicesState(dataDir)); err == nil {
				store.MergeBeforeSave(disk2)
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
		if wait < 500*time.Millisecond {
			wait = 500 * time.Millisecond
		}
		// Slice wait so UI device_req is picked up quickly (no tinytuya in serve).
		deadline := time.Now().Add(wait)
		for time.Now().Before(deadline) {
			if ctx.Err() != nil {
				break
			}
			if ProcessPendingDeviceCmd(ctx, dataDir, store) {
				break
			}
			slice := 300 * time.Millisecond
			if rem := time.Until(deadline); rem < slice {
				slice = rem
			}
			if slice < 20*time.Millisecond {
				break
			}
			select {
			case <-ctx.Done():
			case <-time.After(slice):
			}
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
