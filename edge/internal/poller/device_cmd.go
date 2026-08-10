package poller

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/backend"
	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/andreybmc/poolheat/edge/internal/device"
	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
	"github.com/andreybmc/poolheat/edge/internal/paths"
	"github.com/andreybmc/poolheat/edge/internal/state"
)

// File IPC: serve enqueues device_req.json; poller executes via backends (tuya/tapo/…).
// serve never imports tinytuya.
const (
	deviceReqFile    = "device_req.json"
	deviceResultFile = "device_result.json"
)

// DeviceRequest is enqueued by serve for status (on=null) or set.
type DeviceRequest struct {
	ID       string `json:"id"`
	TS       float64 `json:"ts"`
	DeviceID string `json:"device_id"`
	// On is nil for status-only; true/false for set (logical ON).
	On     *bool  `json:"on"`
	Source string `json:"source,omitempty"`
	Force  bool   `json:"force,omitempty"`
}

// DeviceResult is written after handling DeviceRequest.
type DeviceResult struct {
	ID      string         `json:"id"`
	OK      bool           `json:"ok"`
	On      *bool          `json:"on,omitempty"` // logical on when known
	Error   string         `json:"error,omitempty"`
	TS      float64        `json:"ts"`
	Backend string         `json:"backend,omitempty"`
	Power   map[string]any `json:"power,omitempty"`
	Skipped bool           `json:"skipped,omitempty"`
	Reason  string         `json:"reason,omitempty"`
	Extra   map[string]any `json:"extra,omitempty"`
}

// ProcessPendingDeviceCmd handles one device_req.json if present.
// Returns true if a request was consumed.
func ProcessPendingDeviceCmd(ctx context.Context, dataDir string, store *device.Store) bool {
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

	res := executeDeviceCmd(ctx, dataDir, store, req)
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

	// status only
	if req.On == nil {
		cctx, cancel := context.WithTimeout(ctx, 8*time.Second)
		defer cancel()
		if store != nil {
			err := store.PollStatus(cctx, *cfg, src+":status")
			rt := store.ByID[cfg.ID]
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
			_ = state.Save(paths.DevicesState(dataDir), store.ByID)
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
		if br.On != nil {
			logical := backend.PhysicalToLogical(*br.On, cfg.Inverted)
			res.On = &logical
		}
		return res
	}

	// set logical
	cctx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	if store != nil {
		err := store.SetLogical(cctx, *cfg, *req.On, src, req.Force)
		rt := store.ByID[cfg.ID]
		if rt.LastOn != nil {
			res.On = rt.LastOn
		}
		if rt.LastPower != nil {
			res.Power = rt.LastPower
		}
		if err != nil {
			res.Error = err.Error()
			res.OK = false
			_ = state.Save(paths.DevicesState(dataDir), store.ByID)
			return res
		}
		res.OK = true
		res.Backend = cfg.BackendNorm()
		if res.On == nil {
			on := *req.On
			res.On = &on
		}
		_ = state.Save(paths.DevicesState(dataDir), store.ByID)
		return res
	}
	// no store — direct backend
	phys := backend.LogicalToPhysical(*req.On, cfg.Inverted)
	br, err := backend.Control(cctx, &phys, *cfg)
	if err != nil {
		res.Error = err.Error()
		return res
	}
	res.OK = true
	res.Backend = br.Backend
	res.Skipped = br.Skipped
	res.Reason = br.Reason
	res.Power = br.Power
	if br.On != nil {
		logical := backend.PhysicalToLogical(*br.On, cfg.Inverted)
		res.On = &logical
	} else {
		on := *req.On
		res.On = &on
	}
	return res
}
