package miner

import (
	"encoding/json"
	"os"
	"path/filepath"
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
func WriteMiningWork(dataDir string, work string, source string) error {
	w := work
	switch w {
	case "mining":
		w = "resume"
	case "sleep":
		w = "suspend"
	}
	if w != "resume" && w != "suspend" {
		w = ""
	}
	payload := map[string]any{
		"work":   nil,
		"ts":     float64(time.Now().UnixNano()) / 1e9,
		"source": source,
	}
	if w != "" {
		payload["work"] = w
	}
	return writeJSONAtomic(filepath.Join(dataDir, "mining_work.json"), payload)
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
