package device

import (
	"context"
	"fmt"
	"log"
	"strings"
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

// PollStatus probes device status only (no enforce). Use ApplyPolicy after all probes
// so each set() gets a fresh timeout budget (status+set in one ctx often starved set).
func (s *Store) PollStatus(ctx context.Context, cfg config.DeviceCfg, source string) error {
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
	})
	return nil
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
	cool := s.EnforceCooldownSec
	if cool <= 0 {
		cool = defaultEnforceCooldownSec
	}
	if now-s.EnforceTS[did] < cool {
		return nil
	}
	s.EnforceTS[did] = now
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
	s.DesiredTouched[did] = true
}

// PollAll: phase1 status all devices, phase2 enforce/adopt with fresh timeouts.
// Background hold is independent of UI pokes: every tick status → if enforce and
// reported≠desired → set. UI may also enqueue device_req for set/status.
func (s *Store) PollAll(ctx context.Context, devices []config.DeviceCfg, pol config.PollerCfg) (probed, errors, enforced int) {
	backoff := float64(pol.ErrorBackoffSec)
	if backoff < 5 {
		backoff = 5
	}
	statusTimeout := pol.StatusTimeout()
	setTimeout := pol.SetTimeout()
	// per-tick cooldown from config (UI Advanced → enforce_cooldown_sec)
	if pol.EnforceCooldownSec > 0 {
		s.EnforceCooldownSec = float64(pol.EnforceCooldownSec)
	} else if s.EnforceCooldownSec <= 0 {
		s.EnforceCooldownSec = defaultEnforceCooldownSec
	}
	now := float64(time.Now().UnixNano()) / 1e9

	type item struct {
		cfg config.DeviceCfg
		ok  bool // true = fresh status this tick
	}
	var batch []item

	// ── phase 1: status only ───────────────────────────────────────────
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
		// Back off only after status probe failures (not enforce fails)
		if rt.LastError != nil && rt.LastAction != nil {
			act := *rt.LastAction
			if strings.HasSuffix(act, "_fail") && !strings.Contains(act, "enforce") {
				if now-s.SyncTS[cfg.ID] < backoff {
					// still allow hold on last known mismatch without new status
					batch = append(batch, item{cfg: cfg, ok: false})
					continue
				}
			}
		}
		cctx, cancel := context.WithTimeout(ctx, statusTimeout)
		err := s.PollStatus(cctx, cfg, "poll")
		cancel()
		if err != nil {
			errors++
			s.SyncTS[cfg.ID] = now
			log.Printf("[devices-poller] status %s: %v", cfg.Label(), err)
			batch = append(batch, item{cfg: cfg, ok: false})
			continue
		}
		probed++
		batch = append(batch, item{cfg: cfg, ok: true})
	}

	// ── phase 2: enforce / adopt ───────────────────────────────────────
	// adopt only on fresh status; enforce (hold) also on last-known mismatch
	// when status failed this tick (still try to push desired on the wire).
	for _, it := range batch {
		cfg := it.cfg
		rt := s.getRT(cfg.ID)
		if rt.LastOn == nil {
			continue
		}
		if !it.ok {
			// no fresh status: only hold if enforce + known mismatch
			if !cfg.EnforceDesired || rt.DesiredOn == nil || *rt.DesiredOn == *rt.LastOn {
				continue
			}
			log.Printf("[devices-poller] hold (stale status) %s: last=%s desired=%s → restore",
				cfg.Label(), onOff(*rt.LastOn), onOff(*rt.DesiredOn))
			cctx, cancel := context.WithTimeout(ctx, setTimeout)
			err := s.EnforceDesired(cctx, cfg, *rt.LastOn)
			cancel()
			if err != nil {
				log.Printf("[devices-poller] policy %s: %v", cfg.Label(), err)
				continue
			}
			rt2 := s.getRT(cfg.ID)
			if rt2.LastOn != nil && rt2.DesiredOn != nil && *rt2.LastOn == *rt2.DesiredOn {
				enforced++
			}
			continue
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
			// do not mark status backoff — keep retrying hold next tick
			continue
		}
		if needRestore {
			rt2 := s.getRT(cfg.ID)
			if rt2.LastOn != nil && rt2.DesiredOn != nil && *rt2.LastOn == *rt2.DesiredOn {
				enforced++
			}
		}
	}
	return probed, errors, enforced
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
	timeout := pol.SetTimeout()
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
		// error backoff (status fails only — not enforce)
		if rt.LastError != nil && rt.LastAction != nil {
			act := *rt.LastAction
			if strings.HasSuffix(act, "_fail") && !strings.Contains(act, "enforce") {
				if now-s.SyncTS[did] < backoff {
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
