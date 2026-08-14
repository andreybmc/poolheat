package state

import (
	"encoding/json"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

// Runtime is the per-device shadow (devices_state.json by_id).
type Runtime struct {
	LastOn      *bool          `json:"last_on"`
	DesiredOn   *bool          `json:"desired_on"`
	Online      *bool          `json:"online"`
	LastError   *string        `json:"last_error"`
	LastOkTS    *string        `json:"last_ok_ts"`
	LastAction  *string        `json:"last_action"`
	LastPower   map[string]any `json:"last_power"`
	LastPowerTS *string        `json:"last_power_ts"`
	// Lights / dimmers
	LastBrightness *int           `json:"last_brightness,omitempty"` // 0–100 %
	LastMode       *string        `json:"last_mode,omitempty"`       // white|colour|scene|music
	LastTelemetry  map[string]any `json:"last_telemetry,omitempty"`  // compact extras
}

type File struct {
	Version int                 `json:"version"`
	ByID    map[string]Runtime  `json:"by_id"`
}

func Load(path string) (map[string]Runtime, error) {
	var raw map[string]any
	if err := jsonutil.LoadJSON(path, &raw); err != nil {
		return map[string]Runtime{}, err
	}
	out := map[string]Runtime{}
	if raw == nil {
		return out, nil
	}
	byID, _ := raw["by_id"].(map[string]any)
	if byID == nil {
		if d, ok := raw["devices"].(map[string]any); ok {
			byID = d
		}
	}
	for id, v := range byID {
		m, ok := v.(map[string]any)
		if !ok || id == "" {
			continue
		}
		out[id] = parseRuntime(m)
	}
	return out, nil
}

func Save(path string, byID map[string]Runtime) error {
	payload := File{Version: 1, ByID: byID}
	if payload.ByID == nil {
		payload.ByID = map[string]Runtime{}
	}
	return jsonutil.SaveAtomic(path, payload)
}

func parseRuntime(m map[string]any) Runtime {
	r := Runtime{}
	if v, ok := m["last_on"]; ok && v != nil {
		b := asBool(v)
		r.LastOn = &b
	}
	if v, ok := m["desired_on"]; ok && v != nil {
		b := asBool(v)
		r.DesiredOn = &b
	}
	if v, ok := m["online"]; ok && v != nil {
		b := asBool(v)
		r.Online = &b
	}
	if v, ok := m["last_error"]; ok && v != nil {
		s := str(v)
		if s != "" {
			r.LastError = &s
		}
	}
	if v, ok := m["last_ok_ts"]; ok && v != nil {
		s := str(v)
		r.LastOkTS = &s
	}
	if v, ok := m["last_action"]; ok && v != nil {
		s := str(v)
		r.LastAction = &s
	}
	if v, ok := m["last_power"].(map[string]any); ok {
		r.LastPower = v
	}
	if v, ok := m["last_power_ts"]; ok && v != nil {
		s := str(v)
		r.LastPowerTS = &s
	}
	if v, ok := m["last_brightness"]; ok && v != nil {
		if n, ok := asInt(v); ok {
			r.LastBrightness = &n
		}
	}
	if v, ok := m["last_mode"]; ok && v != nil {
		s := str(v)
		if s != "" {
			r.LastMode = &s
		}
	}
	if v, ok := m["last_telemetry"].(map[string]any); ok {
		r.LastTelemetry = v
	}
	return r
}

func asInt(v any) (int, bool) {
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case int64:
		return int(t), true
	case json.Number:
		i, err := t.Int64()
		return int(i), err == nil
	default:
		return 0, false
	}
}

// DeadlinesFile holds suspend-off deadlines + sync throttle timestamps.
type DeadlinesFile struct {
	Deadlines map[string]float64 `json:"deadlines"`
	SyncTS    map[string]float64 `json:"sync_ts"`
	TS        float64            `json:"ts"`
}

func LoadDeadlines(path string) (deadlines, syncTS map[string]float64, err error) {
	var raw DeadlinesFile
	if err = jsonutil.LoadJSON(path, &raw); err != nil {
		return map[string]float64{}, map[string]float64{}, err
	}
	if raw.Deadlines == nil {
		raw.Deadlines = map[string]float64{}
	}
	if raw.SyncTS == nil {
		raw.SyncTS = map[string]float64{}
	}
	return raw.Deadlines, raw.SyncTS, nil
}

func SaveDeadlines(path string, deadlines, syncTS map[string]float64) error {
	return jsonutil.SaveAtomic(path, DeadlinesFile{
		Deadlines: deadlines,
		SyncTS:    syncTS,
		TS:        float64(time.Now().Unix()) + float64(time.Now().Nanosecond())/1e9,
	})
}

func NowISO() string {
	return time.Now().Format("2006-01-02T15:04:05")
}

func asBool(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case float64:
		return t != 0
	case string:
		return t == "1" || t == "true" || t == "True" || t == "yes"
	}
	return false
}

func str(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
