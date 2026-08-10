package miner

import (
	"encoding/json"
	"fmt"
	"html"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/andreybmc/wm-lib/web"
)

// File IPC: full chipmap always on disk; serve/UI only reads this JSON.
const (
	chipmapCacheFile = "chipmap_cache.json"
	chipmapCfgFile   = "chipmap_config.json"
	chipmapReqFile   = "chipmap_req.json"
	chipmapResultFile = "chipmap_result.json"
)

var (
	chipSlotHdrRE = regexp.MustCompile(`(?i)slot:\s*(\d+)\s*,\s*freq:\s*([\d.]+)\s*,\s*temp:\s*([\d.]+)`)
	chipLineRE    = regexp.MustCompile(
		`(?i)C(\d+)\s+freq:(\d+)\s+vol:(\d+)\s+temp:([\d.]+)\s+` +
			`nonce:(\d+)\s+err:(\d+)\s+crc:(\d+)\s+` +
			`x:(\d+)\s*/\s*(\d+)\s+repeat:(\d+)\s+` +
			`pct:\s*([\d.]+)\s*%\s*/\s*([\d.]+)\s*%`,
	)
	chipTextareaRE = regexp.MustCompile(`(?is)<textarea[^>]*id=["']syslog["'][^>]*>(.*?)</textarea>`)
	tagStripRE     = regexp.MustCompile(`(?s)<[^>]+>`)
)

// ChipmapCfg mirrors serve.py DEFAULT_CHIPMAP_CFG (subset used by poller).
type ChipmapCfg struct {
	Enabled         bool   `json:"enabled"`
	PollIntervalSec int    `json:"poll_interval_sec"`
	WebUser         string `json:"web_user"`
	WebPassword     string `json:"web_password"`
	WebScheme       string `json:"web_scheme"`
	VerifyTLS       bool   `json:"verify_tls"`
	PersistCache    bool   `json:"persist_cache"`
}

func defaultChipmapCfg() ChipmapCfg {
	return ChipmapCfg{
		Enabled:         true,
		PollIntervalSec: 30,
		WebUser:         "admin",
		WebPassword:     "",
		WebScheme:       "https",
		VerifyTLS:       false,
		PersistCache:    true,
	}
}

func loadChipmapCfg(dataDir string, apiPassword string) ChipmapCfg {
	cfg := defaultChipmapCfg()
	path := filepath.Join(dataDir, chipmapCfgFile)
	b, err := os.ReadFile(path)
	if err != nil {
		if cfg.WebPassword == "" {
			cfg.WebPassword = apiPassword
		}
		return cfg
	}
	var raw map[string]any
	if json.Unmarshal(b, &raw) != nil {
		return cfg
	}
	if v, ok := raw["enabled"].(bool); ok {
		cfg.Enabled = v
	}
	if v, ok := asInt(raw["poll_interval_sec"]); ok && v >= 10 {
		cfg.PollIntervalSec = v
	}
	if v := str(raw["web_user"]); v != "" {
		cfg.WebUser = v
	}
	if v, ok := raw["web_password"].(string); ok {
		cfg.WebPassword = v
	}
	if v := strings.ToLower(str(raw["web_scheme"])); v == "http" || v == "https" {
		cfg.WebScheme = v
	}
	if v, ok := raw["verify_tls"].(bool); ok {
		cfg.VerifyTLS = v
	}
	if v, ok := raw["persist_cache"].(bool); ok {
		cfg.PersistCache = v
	}
	if strings.TrimSpace(cfg.WebPassword) == "" {
		cfg.WebPassword = apiPassword
	}
	if cfg.PollIntervalSec < 10 {
		cfg.PollIntervalSec = 10
	}
	return cfg
}

// ChipmapState tracks last successful poll time for interval.
type ChipmapState struct {
	lastOK   time.Time
	lastTry  time.Time
}

// ProcessChipmapTick runs on-demand (chipmap_req.json) and/or interval scrape.
// Always writes a full ready JSON to chipmap_cache.json when persist is on.
func ProcessChipmapTick(s Settings, st *ChipmapState) {
	if st == nil {
		return
	}
	cfg := loadChipmapCfg(s.DataDir, s.Password)
	forceID := ""
	reqPath := filepath.Join(s.DataDir, chipmapReqFile)
	if b, err := os.ReadFile(reqPath); err == nil {
		_ = os.Remove(reqPath)
		var req map[string]any
		if json.Unmarshal(b, &req) == nil {
			forceID = str(req["id"])
		} else {
			forceID = "req"
		}
	}

	if !cfg.Enabled && forceID == "" {
		return
	}
	interval := time.Duration(cfg.PollIntervalSec) * time.Second
	if forceID == "" && !st.lastTry.IsZero() && time.Since(st.lastTry) < interval {
		return
	}

	st.lastTry = time.Now()
	out := fetchChipmap(s, cfg)
	if cfg.PersistCache || forceID != "" {
		if err := writeJSONAtomic(filepath.Join(s.DataDir, chipmapCacheFile), out); err != nil {
			log.Printf("[miner-poller] chipmap cache: %v", err)
		} else if out["ok"] == true {
			st.lastOK = time.Now()
			log.Printf("[miner-poller] chipmap ok chips=%v boards=%v ms=%v",
				out["chip_count"], out["board_count"], out["fetch_ms"])
		} else {
			log.Printf("[miner-poller] chipmap: %v", out["error"])
		}
	}
	if forceID != "" {
		res := map[string]any{
			"id": forceID,
			"ok": out["ok"] == true,
			"ts": float64(time.Now().UnixNano()) / 1e9,
		}
		if out["ok"] != true {
			res["error"] = out["error"]
		} else {
			res["chip_count"] = out["chip_count"]
			res["board_count"] = out["board_count"]
		}
		_ = writeJSONAtomic(filepath.Join(s.DataDir, chipmapResultFile), res)
	}
}

func fetchChipmap(s Settings, cfg ChipmapCfg) map[string]any {
	t0 := time.Now()
	// Suspend: miner not hashing — keep last good map, mark suspend
	if suspended, detail := chipmapIsSuspended(s.DataDir); suspended {
		out := loadChipmapDisk(s.DataDir)
		if out == nil {
			out = map[string]any{
				"ok":         true,
				"reason":     "suspend",
				"message":    "miner not hashing · " + detail,
				"boards":     []any{},
				"chip_count": 0,
				"board_count": 0,
			}
		} else {
			out = copyMap(out)
			out["ok"] = true
			out["reason"] = "suspend"
			out["stale"] = true
			out["message"] = "last chip map from disk · miner not hashing · " + detail
		}
		out["ts"] = time.Now().Format("2006-01-02T15:04:05")
		out["fetch_ms"] = int(time.Since(t0).Milliseconds())
		out["source"] = "live_cache:suspend"
		out["boards_in_ram"] = false
		out["persisted"] = true
		return out
	}

	base := fmt.Sprintf("%s://%s", cfg.WebScheme, s.Host)
	lc := web.NewLuCI(base)
	lc.Username = cfg.WebUser
	if lc.Username == "" {
		lc.Username = "admin"
	}
	lc.Password = cfg.WebPassword
	if lc.Password == "" {
		lc.Password = s.Password
	}
	if lc.Password == "" {
		lc.Password = "admin"
	}
	lc.VerifyTLS = cfg.VerifyTLS
	lc.Timeout = 15 * time.Second

	if err := lc.Login(); err != nil {
		return chipmapError(s.DataDir, t0, fmt.Sprintf("luci login: %v", err))
	}
	code, body, err := lc.Get("/cgi-bin/luci/admin/status/btminerapi")
	if err != nil {
		return chipmapError(s.DataDir, t0, fmt.Sprintf("btminerapi: %v", err))
	}
	if code != 200 {
		return chipmapError(s.DataDir, t0, fmt.Sprintf("btminerapi HTTP %d", code))
	}
	htmlBody := string(body)
	logText := ""
	if m := chipTextareaRE.FindStringSubmatch(htmlBody); len(m) > 1 {
		logText = html.UnescapeString(m[1])
	} else {
		logText = tagStripRE.ReplaceAllString(htmlBody, "\n")
	}
	parsed := parseChipmapLog(logText)
	chipCount, _ := parsed["chip_count"].(int)
	if chipCount == 0 {
		// re-check suspend after empty parse
		if suspended, detail := chipmapIsSuspended(s.DataDir); suspended {
			out := loadChipmapDisk(s.DataDir)
			if out == nil {
				out = map[string]any{
					"ok": true, "reason": "suspend", "boards": []any{},
					"chip_count": 0, "board_count": 0,
					"message": "empty chip log · " + detail,
				}
			} else {
				out = copyMap(out)
				out["ok"] = true
				out["reason"] = "suspend"
				out["stale"] = true
				out["message"] = "empty chip log · miner not hashing"
			}
			out["ts"] = time.Now().Format("2006-01-02T15:04:05")
			out["fetch_ms"] = int(time.Since(t0).Milliseconds())
			out["source"] = "luci:empty-suspend"
			return out
		}
		return chipmapError(s.DataDir, t0,
			"no chip lines parsed — check web password / page content (or miner not hashing yet)")
	}

	out := map[string]any{
		"ok":            true,
		"reason":        nil,
		"message":       nil,
		"error":         nil,
		"ts":            time.Now().Format("2006-01-02T15:04:05"),
		"fetch_ms":      int(time.Since(t0).Milliseconds()),
		"source":        "luci:btminerapi",
		"host":          base,
		"boards_in_ram": false,
		"persisted":     true,
	}
	for k, v := range parsed {
		out[k] = v
	}
	// attach hashrate estimates from live_cache if present
	attachChipmapHashEstimates(s.DataDir, out)
	return out
}

func chipmapError(dataDir string, t0 time.Time, errMsg string) map[string]any {
	ms := int(time.Since(t0).Milliseconds())
	if suspended, _ := chipmapIsSuspended(dataDir); suspended {
		out := loadChipmapDisk(dataDir)
		if out == nil {
			out = map[string]any{"ok": true, "boards": []any{}, "chip_count": 0}
		} else {
			out = copyMap(out)
		}
		out["ok"] = true
		out["reason"] = "suspend"
		out["stale"] = true
		out["message"] = "last map · suspend · " + errMsg
		out["error"] = nil
		out["fetch_ms"] = ms
		out["ts"] = time.Now().Format("2006-01-02T15:04:05")
		return out
	}
	disk := loadChipmapDisk(dataDir)
	out := map[string]any{
		"ok":       false,
		"reason":   "error",
		"error":    errMsg,
		"fetch_ms": ms,
		"ts":       time.Now().Format("2006-01-02T15:04:05"),
		"message":  nil,
	}
	if disk != nil {
		// keep last good boards for UI
		for _, k := range []string{"boards", "chip_count", "board_count", "temp_min", "temp_max", "temp_avg", "nonce_total"} {
			if v, ok := disk[k]; ok {
				out[k] = v
			}
		}
		out["stale"] = true
		out["last_good_ts"] = disk["ts"]
		out["message"] = "last map (stale) · " + truncStr(errMsg, 120)
	} else {
		out["boards"] = []any{}
		out["chip_count"] = 0
		out["board_count"] = 0
	}
	out["boards_in_ram"] = false
	return out
}

func chipmapIsSuspended(dataDir string) (bool, string) {
	path := filepath.Join(dataDir, "live_cache.json")
	b, err := os.ReadFile(path)
	if err != nil {
		return false, "no live_cache"
	}
	var raw map[string]any
	if json.Unmarshal(b, &raw) != nil {
		return false, "bad live_cache"
	}
	live, _ := raw["live"].(map[string]any)
	if live == nil {
		return false, "empty live"
	}
	if live["ok"] != true {
		return true, "live not ok"
	}
	work := strings.ToLower(str(firstNonEmpty(live["work_measured"], live["work"], live["work_state"])))
	if work == "sleep" || work == "suspend" {
		return true, "work=" + work
	}
	h := asF(live["hashrate_th"])
	mo := strings.ToLower(str(live["mineroff"]))
	if mo == "true" || mo == "1" || mo == "yes" {
		if h < 1.0 {
			return true, "mineroff"
		}
	}
	if h >= 0 && h < 0.5 {
		// very low hash may mean stop; only if also no power mode hashing signal
		pw := asF(live["power"])
		if pw < 50 {
			return true, "near-zero hash/power"
		}
	}
	return false, ""
}

func loadChipmapDisk(dataDir string) map[string]any {
	path := filepath.Join(dataDir, chipmapCacheFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil || m == nil {
		return nil
	}
	return m
}

func parseChipmapLog(text string) map[string]any {
	type chip struct {
		ID       int     `json:"id"`
		Freq     int     `json:"freq"`
		Vol      int     `json:"vol"`
		Temp     *float64 `json:"temp"`
		Nonce    int     `json:"nonce"`
		Err      int     `json:"err"`
		CRC      int     `json:"crc"`
		X        int     `json:"x"`
		X2       int     `json:"x2"`
		Repeat   int     `json:"repeat"`
		Pct      *float64 `json:"pct"`
		Pct2     *float64 `json:"pct2"`
		HashShare float64 `json:"hash_share"`
	}
	type board struct {
		Slot      int     `json:"slot"`
		BoardFreq *float64 `json:"board_freq"`
		BoardTemp *float64 `json:"board_temp"`
		Chips     []chip  `json:"chips"`
		ChipCount int     `json:"chip_count"`
		TempMin   *float64 `json:"temp_min"`
		TempMax   *float64 `json:"temp_max"`
		TempAvg   *float64 `json:"temp_avg"`
		NonceSum  int     `json:"nonce_sum"`
	}

	boardsMap := map[int]*board{}
	var currentSlot *int
	var allTemps []float64

	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if m := chipSlotHdrRE.FindStringSubmatch(line); m != nil {
			slot, _ := strconv.Atoi(m[1])
			currentSlot = &slot
			bf := parseFPtr(m[2])
			bt := parseFPtr(m[3])
			if boardsMap[slot] == nil {
				boardsMap[slot] = &board{Slot: slot, BoardFreq: bf, BoardTemp: bt, Chips: []chip{}}
			} else {
				boardsMap[slot].BoardFreq = bf
				boardsMap[slot].BoardTemp = bt
			}
			continue
		}
		if m := chipLineRE.FindStringSubmatch(line); m != nil && currentSlot != nil {
			slot := *currentSlot
			if boardsMap[slot] == nil {
				boardsMap[slot] = &board{Slot: slot, Chips: []chip{}}
			}
			temp := parseFPtr(m[4])
			pct := parseFPtr(m[11])
			pct2 := parseFPtr(m[12])
			c := chip{
				ID:     atoi(m[1]),
				Freq:   atoi(m[2]),
				Vol:    atoi(m[3]),
				Temp:   temp,
				Nonce:  atoi(m[5]),
				Err:    atoi(m[6]),
				CRC:    atoi(m[7]),
				X:      atoi(m[8]),
				X2:     atoi(m[9]),
				Repeat: atoi(m[10]),
				Pct:    pct,
				Pct2:   pct2,
			}
			boardsMap[slot].Chips = append(boardsMap[slot].Chips, c)
			if temp != nil {
				allTemps = append(allTemps, *temp)
			}
		}
	}

	slots := make([]int, 0, len(boardsMap))
	for k := range boardsMap {
		slots = append(slots, k)
	}
	sort.Ints(slots)
	boards := make([]board, 0, len(slots))
	totalNonce := 0
	chipCount := 0
	for _, s := range slots {
		b := boardsMap[s]
		sort.Slice(b.Chips, func(i, j int) bool { return b.Chips[i].ID < b.Chips[j].ID })
		b.ChipCount = len(b.Chips)
		chipCount += b.ChipCount
		var cts []float64
		nsum := 0
		for _, c := range b.Chips {
			if c.Temp != nil {
				cts = append(cts, *c.Temp)
			}
			nsum += c.Nonce
		}
		b.NonceSum = nsum
		totalNonce += nsum
		if len(cts) > 0 {
			mn, mx, sum := cts[0], cts[0], 0.0
			for _, t := range cts {
				if t < mn {
					mn = t
				}
				if t > mx {
					mx = t
				}
				sum += t
			}
			avg := sum / float64(len(cts))
			b.TempMin, b.TempMax, b.TempAvg = &mn, &mx, &avg
		}
		for i := range b.Chips {
			if nsum > 0 {
				b.Chips[i].HashShare = round3(100.0 * float64(b.Chips[i].Nonce) / float64(nsum))
			}
		}
		boards = append(boards, *b)
	}

	out := map[string]any{
		"boards":      boards,
		"chip_count":  chipCount,
		"board_count": len(boards),
		"nonce_total": totalNonce,
	}
	if len(allTemps) > 0 {
		mn, mx, sum := allTemps[0], allTemps[0], 0.0
		for _, t := range allTemps {
			if t < mn {
				mn = t
			}
			if t > mx {
				mx = t
			}
			sum += t
		}
		avg := sum / float64(len(allTemps))
		out["temp_min"] = mn
		out["temp_max"] = mx
		out["temp_avg"] = avg
	} else {
		out["temp_min"] = nil
		out["temp_max"] = nil
		out["temp_avg"] = nil
	}
	return out
}

func attachChipmapHashEstimates(dataDir string, payload map[string]any) {
	livePath := filepath.Join(dataDir, "live_cache.json")
	b, err := os.ReadFile(livePath)
	if err != nil {
		return
	}
	var raw map[string]any
	if json.Unmarshal(b, &raw) != nil {
		return
	}
	live, _ := raw["live"].(map[string]any)
	if live == nil {
		return
	}
	hr := asF(live["hashrate_th"])
	elapsed := asF(firstNonEmpty(live["elapsed"], live["Elapsed"]))
	if hr <= 0 {
		return
	}
	// boards may be []board from parse — re-marshal for generic chip mutation
	bb, err := json.Marshal(payload["boards"])
	if err != nil {
		return
	}
	var boardMaps []map[string]any
	if json.Unmarshal(bb, &boardMaps) != nil {
		return
	}
	nonceTotal := 0
	for _, bd := range boardMaps {
		chips, _ := bd["chips"].([]any)
		for _, c := range chips {
			cm, _ := c.(map[string]any)
			if cm == nil {
				continue
			}
			nonceTotal += int(asF(cm["nonce"]))
		}
	}
	if nonceTotal <= 0 {
		return
	}
	for _, bd := range boardMaps {
		chips, _ := bd["chips"].([]any)
		for _, c := range chips {
			cm, _ := c.(map[string]any)
			if cm == nil {
				continue
			}
			n := asF(cm["nonce"])
			share := n / float64(nonceTotal)
			cm["est_th"] = round3(hr * share)
			if elapsed > 0 {
				cm["nonce_rate"] = round3(n / elapsed)
			}
		}
	}
	payload["boards"] = boardMaps
	payload["hashrate_th"] = hr
	if elapsed > 0 {
		payload["elapsed"] = elapsed
	}
}

func parseFPtr(s string) *float64 {
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return nil
	}
	return &f
}

func atoi(s string) int {
	n, _ := strconv.Atoi(strings.TrimSpace(s))
	return n
}

func copyMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func truncStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
