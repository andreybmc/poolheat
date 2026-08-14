package miner

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// PublishLive writes live_cache.json (same shape as Python publish_live_snapshot).
func PublishLive(dataDir string, live map[string]any, host string, pid int) error {
	if live == nil {
		return nil
	}
	payload := map[string]any{
		"ts":   float64(time.Now().UnixNano()) / 1e9,
		"live": live,
		"host": host,
		"pid":  pid,
	}
	path := filepath.Join(dataDir, "live_cache.json")
	return writeJSONAtomic(path, payload)
}

// WriteMiningWork publishes resume|suspend for devices-poller.
// Merges into existing by_miner so fleet-live entries for other miners survive.
func WriteMiningWork(dataDir string, work string, source string) error {
	return WriteMiningWorkEx(dataDir, work, source, "", "")
}

// WriteMiningWorkEx updates top-level work and optionally by_miner[minerID].
func WriteMiningWorkEx(dataDir string, work string, source string, minerID string, host string) error {
	w := normWork(work)
	now := float64(time.Now().UnixNano()) / 1e9
	path := filepath.Join(dataDir, "mining_work.json")

	// merge existing
	prev := map[string]any{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, &prev)
	}
	by := map[string]any{}
	if rawBy, ok := prev["by_miner"].(map[string]any); ok {
		for k, v := range rawBy {
			if m, ok := v.(map[string]any); ok {
				by[k] = m
			}
		}
	}
	mid := strings.TrimSpace(minerID)
	if mid != "" && w != "" {
		by[mid] = map[string]any{
			"work": w,
			"ts":   now,
			"host": host,
		}
	}
	activeID := mid
	if activeID == "" {
		if s, ok := prev["active_miner_id"].(string); ok {
			activeID = strings.TrimSpace(s)
		}
	}
	payload := map[string]any{
		"work":            nil,
		"ts":              now,
		"source":          source,
		"active_miner_id": activeID,
		"by_miner":        by,
	}
	if w != "" {
		payload["work"] = w
	}
	return writeJSONAtomic(path, payload)
}

func normWork(work string) string {
	w := strings.ToLower(strings.TrimSpace(work))
	switch w {
	case "mining":
		w = "resume"
	case "sleep":
		w = "suspend"
	}
	if w != "resume" && w != "suspend" {
		return ""
	}
	return w
}

func writeJSONAtomic(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
