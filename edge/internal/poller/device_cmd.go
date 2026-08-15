package poller

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/backend"
	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/andreybmc/poolheat/edge/internal/device"
	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
	"github.com/andreybmc/poolheat/edge/internal/paths"
	"github.com/andreybmc/poolheat/edge/internal/state"
)

// serialize file pickup so the tick loop and the UI watcher never double-read
var deviceCmdMu sync.Mutex

// File IPC: serve enqueues device_req.json; poller executes via backends (tuya/tapo/…).
// serve never imports tinytuya.
const (
	deviceReqFile    = "device_req.json"
	deviceResultFile = "device_result.json"
)

// DeviceRequest is enqueued by serve for status (on=null) or set.
type DeviceRequest struct {
	ID       string  `json:"id"`
	TS       float64 `json:"ts"`
	DeviceID string  `json:"device_id"`
	// On is nil for status-only; true/false for set (logical ON).
	// Brightness/Mode alone also count as set (lights/dimmers).
	On         *bool   `json:"on"`
	Brightness *int    `json:"brightness,omitempty"` // 0–100 %
	Mode       *string `json:"mode,omitempty"`       // white|colour|scene|music
	Source     string  `json:"source,omitempty"`
	Force      bool    `json:"force,omitempty"`
}

// DeviceResult is written after handling DeviceRequest.
type DeviceResult struct {
	ID         string         `json:"id"`
	OK         bool           `json:"ok"`
	On         *bool          `json:"on,omitempty"` // logical on when known
	Brightness *int           `json:"brightness,omitempty"`
	Mode       *string        `json:"mode,omitempty"`
	Error      string         `json:"error,omitempty"`
	TS         float64        `json:"ts"`
	Backend    string         `json:"backend,omitempty"`
	Power      map[string]any `json:"power,omitempty"`
	Skipped    bool           `json:"skipped,omitempty"`
	Reason     string         `json:"reason,omitempty"`
	Extra      map[string]any `json:"extra,omitempty"`
}

// ProcessPendingDeviceCmd handles one device_req.json if present.
// Returns true if a request was consumed.
func ProcessPendingDeviceCmd(ctx context.Context, dataDir string, store *device.Store) bool {
	deviceCmdMu.Lock()
	defer deviceCmdMu.Unlock()
	path := filepath.Join(dataDir, deviceReqFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var req DeviceRequest
	if err := json.Unmarshal(b, &req); err != nil {
		log.Printf("[devices-poller] device-req json: %v", err)
		_ = os.Remove(path)
		return true
	}
	if strings.TrimSpace(req.ID) == "" || strings.TrimSpace(req.DeviceID) == "" {
		log.Printf("[devices-poller] device-req incomplete id=%q did=%q", req.ID, req.DeviceID)
		_ = os.Remove(path)
		return true
	}
	_ = os.Remove(path)

	res := executeDeviceCmd(backend.WithPriority(ctx), dataDir, store, req)
	if err := jsonutil.SaveAtomic(filepath.Join(dataDir, deviceResultFile), res); err != nil {
		log.Printf("[devices-poller] device-result: %v", err)
	}
	if res.OK {
		log.Printf("[devices-poller] device-cmd ok id=%s did=%s on=%v", req.ID, req.DeviceID, req.On)
	} else {
		log.Printf("[devices-poller] device-cmd fail id=%s: %s", req.ID, res.Error)
	}
	return true
}

func executeDeviceCmd(ctx context.Context, dataDir string, store *device.Store, req DeviceRequest) DeviceResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := DeviceResult{ID: req.ID, TS: now}
	cfgFile, err := config.Load(paths.DevicesConfig(dataDir))
	if err != nil {
		res.Error = fmt.Sprintf("load config: %v", err)
		return res
	}
	var cfg *config.DeviceCfg
	for i := range cfgFile.Devices {
		if cfgFile.Devices[i].ID == req.DeviceID {
			cfg = &cfgFile.Devices[i]
			break
		}
	}
	if cfg == nil {
		res.Error = "device not found"
		return res
	}
	src := strings.TrimSpace(req.Source)
	if src == "" {
		src = "api"
	}

	isSet := req.On != nil || req.Brightness != nil || req.Mode != nil

	// status only
	if !isSet {
		cctx, cancel := context.WithTimeout(ctx, 8*time.Second)
		defer cancel()
		if store != nil {
			store.LockDevice(cfg.ID)
			err := store.PollStatus(cctx, *cfg, src+":status")
			store.UnlockDevice(cfg.ID)
			rt := store.Runtime(cfg.ID)
			if err != nil {
				res.Error = err.Error()
				if rt.LastOn != nil {
					res.On = rt.LastOn
				}
				return res
			}
			res.OK = true
			res.Backend = cfg.BackendNorm()
			if rt.LastOn != nil {
				res.On = rt.LastOn
			}
			if rt.LastPower != nil {
				res.Power = rt.LastPower
			}
			res.Brightness = rt.LastBrightness
			res.Mode = rt.LastMode
			if rt.LastTelemetry != nil {
				res.Extra = rt.LastTelemetry
			}
			_ = state.Save(paths.DevicesState(dataDir), store.SnapshotRuntime())
			return res
		}
		br, err := backend.Control(cctx, nil, *cfg)
		if err != nil {
			res.Error = err.Error()
			return res
		}
		res.OK = true
		res.Backend = br.Backend
		res.Power = br.Power
		res.Extra = br.Extra
		fillLightResult(&res, br.Extra)
		if br.On != nil {
			logical := backend.PhysicalToLogical(*br.On, cfg.Inverted)
			res.On = &logical
		}
		return res
	}

	// set logical (+ optional light brightness/mode)
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cfgCopy := *cfg
	if req.Brightness != nil {
		b := *req.Brightness
		if b < 0 {
			b = 0
		}
		if b > 100 {
			b = 100
		}
		cfgCopy.SetBrightness = &b
	}
	if req.Mode != nil && strings.TrimSpace(*req.Mode) != "" {
		m := strings.TrimSpace(*req.Mode)
		cfgCopy.SetMode = &m
	}
	// if only brightness/mode — keep current on state (or turn on when bright>0)
	wantOn := false
	if req.On != nil {
		wantOn = *req.On
	} else if store != nil {
		rt := store.Runtime(cfg.ID)
		if rt.LastOn != nil {
			wantOn = *rt.LastOn
		} else {
			wantOn = true // dimming implies on
		}
	} else {
		wantOn = true
	}
	if req.Brightness != nil && *req.Brightness > 0 && req.On == nil {
		wantOn = true
	}

	if store != nil {
		store.LockDevice(cfg.ID)
		err := store.SetLogical(cctx, cfgCopy, wantOn, src, req.Force || req.Brightness != nil || req.Mode != nil)
		store.UnlockDevice(cfg.ID)
		rt := store.Runtime(cfg.ID)
		if rt.LastOn != nil {
			res.On = rt.LastOn
		}
		if rt.LastPower != nil {
			res.Power = rt.LastPower
		}
		res.Brightness = rt.LastBrightness
		res.Mode = rt.LastMode
		if rt.LastTelemetry != nil {
			res.Extra = rt.LastTelemetry
		}
		if err != nil {
			res.Error = err.Error()
			res.OK = false
			_ = state.Save(paths.DevicesState(dataDir), store.SnapshotRuntime())
			return res
		}
		res.OK = true
		res.Backend = cfg.BackendNorm()
		if res.On == nil {
			on := wantOn
			res.On = &on
		}
		_ = state.Save(paths.DevicesState(dataDir), store.SnapshotRuntime())
		return res
	}
	// no store — direct backend
	phys := backend.LogicalToPhysical(wantOn, cfg.Inverted)
	br, err := backend.Control(cctx, &phys, cfgCopy)
	if err != nil {
		res.Error = err.Error()
		return res
	}
	res.OK = true
	res.Backend = br.Backend
	res.Skipped = br.Skipped
	res.Reason = br.Reason
	res.Power = br.Power
	res.Extra = br.Extra
	fillLightResult(&res, br.Extra)
	if br.On != nil {
		logical := backend.PhysicalToLogical(*br.On, cfg.Inverted)
		res.On = &logical
	} else {
		on := wantOn
		res.On = &on
	}
	return res
}

func fillLightResult(res *DeviceResult, extra map[string]any) {
	if extra == nil {
		return
	}
	if v, ok := extra["brightness_pct"]; ok && v != nil {
		switch t := v.(type) {
		case float64:
			n := int(t)
			res.Brightness = &n
		case int:
			res.Brightness = &t
		}
	}
	if v, ok := extra["mode"].(string); ok && v != "" {
		res.Mode = &v
	}
}
