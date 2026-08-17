package miner

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// PublishLive writes live_cache.json (deep-poll cache for this host).
// When live is OK (or fail), also merges this host into fleet_live.json so a
// multi-host poll timeout cannot stamp this peer offline while deep poll is fine.
func PublishLive(dataDir string, live map[string]any, host string, pid int) error {
	if live == nil {
		return nil
	}
	now := float64(time.Now().UnixNano()) / 1e9
	payload := map[string]any{
		"ts":   now,
		"live": live,
		"host": host,
		"pid":  pid,
	}
	path := filepath.Join(dataDir, "live_cache.json")
	if err := writeJSONAtomic(path, payload); err != nil {
		return err
	}
	// Sync fleet_live for recovery (best-effort). Also publish ok:false so
	// sticky offline can age out and host key switches (1.10 vs 195) stick.
	ok, _ := live["ok"].(bool)
	if !ok {
		// still merge fail stamp so UI sees current host offline, not stale peer
		hip := hostIP(host)
		if hip == "" {
			hip = hostIP(fmtSprint(live["host"]))
		}
		if hip != "" {
			_ = mergeFleetLiveHost(dataDir, hip, live, now)
		}
		return nil
	}
	hip := hostIP(host)
	if hip == "" {
		hip = hostIP(fmtSprint(live["host"]))
	}
	if hip == "" {
		return nil
	}
	_ = mergeFleetLiveHost(dataDir, hip, live, now)
	return nil
}

func fmtSprint(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func hostIP(host string) string {
	h := strings.TrimSpace(host)
	if h == "" {
		return ""
	}
	// strip :port
	if i := strings.LastIndex(h, ":"); i > 0 {
		// avoid cutting IPv6; our hosts are IPv4 / names
		rest := h[i+1:]
		if rest != "" && strings.IndexFunc(rest, func(r rune) bool { return r < '0' || r > '9' }) < 0 {
			h = h[:i]
		}
	}
	return strings.TrimSpace(h)
}

func mergeFleetLiveHost(dataDir, host string, live map[string]any, now float64) error {
	path := filepath.Join(dataDir, "fleet_live.json")
	prev := map[string]any{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, &prev)
	}
	miners := map[string]any{}
	if raw, ok := prev["miners"].(map[string]any); ok {
		for k, v := range raw {
			miners[k] = v
		}
	}
	// drop very old entries (>10 min)
	for k, v := range miners {
		m, ok := v.(map[string]any)
		if !ok {
			delete(miners, k)
			continue
		}
		ts, _ := m["ts"].(float64)
		if ts > 0 && (now-ts) > 600 {
			delete(miners, k)
		}
	}
	miners[host] = map[string]any{
		"ts":   now,
		"live": live,
		"host": host,
	}
	payload := map[string]any{
		"ts":     now,
		"miners": miners,
	}
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
	// Keep active_miner_id as last-written mirror only (deprecated; by_miner is source of truth)
	lastID := mid
	if lastID == "" {
		if s, ok := prev["active_miner_id"].(string); ok {
			lastID = strings.TrimSpace(s)
		}
	}
	payload := map[string]any{
		"work":            nil,
		"ts":              now,
		"source":          source,
		"active_miner_id": lastID, // deprecated mirror
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
