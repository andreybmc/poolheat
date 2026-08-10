package device

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/backend"
	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/andreybmc/poolheat/edge/internal/state"
)

const enforceCooldownSec = 8.0

// Store holds mutable runtime + deadlines for one process.
type Store struct {
	ByID      map[string]state.Runtime
	Deadlines map[string]float64
	SyncTS    map[string]float64
	EnforceTS map[string]float64
	// DesiredTouched: ids whose desired_on we set this tick (don't clobber from disk on save)
	DesiredTouched map[string]bool
}

func NewStore(byID map[string]state.Runtime, deadlines, syncTS map[string]float64) *Store {
	if byID == nil {
		byID = map[string]state.Runtime{}
	}
	if deadlines == nil {
		deadlines = map[string]float64{}
	}
	if syncTS == nil {
		syncTS = map[string]float64{}
	}
	return &Store{
		ByID:           byID,
		Deadlines:      deadlines,
		SyncTS:         syncTS,
		EnforceTS:      map[string]float64{},
		DesiredTouched: map[string]bool{},
	}
}

func (s *Store) getRT(id string) state.Runtime {
	if r, ok := s.ByID[id]; ok {
		return r
	}
	return state.Runtime{}
}

func (s *Store) update(id string, mut func(*state.Runtime)) {
	r := s.getRT(id)
	mut(&r)
	s.ByID[id] = r
}

func boolPtr(b bool) *bool { return &b }
func strPtr(s string) *string { return &s }

// PollStatus probes device and applies enforce/adopt when applyPolicy.
func (s *Store) PollStatus(ctx context.Context, cfg config.DeviceCfg, source string, applyPolicy bool) error {
	did := cfg.ID
	be := cfg.BackendNorm()
	res, err := backend.Control(ctx, nil, cfg)
	if err != nil {
		pretty := err.Error()
		s.update(did, func(r *state.Runtime) {
			r.Online = boolPtr(false)
			r.LastError = strPtr(pretty)
			r.LastAction = strPtr(source + "_fail")
		})
		return err
	}
	var reported *bool
	if res.On != nil {
		logi := backend.PhysicalToLogical(*res.On, cfg.Inverted)
		reported = &logi
	}
	s.update(did, func(r *state.Runtime) {
		if reported != nil {
			r.LastOn = boolPtr(*reported)
		}
		r.Online = boolPtr(true)
		r.LastError = nil
		ts := state.NowISO()
		r.LastOkTS = &ts
		r.LastAction = strPtr(source + ":" + be)
		if res.Power != nil {
			r.LastPower = res.Power
			r.LastPowerTS = &ts
		}
	})
	if applyPolicy && reported != nil {
		if cfg.EnforceDesired {
			_ = s.EnforceDesired(ctx, cfg, *reported)
		} else {
			s.AdoptReported(did, *reported)
		}
	}
	return nil
}

// SetLogical sets logical ON/OFF.
// force=true: always talk to device (enforce path — don't trust cached LastOn).
func (s *Store) SetLogical(ctx context.Context, cfg config.DeviceCfg, on bool, source string, force bool) error {
	did := cfg.ID
	be := cfg.BackendNorm()
	// no-op if already reported same (unless force — enforce must re-check hardware)
	rt := s.getRT(did)
	if !force && rt.LastOn != nil && *rt.LastOn == on {
		s.update(did, func(r *state.Runtime) {
			r.DesiredOn = boolPtr(on)
		})
		s.DesiredTouched[did] = true
		return nil
	}
	phys := backend.LogicalToPhysical(on, cfg.Inverted)
	res, err := backend.Control(ctx, &phys, cfg)
	if err != nil {
		pretty := err.Error()
		s.update(did, func(r *state.Runtime) {
			r.Online = boolPtr(false)
			r.LastError = strPtr(pretty)
			r.LastAction = strPtr(source + "_fail")
			// keep desired so next tick still tries to restore
			r.DesiredOn = boolPtr(on)
		})
		s.DesiredTouched[did] = true
		return err
	}
	reported := on
	if res.On != nil {
		reported = backend.PhysicalToLogical(*res.On, cfg.Inverted)
	}
	s.update(did, func(r *state.Runtime) {
		r.LastOn = boolPtr(reported)
		r.DesiredOn = boolPtr(on)
		r.Online = boolPtr(true)
		r.LastError = nil
		ts := state.NowISO()
		r.LastOkTS = &ts
		if res.Skipped {
			r.LastAction = strPtr(source + ":" + be + ":skip")
		} else {
			r.LastAction = strPtr(source + ":" + be)
		}
		if res.Power != nil {
			r.LastPower = res.Power
			r.LastPowerTS = &ts
		}
	})
	s.DesiredTouched[did] = true
	if res.Skipped {
		log.Printf("[devices] set %s: skipped (%s)", cfg.Label(), res.Reason)
	} else {
		log.Printf("[devices] set %s: %v (%s)", cfg.Label(), on, source)
	}
	// if hardware still disagrees after set — surface as soft error for next tick
	if reported != on {
		err = fmt.Errorf("after set: reported=%s desired=%s", onOff(reported), onOff(on))
		log.Printf("[devices] set %s: %v", cfg.Label(), err)
		// still keep desired; return error so caller can log
		return err
	}
	return nil
}

func (s *Store) EnforceDesired(ctx context.Context, cfg config.DeviceCfg, reported bool) error {
	if !cfg.EnforceDesired {
		return nil
	}
	did := cfg.ID
	rt := s.getRT(did)
	if rt.DesiredOn == nil {
		// seed hold target from first successful probe
		s.update(did, func(r *state.Runtime) {
			r.DesiredOn = boolPtr(reported)
		})
		s.DesiredTouched[did] = true
		log.Printf("[devices] enforce seed desired %s = %s", cfg.Label(), onOff(reported))
		return nil
	}
	desired := *rt.DesiredOn
	if desired == reported {
		return nil
	}
	now := float64(time.Now().UnixNano()) / 1e9
	if now-s.EnforceTS[did] < enforceCooldownSec {
		return nil
	}
	s.EnforceTS[did] = now
	driver := backend.DriverLabel(cfg.Backend)
	log.Printf("[devices] enforce desired %s/%s: reported=%s → desired=%s",
		cfg.Label(), driver, onOff(reported), onOff(desired))
	// force=true: ignore cached LastOn, re-apply on wire (SmartLife external change)
	err := s.SetLogical(ctx, cfg, desired, "enforce_desired", true)
	if err != nil {
		log.Printf("[devices] enforce desired %s FAILED: %v", cfg.Label(), err)
	} else {
		log.Printf("[devices] enforce desired %s OK restored %s", cfg.Label(), onOff(desired))
	}
	return err
}

func (s *Store) AdoptReported(did string, reported bool) {
	rt := s.getRT(did)
	if rt.DesiredOn != nil && *rt.DesiredOn == reported {
		return
	}
	s.update(did, func(r *state.Runtime) {
		r.DesiredOn = boolPtr(reported)
	})
	s.DesiredTouched[did] = true
}

// PollAll status for enabled ready devices.
func (s *Store) PollAll(ctx context.Context, devices []config.DeviceCfg, pol config.PollerCfg) (probed, errors int) {
	backoff := float64(pol.ErrorBackoffSec)
	if backoff < 5 {
		backoff = 5
	}
	timeout := time.Duration(pol.SetTimeoutSec) * time.Second
	if timeout < 3*time.Second {
		timeout = 15 * time.Second
	}
	now := float64(time.Now().UnixNano()) / 1e9
	for _, cfg := range devices {
		if !cfg.IsEnabled() || cfg.ID == "" {
			continue
		}
		if !backend.Ready(cfg) {
			continue
		}
		rt := s.getRT(cfg.ID)
		// Back off only after status probe failures — still re-poll after enforce
		// set failures so hold can restore after SmartLife toggles.
		if rt.LastError != nil && rt.LastAction != nil {
			act := *rt.LastAction
			if strings.HasSuffix(act, "_fail") && !strings.Contains(act, "enforce") {
				if now-s.SyncTS[cfg.ID] < backoff {
					continue
				}
			}
		}
		cctx, cancel := context.WithTimeout(ctx, timeout)
		err := s.PollStatus(cctx, cfg, "poll", true)
		cancel()
		if err != nil {
			errors++
			s.SyncTS[cfg.ID] = now
			log.Printf("[devices-poller] status %s: %v", cfg.Label(), err)
			continue
		}
		probed++
	}
	return probed, errors
}

// SyncWithMining applies auto_on / auto_off policy.
func (s *Store) SyncWithMining(ctx context.Context, devices []config.DeviceCfg, work string, pol config.PollerCfg) {
	work = strings.ToLower(work)
	if work == "mining" {
		work = "resume"
	}
	if work == "sleep" {
		work = "suspend"
	}
	if work != "resume" && work != "suspend" {
		return
	}
	timeout := time.Duration(pol.SetTimeoutSec) * time.Second
	if timeout < 3*time.Second {
		timeout = 15 * time.Second
	}
	backoff := float64(pol.ErrorBackoffSec)
	if backoff < 5 {
		backoff = 5
	}
	now := float64(time.Now().UnixNano()) / 1e9

	for _, cfg := range devices {
		if !cfg.IsEnabled() || !backend.Ready(cfg) || cfg.ID == "" {
			continue
		}
		did := cfg.ID
		rt := s.getRT(did)
		// error backoff
		if rt.LastError != nil && rt.LastAction != nil && strings.HasSuffix(*rt.LastAction, "_fail") {
			if now-s.SyncTS[did] < backoff {
				continue
			}
		}
		var reported *bool
		if rt.LastOn != nil {
			reported = rt.LastOn
		}
		var desired *bool
		if rt.DesiredOn != nil {
			desired = rt.DesiredOn
		}
		autoOn := cfg.AutoOnMining
		autoOff := cfg.AutoOffSuspend
		delay := float64(cfg.OffDelaySec())
		var want *bool
		src := "auto"

		if work == "resume" {
			if _, ok := s.Deadlines[did]; ok {
				delete(s.Deadlines, did)
				log.Printf("[devices] suspend-off cancelled %s (mining resume)", cfg.Label())
			}
			if autoOn {
				if reported == nil || !*reported {
					t := true
					want = &t
					src = "auto_mining"
				} else if desired == nil || !*desired {
					s.update(did, func(r *state.Runtime) {
						r.DesiredOn = boolPtr(true)
					})
				}
			}
		} else if work == "suspend" && autoOff {
			if reported != nil && !*reported {
				delete(s.Deadlines, did)
				if desired == nil || *desired {
					s.update(did, func(r *state.Runtime) {
						r.DesiredOn = boolPtr(false)
					})
				}
			} else {
				dl, has := s.Deadlines[did]
				if !has {
					if delay <= 0 {
						t := false
						want = &t
						src = "auto_suspend"
					} else {
						s.Deadlines[did] = now + delay
						log.Printf("[devices] suspend-off in %.0fs %s", delay, cfg.Label())
					}
				} else if now >= dl {
					t := false
					want = &t
					src = "auto_suspend"
					delete(s.Deadlines, did)
				}
			}
		} else if work == "suspend" && !autoOff {
			delete(s.Deadlines, did)
		}

		if want == nil {
			continue
		}
		s.SyncTS[did] = now
		repS, desS := "?", "?"
		if reported != nil {
			repS = onOff(*reported)
		}
		if desired != nil {
			desS = onOff(*desired)
		}
		log.Printf("[devices] sync %s: work=%s → %s (%s) reported=%s desired=%s",
			cfg.Label(), work, onOff(*want), src, repS, desS)
		cctx, cancel := context.WithTimeout(ctx, timeout)
		err := s.SetLogical(cctx, cfg, *want, src, true)
		cancel()
		if err != nil {
			log.Printf("[devices] sync %s: %v", cfg.Label(), err)
		}
	}
}

// MergeDesiredFromDisk refreshes desired_on from devices_state.json (UI may write).
// Does not overwrite desired we set this tick (DesiredTouched).
func (s *Store) MergeDesiredFromDisk(disk map[string]state.Runtime) {
	if disk == nil {
		return
	}
	for id, diskRT := range disk {
		if s.DesiredTouched[id] {
			continue
		}
		if diskRT.DesiredOn == nil {
			continue
		}
		cur := s.getRT(id)
		// always take disk desired when UI/API wrote it
		if cur.DesiredOn == nil || *cur.DesiredOn != *diskRT.DesiredOn {
			cur.DesiredOn = diskRT.DesiredOn
			s.ByID[id] = cur
		}
		// seed last_on only if we never probed
		if cur.LastOn == nil && diskRT.LastOn != nil {
			cur.LastOn = diskRT.LastOn
			s.ByID[id] = cur
		}
	}
}

// MergeBeforeSave: disk desired wins for devices we did not touch this tick
// (prevents wiping concurrent UI writes).
func (s *Store) MergeBeforeSave(disk map[string]state.Runtime) {
	if disk == nil {
		return
	}
	for id, diskRT := range disk {
		if s.DesiredTouched[id] {
			continue
		}
		cur, ok := s.ByID[id]
		if !ok {
			// keep unknown ids from disk
			s.ByID[id] = diskRT
			continue
		}
		if diskRT.DesiredOn != nil {
			cur.DesiredOn = diskRT.DesiredOn
		}
		s.ByID[id] = cur
	}
}

// ClearTickFlags resets per-tick bookkeeping.
func (s *Store) ClearTickFlags() {
	s.DesiredTouched = map[string]bool{}
}

func onOff(b bool) string {
	if b {
		return "ON"
	}
	return "OFF"
}

// MergeCfgState returns cfg with runtime fields for logging (not needed often).
func Merge(cfg config.DeviceCfg, rt state.Runtime) string {
	return fmt.Sprintf("%s last_on=%v online=%v", cfg.Label(), rt.LastOn, rt.Online)
}
