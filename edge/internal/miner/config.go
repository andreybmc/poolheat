package miner

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Settings for the ASIC poller (from env + config.json).
type Settings struct {
	DataDir         string
	Host            string
	Port            int
	Password        string
	PollIntervalSec int
	HistoryEnabled  bool
	HistoryInterval int
	HistoryDB       string
	DryRun          bool
}

func DefaultSettings() Settings {
	return Settings{
		DataDir:         envOr("POOLHEAT_DATA", "/opt/var/poolheat"),
		Host:            envOr("POOLHEAT_MINER_HOST", "192.168.1.10"),
		Port:            envInt("POOLHEAT_MINER_PORT", 4028),
		Password:        envOr("POOLHEAT_API_PASSWORD", "admin"),
		PollIntervalSec: envInt("POOLHEAT_POLL_INTERVAL", 5),
		HistoryEnabled:  true,
		HistoryInterval: 30,
		DryRun:          false,
	}
}

// Load merges env defaults with /opt/etc/poolheat/config.json and history_config.json.
func LoadSettings() Settings {
	s := DefaultSettings()
	// app config
	for _, p := range []string{
		os.Getenv("POOLHEAT_CONFIG"),
		"/opt/etc/poolheat/config.json",
		filepath.Join(s.DataDir, "config.json"),
	} {
		if p == "" {
			continue
		}
		if applyAppConfig(p, &s) {
			break
		}
	}
	// history config under DATA
	histPath := filepath.Join(s.DataDir, "history_config.json")
	if b, err := os.ReadFile(histPath); err == nil {
		var h map[string]any
		if json.Unmarshal(b, &h) == nil {
			if v, ok := h["enabled"].(bool); ok {
				s.HistoryEnabled = v
			}
			if v, ok := asInt(h["sample_interval_sec"]); ok && v >= 5 {
				s.HistoryInterval = v
			}
		}
	}
	if s.HistoryDB == "" {
		s.HistoryDB = filepath.Join(s.DataDir, "history.db")
	}
	if s.PollIntervalSec < 2 {
		s.PollIntervalSec = 2
	}
	if s.PollIntervalSec > 300 {
		s.PollIntervalSec = 300
	}
	return s
}

func applyAppConfig(path string, s *Settings) bool {
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return false
	}
	if v := str(m["miner_host"]); v != "" {
		s.Host = v
	}
	if v, ok := asInt(m["miner_port"]); ok && v > 0 {
		s.Port = v
	}
	if v := str(m["api_password"]); v != "" {
		s.Password = v
	}
	if v, ok := asInt(m["poll_interval_sec"]); ok && v >= 2 {
		s.PollIntervalSec = v
	}
	if v, ok := m["dry_run"].(bool); ok {
		s.DryRun = v
	}
	return true
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	v := strings.TrimSpace(os.Getenv(k))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func str(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return strings.TrimSpace(s)
	}
	return strings.TrimSpace(strconv.FormatFloat(asFloat(v), 'f', -1, 64))
}

func asInt(v any) (int, bool) {
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(t))
		return n, err == nil
	case json.Number:
		i, err := t.Int64()
		return int(i), err == nil
	}
	return 0, false
}

func asFloat(v any) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case int:
		return float64(t)
	case string:
		f, _ := strconv.ParseFloat(t, 64)
		return f
	}
	return 0
}
