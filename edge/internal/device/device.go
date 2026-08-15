package device

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/backend"
	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/andreybmc/poolheat/edge/internal/events"
	"github.com/andreybmc/poolheat/edge/internal/state"
)

// Default enforce cooldown when poller config omits enforce_cooldown_sec.
const defaultEnforceCooldownSec = 1.5

// Store holds mutable runtime + deadlines for one process.
type Store struct {
	mu sync.Mutex
	// devMu serializes LAN I/O per device so a hung Tuya cannot block a UI click
	// on another socket, and UI set is not interleaved with a status probe.
	devMu map[string]*sync.Mutex
	// DataDir is POOLHEAT_DATA — used to write policy_events.json (UI Action log).
	DataDir string
	// EnforceCooldownSec from devices_config poller (override default).
	EnforceCooldownSec float64
	ByID               map[string]state.Runtime
	Deadlines          map[string]float64
	SyncTS             map[string]float64
	EnforceTS          map[string]float64
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
		devMu:          map[string]*sync.Mutex{},
	}
}

func (s *Store) getRT(id string) state.Runtime {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.getRTLocked(id)
}

func (s *Store) update(id string, mut func(*state.Runtime)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r := s.getRTLocked(id)
	mut(&r)
	s.ByID[id] = r
}

func boolPtr(b bool) *bool    { return &b }
func strPtr(s string) *string { return &s }

// PollStatus probes device status only (no enforce). Use ApplyPolicy after all probes
// so each set() gets a fresh timeout budget (status+set in one ctx often starved set).
func (s *Store) PollStatus(ctx context.Context, cfg config.DeviceCfg, source string) error {
	did := cfg.ID
	be := cfg.BackendNorm()
	res, err := backend.Control(ctx, nil, cfg)
	if err != nil {
		if errors.Is(err, backend.ErrPreempted) {
			return err
		}
		// Technical detail in log only; UI gets laconic category
		log.Printf("[devices] %s status fail (%s): %v", cfg.Label(), be, err)
		pretty := UserFacingError(err, be)
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
	} else {
		// surface missing switch DPS so logs explain why hold cannot run
		log.Printf("[devices] %s status: on=nil (check tuya_switch_dps / local_key)", cfg.Label())
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
		applyLightExtra(r, res.Extra)
	})
	return nil
}

func applyLightExtra(r *state.Runtime, extra map[string]any) {
	if extra == nil {
		return
	}
	if v, ok := extra["brightness_pct"]; ok && v != nil {
		switch t := v.(type) {
		case float64:
			n := int(t)
			r.LastBrightness = &n
		case int:
			r.LastBrightness = &t
		case int64:
			n := int(t)
			r.LastBrightness = &n
		}
	}
	if v, ok := extra["mode"].(string); ok && v != "" {
		r.LastMode = &v
	}
	// compact telemetry for UI (avoid huge raw dumps)
	tel := map[string]any{}
	if v, ok := extra["brightness_raw"]; ok {
		tel["brightness_raw"] = v
	}
	if v, ok := extra["mode"]; ok {
		tel["mode"] = v
	}
	if v, ok := extra["colour_data"]; ok {
		tel["colour_data"] = v
	}
	if v, ok := extra["dps"]; ok {
		// keep only light-related keys
		if dps, ok := v.(map[string]any); ok {
			small := map[string]any{}
			for _, k := range []string{"20", "21", "22", "24", "25", "26", "34"} {
				if x, ok := dps[k]; ok {
					small[k] = x
				}
			}
			if len(small) > 0 {
				tel["dps"] = small
			}
		}
	}
	if len(tel) > 0 {
		r.LastTelemetry = tel
	}
}

// ApplyPolicy runs enforce_desired or adopt after a successful status probe.
func (s *Store) ApplyPolicy(ctx context.Context, cfg config.DeviceCfg) error {
	if !cfg.IsEnabled() {
		return nil
	}
	rt := s.getRT(cfg.ID)
	if rt.LastOn == nil {
		return nil
	}
	reported := *rt.LastOn
	if cfg.EnforceDesired {
		return s.EnforceDesired(ctx, cfg, reported)
	}
	s.AdoptReported(cfg.ID, reported)
	return nil
}

// SetLogical sets logical ON/OFF (and optional light brightness/mode via cfg.Set*).
// force=true: always talk to device (enforce path — don't trust cached LastOn).
func (s *Store) SetLogical(ctx context.Context, cfg config.DeviceCfg, on bool, source string, force bool) error {
	did := cfg.ID
	be := cfg.BackendNorm()
	// no-op if already reported same (unless force — enforce must re-check hardware)
	// Still send if brightness/mode requested.
	lightSet := cfg.SetBrightness != nil || cfg.SetMode != nil
	rt := s.getRT(did)
	if !force && !lightSet && rt.LastOn != nil && *rt.LastOn == on {
		s.update(did, func(r *state.Runtime) {
			r.DesiredOn = boolPtr(on)
		})
		s.touchDesired(did)
		return nil
	}
	phys := backend.LogicalToPhysical(on, cfg.Inverted)
	res, err := backend.Control(ctx, &phys, cfg)
	if err != nil {
		log.Printf("[devices] %s set fail (%s) on=%v: %v", cfg.Label(), be, on, err)
		pretty := UserFacingError(err, be)
		s.update(did, func(r *state.Runtime) {
			r.Online = boolPtr(false)
			r.LastError = strPtr(pretty)
			r.LastAction = strPtr(source + "_fail")
			// keep desired so next tick still tries to restore
			r.DesiredOn = boolPtr(on)
		})
		s.touchDesired(did)
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
		applyLightExtra(r, res.Extra)
	})
	s.touchDesired(did)
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
		s.touchDesired(did)
		log.Printf("[devices] enforce seed desired %s = %s", cfg.Label(), onOff(reported))
		return nil
	}
	desired := *rt.DesiredOn
	if desired == reported {
		return nil
	}
	now := float64(time.Now().UnixNano()) / 1e9
	cool := s.EnforceCooldownSec
	if cool <= 0 {
		cool = defaultEnforceCooldownSec
	}
	if now-s.getEnforceTS(did) < cool {
		return nil
	}
	s.setEnforceTS(did, now)
	driver := backend.DriverLabel(cfg.Backend)
	who := displayWho(cfg, driver)
	wantS := onOff(desired)
	wasS := onOff(reported)
	log.Printf("[devices] enforce desired %s/%s: reported=%s → desired=%s",
		cfg.Label(), driver, wasS, wantS)
	// force=true: ignore cached LastOn, re-apply on wire (SmartLife/Tapo external change)
	err := s.SetLogical(ctx, cfg, desired, "enforce_desired", true)
	if err != nil {
		log.Printf("[devices] enforce desired %s FAILED: %v", cfg.Label(), err)
		s.logEnforceEvent("err",
			fmt.Sprintf("%s: failed to restore %s: %v", who, wantS, err),
			cfg, driver, desired, reported)
		return err
	}
	// Re-check runtime after set — refuse false success (skipped/already_in_state lies).
	rt2 := s.getRT(did)
	if rt2.LastOn == nil || *rt2.LastOn != desired {
		rep := "?"
		if rt2.LastOn != nil {
			rep = onOff(*rt2.LastOn)
		}
		err = fmt.Errorf("still reported=%s after set desired=%s", rep, onOff(desired))
		log.Printf("[devices] enforce desired %s FAILED: %v", cfg.Label(), err)
		s.logEnforceEvent("err",
			fmt.Sprintf("%s: failed to restore %s: %v", who, wantS, err),
			cfg, driver, desired, reported)
		return err
	}
	log.Printf("[devices] enforce desired %s OK restored %s", cfg.Label(), wantS)
	// UI Action log (policy_events.json) — same shape as Python _devices_event_log
	s.logEnforceEvent("device",
		fmt.Sprintf("%s: restored %s (was %s, external change)", who, wantS, wasS),
		cfg, driver, desired, reported)
	return nil
}

// logEnforceEvent writes to system Action log (policy_events.json) for UI Events.
func (s *Store) logEnforceEvent(kind, msg string, cfg config.DeviceCfg, driver string, desired, reported bool) {
	if s == nil || s.DataDir == "" {
		return
	}
	d, r := desired, reported
	extra := events.Event{
		Source:     "enforce_desired",
		DeviceID:   cfg.ID,
		Alias:      cfg.Alias,
		Backend:    cfg.Backend,
		Driver:     driver,
		DesiredOn:  &d,
		ReportedOn: &r,
	}
	events.AppendDevice(s.DataDir, kind, msg, extra)
}

// displayWho matches serve.py who = "{name} ({driver})" for Action log lines.
func displayWho(cfg config.DeviceCfg, driver string) string {
	name := strings.TrimSpace(cfg.NameEN)
	if name == "" {
		name = strings.TrimSpace(cfg.Name)
	}
	if name == "" {
		name = strings.TrimSpace(cfg.NameRU)
	}
	if name == "" {
		name = strings.TrimSpace(cfg.Alias)
	}
	if name == "" {
		name = cfg.ID
	}
	if driver != "" {
		return name + " (" + driver + ")"
	}
	return name
}

func (s *Store) AdoptReported(did string, reported bool) {
	rt := s.getRT(did)
	if rt.DesiredOn != nil && *rt.DesiredOn == reported {
		return
	}
	s.update(did, func(r *state.Runtime) {
		r.DesiredOn = boolPtr(reported)
	})
	s.touchDesired(did)
}

// PollAll: phase1 status all devices in parallel, phase2 enforce/adopt.
// A hung device cannot block others. UI commands take the per-device lock
// and are skipped here (TryLock) so a click is not queued behind status.
func (s *Store) PollAll(ctx context.Context, devices []config.DeviceCfg, pol config.PollerCfg) (probed, errCount, enforced int) {
	backoff := float64(pol.ErrorBackoffSec)
	if backoff < 5 {
		backoff = 5
	}
	statusTimeout := pol.StatusTimeout()
	setTimeout := pol.SetTimeout()
	if pol.EnforceCooldownSec > 0 {
		s.EnforceCooldownSec = float64(pol.EnforceCooldownSec)
	} else if s.EnforceCooldownSec <= 0 {
		s.EnforceCooldownSec = defaultEnforceCooldownSec
	}
	now := float64(time.Now().UnixNano()) / 1e9

	type item struct {
		cfg config.DeviceCfg
		ok  bool
	}
	var (
		todo  []config.DeviceCfg
		batch []item
		bmu   sync.Mutex
	)

	for _, cfg := range devices {
		if !cfg.IsEnabled() || cfg.ID == "" {
			continue
		}
		if !backend.Ready(cfg) {
			if cfg.BackendNorm() == "tuya" {
				log.Printf("[devices-poller] skip %s: not ready (need ip+device_id+local_key)", cfg.Label())
			}
			continue
		}
		rt := s.getRT(cfg.ID)
		if rt.LastError != nil && rt.LastAction != nil {
			act := *rt.LastAction
			if strings.HasSuffix(act, "_fail") && !strings.Contains(act, "enforce") {
				if now-s.getSyncTS(cfg.ID) < backoff {
					batch = append(batch, item{cfg: cfg, ok: false})
					continue
				}
			}
		}
		todo = append(todo, cfg)
	}

	workers := pollWorkers(len(todo))
	sem := make(chan struct{}, workers)
	var wg sync.WaitGroup
	var probedN, errN atomic.Int32

	for i := range todo {
		cfg := todo[i]
		wg.Add(1)
		go func(cfg config.DeviceCfg) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
				defer func() { <-sem }()
			case <-ctx.Done():
				return
			}
			if !s.TryLockDevice(cfg.ID) {
				// UI command in flight — don't contend
				bmu.Lock()
				batch = append(batch, item{cfg: cfg, ok: false})
				bmu.Unlock()
				return
			}
			defer s.UnlockDevice(cfg.ID)

			cctx, cancel := context.WithTimeout(ctx, statusTimeout)
			err := s.PollStatus(cctx, cfg, "poll")
			cancel()
			bmu.Lock()
			defer bmu.Unlock()
			if err != nil {
				if errors.Is(err, backend.ErrPreempted) {
					batch = append(batch, item{cfg: cfg, ok: false})
					return
				}
				errN.Add(1)
				s.setSyncTS(cfg.ID, now)
				log.Printf("[devices-poller] status %s: %v", cfg.Label(), err)
				batch = append(batch, item{cfg: cfg, ok: false})
				return
			}
			probedN.Add(1)
			batch = append(batch, item{cfg: cfg, ok: true})
		}(cfg)
	}
	wg.Wait()
	probed = int(probedN.Load())
	errCount = int(errN.Load())

	// phase 2 — also parallel; skip devices the UI still holds
	var wg2 sync.WaitGroup
	sem2 := make(chan struct{}, workers)
	var enfN atomic.Int32
	snap := append([]item(nil), batch...)
	for i := range snap {
		it := snap[i]
		wg2.Add(1)
		go func(it item) {
			defer wg2.Done()
			select {
			case sem2 <- struct{}{}:
				defer func() { <-sem2 }()
			case <-ctx.Done():
				return
			}
			cfg := it.cfg
			rt := s.getRT(cfg.ID)
			if rt.LastOn == nil {
				return
			}
			if !s.TryLockDevice(cfg.ID) {
				return
			}
			defer s.UnlockDevice(cfg.ID)

			if !it.ok {
				if !cfg.EnforceDesired || rt.DesiredOn == nil || *rt.DesiredOn == *rt.LastOn {
					return
				}
				log.Printf("[devices-poller] hold (stale status) %s: last=%s desired=%s → restore",
					cfg.Label(), onOff(*rt.LastOn), onOff(*rt.DesiredOn))
				cctx, cancel := context.WithTimeout(ctx, setTimeout)
				err := s.EnforceDesired(cctx, cfg, *rt.LastOn)
				cancel()
				if err != nil {
					log.Printf("[devices-poller] policy %s: %v", cfg.Label(), err)
					return
				}
				rt2 := s.getRT(cfg.ID)
				if rt2.LastOn != nil && rt2.DesiredOn != nil && *rt2.LastOn == *rt2.DesiredOn {
					enfN.Add(1)
				}
				return
			}
			needRestore := cfg.EnforceDesired && rt.DesiredOn != nil && *rt.DesiredOn != *rt.LastOn
			if needRestore {
				log.Printf("[devices-poller] hold mismatch %s: reported=%s desired=%s → restore",
					cfg.Label(), onOff(*rt.LastOn), onOff(*rt.DesiredOn))
			}
			cctx, cancel := context.WithTimeout(ctx, setTimeout)
			err := s.ApplyPolicy(cctx, cfg)
			cancel()
			if err != nil {
				log.Printf("[devices-poller] policy %s: %v", cfg.Label(), err)
				return
			}
			if needRestore {
				rt2 := s.getRT(cfg.ID)
				if rt2.LastOn != nil && rt2.DesiredOn != nil && *rt2.LastOn == *rt2.DesiredOn {
					enfN.Add(1)
				}
			}
		}(it)
	}
	wg2.Wait()
	enforced = int(enfN.Load())
	return probed, errCount, enforced
}

// SyncWithMining applies auto_on / auto_off policy per bound miner.
// Unbound devices (empty MinerID) skip mining policy entirely.
// workByMiner maps managed miner id → resume|suspend (from mining_work by_miner).
// globalWork is legacy fallback when a bound miner has no by_miner entry but is active.
func (s *Store) SyncWithMining(ctx context.Context, devices []config.DeviceCfg, workByMiner map[string]string, globalWork string, pol config.PollerCfg) {
	timeout := pol.SetTimeout()
	backoff := float64(pol.ErrorBackoffSec)
	if backoff < 5 {
		backoff = 5
	}
	now := float64(time.Now().UnixNano()) / 1e9
	if workByMiner == nil {
		workByMiner = map[string]string{}
	}

	for _, cfg := range devices {
		if !cfg.IsEnabled() || !backend.Ready(cfg) || cfg.ID == "" {
			continue
		}
		// Unbound → ignore mining auto policy
		mid := strings.TrimSpace(cfg.MinerID)
		if mid == "" {
			s.clearDeadline(cfg.ID)
			continue
		}
		work := strings.ToLower(strings.TrimSpace(workByMiner[mid]))
		if work == "mining" {
			work = "resume"
		}
		if work == "sleep" {
			work = "suspend"
		}
		if work != "resume" && work != "suspend" {
			// unknown/stale for this miner — skip (no false triggers)
			continue
		}
		did := cfg.ID
		rt := s.getRT(did)
		// error backoff (status fails only — not enforce)
		if rt.LastError != nil && rt.LastAction != nil {
			act := *rt.LastAction
			if strings.HasSuffix(act, "_fail") && !strings.Contains(act, "enforce") {
				if now-s.getSyncTS(did) < backoff {
					continue
				}
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
		_ = globalWork // reserved for future active-id fallback

		if work == "resume" {
			if _, ok := s.deadlineOf(did); ok {
				s.clearDeadline(did)
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
				s.clearDeadline(did)
				if desired == nil || *desired {
					s.update(did, func(r *state.Runtime) {
						r.DesiredOn = boolPtr(false)
					})
				}
			} else {
				dl, has := s.deadlineOf(did)
				if !has {
					if delay <= 0 {
						t := false
						want = &t
						src = "auto_suspend"
					} else {
						s.setDeadline(did, now+delay)
						log.Printf("[devices] suspend-off in %.0fs %s", delay, cfg.Label())
					}
				} else if now >= dl {
					t := false
					want = &t
					src = "auto_suspend"
					s.clearDeadline(did)
				}
			}
		} else if work == "suspend" && !autoOff {
			s.clearDeadline(did)
		}

		if want == nil {
			continue
		}
		s.setSyncTS(did, now)
		repS, desS := "?", "?"
		if reported != nil {
			repS = onOff(*reported)
		}
		if desired != nil {
			desS = onOff(*desired)
		}
		log.Printf("[devices] sync %s: work=%s → %s (%s) reported=%s desired=%s",
			cfg.Label(), work, onOff(*want), src, repS, desS)
		if !s.TryLockDevice(did) {
			continue
		}
		cctx, cancel := context.WithTimeout(ctx, timeout)
		err := s.SetLogical(cctx, cfg, *want, src, true)
		cancel()
		s.UnlockDevice(did)
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
	s.mu.Lock()
	defer s.mu.Unlock()
	s.initGuards()
	for id, diskRT := range disk {
		if s.DesiredTouched[id] {
			continue
		}
		if diskRT.DesiredOn == nil {
			continue
		}
		cur := s.getRTLocked(id)
		if cur.DesiredOn == nil || *cur.DesiredOn != *diskRT.DesiredOn {
			cur.DesiredOn = diskRT.DesiredOn
			s.ByID[id] = cur
		}
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
	s.mu.Lock()
	defer s.mu.Unlock()
	s.initGuards()
	for id, diskRT := range disk {
		if s.DesiredTouched[id] {
			continue
		}
		cur, ok := s.ByID[id]
		if !ok {
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
	s.mu.Lock()
	s.DesiredTouched = map[string]bool{}
	s.mu.Unlock()
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
