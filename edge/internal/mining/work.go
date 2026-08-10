package mining

import (
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

// Read returns "resume" | "suspend" | "" if missing/stale.
func Read(path string, maxAgeSec float64) string {
	var raw map[string]any
	if err := jsonutil.LoadJSON(path, &raw); err != nil || raw == nil {
		return ""
	}
	ts, _ := raw["ts"].(float64)
	if ts <= 0 {
		return ""
	}
	age := float64(time.Now().UnixNano())/1e9 - ts
	if age > maxAgeSec {
		return ""
	}
	w := strings.ToLower(strings.TrimSpace(str(raw["work"])))
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
