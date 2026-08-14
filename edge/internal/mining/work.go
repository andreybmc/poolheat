package mining

import (
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

// Entry is per-miner work with timestamp.
type Entry struct {
	Work string
	TS   float64
	Host string
}

// Snapshot is mining_work.json (global + by_miner).
type Snapshot struct {
	Work          string
	TS            float64
	Source        string
	ActiveMinerID string
	ByMiner       map[string]Entry
}

// Read returns global "resume" | "suspend" | "" if missing/stale.
func Read(path string, maxAgeSec float64) string {
	snap := ReadAll(path, maxAgeSec)
	return snap.Work
}

// ReadAll loads full snapshot; empty Work/ByMiner when missing/stale.
func ReadAll(path string, maxAgeSec float64) Snapshot {
	out := Snapshot{ByMiner: map[string]Entry{}}
	var raw map[string]any
	if err := jsonutil.LoadJSON(path, &raw); err != nil || raw == nil {
		return out
	}
	now := float64(time.Now().UnixNano()) / 1e9
	out.Source = str(raw["source"])
	out.ActiveMinerID = strings.TrimSpace(str(raw["active_miner_id"]))
	ts, _ := raw["ts"].(float64)
	out.TS = ts
	if ts > 0 && (now-ts) <= maxAgeSec {
		out.Work = normWork(str(raw["work"]))
	}
	by, _ := raw["by_miner"].(map[string]any)
	if by == nil {
		return out
	}
	for k, v := range by {
		mid := strings.TrimSpace(k)
		if mid == "" {
			continue
		}
		m, ok := v.(map[string]any)
		if !ok {
			continue
		}
		ets, _ := m["ts"].(float64)
		if ets <= 0 || (now-ets) > maxAgeSec {
			continue
		}
		w := normWork(str(m["work"]))
		if w == "" {
			continue
		}
		out.ByMiner[mid] = Entry{
			Work: w,
			TS:   ets,
			Host: str(m["host"]),
		}
	}
	return out
}

// WorkForMiner returns resume|suspend for a bound miner, or "" if unbound/unknown/stale.
// Empty minerID means unbound → always "".
func WorkForMiner(snap Snapshot, minerID string) string {
	mid := strings.TrimSpace(minerID)
	if mid == "" {
		return ""
	}
	if e, ok := snap.ByMiner[mid]; ok && e.Work != "" {
		return e.Work
	}
	// fallback: active miner top-level work
	if snap.ActiveMinerID == mid && snap.Work != "" {
		return snap.Work
	}
	return ""
}

func normWork(w string) string {
	w = strings.ToLower(strings.TrimSpace(w))
	switch w {
	case "mining", "resume":
		return "resume"
	case "sleep", "suspend":
		return "suspend"
	}
	return ""
}

func str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
