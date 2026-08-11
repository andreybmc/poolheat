// Package events appends to policy_events.json (UI Action log / system Events).
// Compatible with serve.py _devices_event_log / _policy_log merge semantics.
package events

import (
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

const defaultMax = 200

var mu sync.Mutex

// Event is one Action-log line (newest first in the file ring).
type Event struct {
	TS         string `json:"ts"`
	Kind       string `json:"kind"`
	Msg        string `json:"msg"`
	Source     string `json:"source,omitempty"`
	DeviceID   string `json:"device_id,omitempty"`
	Alias      string `json:"alias,omitempty"`
	Backend    string `json:"backend,omitempty"`
	Driver     string `json:"driver,omitempty"`
	DesiredOn  *bool  `json:"desired_on,omitempty"`
	ReportedOn *bool  `json:"reported_on,omitempty"`
}

type fileShape struct {
	Events []map[string]any `json:"events"`
	Max    int              `json:"max"`
}

// Append merges one event into path (policy_events.json), newest first, de-duped.
// Safe vs serve: both processes merge disk on write; serve reloads on mtime.
func Append(path string, kind, msg string, extra Event) {
	if path == "" {
		return
	}
	kind = strings.TrimSpace(kind)
	if kind == "" {
		kind = "device"
	}
	msg = clip(msg, 400)
	if msg == "" {
		return
	}

	ev := map[string]any{
		"ts":   time.Now().Format("2006-01-02T15:04:05"),
		"kind": kind,
		"msg":  msg,
	}
	if extra.Source != "" {
		ev["source"] = extra.Source
	}
	if extra.DeviceID != "" {
		ev["device_id"] = extra.DeviceID
	}
	if extra.Alias != "" {
		ev["alias"] = extra.Alias
	}
	if extra.Backend != "" {
		ev["backend"] = extra.Backend
	}
	if extra.Driver != "" {
		ev["driver"] = extra.Driver
	}
	if extra.DesiredOn != nil {
		ev["desired_on"] = *extra.DesiredOn
	}
	if extra.ReportedOn != nil {
		ev["reported_on"] = *extra.ReportedOn
	}

	mu.Lock()
	defer mu.Unlock()

	mx := defaultMax
	var disk fileShape
	if err := jsonutil.LoadJSON(path, &disk); err != nil {
		log.Printf("[devices] event log load: %v", err)
	}
	if disk.Max > 0 {
		mx = disk.Max
	}
	if mx < 20 {
		mx = 20
	}
	if mx > 5000 {
		mx = 5000
	}

	seen := map[string]struct{}{}
	merged := make([]map[string]any, 0, mx)
	for _, e := range append([]map[string]any{ev}, disk.Events...) {
		if e == nil {
			continue
		}
		key := str(e["ts"]) + "|" + str(e["kind"]) + "|" + str(e["msg"])
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		merged = append(merged, e)
		if len(merged) >= mx {
			break
		}
	}

	out := fileShape{Events: merged, Max: mx}
	if err := jsonutil.SaveAtomic(path, out); err != nil {
		log.Printf("[devices] event log save: %v", err)
		return
	}
	log.Printf("[devices] %s %s: %s", ev["ts"], kind, msg)
}

// AppendDevice is a short-hand for kind=device / err device restores.
func AppendDevice(dataDir, kind, msg string, extra Event) {
	if dataDir == "" {
		return
	}
	path := filepath.Join(dataDir, "policy_events.json")
	// ensure dir exists (usually already does)
	_ = os.MkdirAll(dataDir, 0o755)
	Append(path, kind, msg, extra)
}

func str(v any) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

func clip(s string, n int) string {
	s = strings.TrimSpace(s)
	if n <= 0 || utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n])
}
