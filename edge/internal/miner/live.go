package miner

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/andreybmc/wm-lib/api"
)

// Liquid coolant (M63+ V3 :4433) — sticky so brief V3 timeouts do not show «н/д».
var (
	liquidCacheVal     *float64
	liquidCacheAt      time.Time
	liquidCacheSrc     string
	liquidLastV3Try    time.Time
	liquidRefreshEvery = 20 * time.Second // do not hammer :4433 every poll
	// Prefer stale reading over null for UI/control (sensor rarely jumps wildly).
	liquidStickyMaxAge = 30 * time.Minute
)

// FetchLive polls Whatsminer public API (V2 :4028 + V3 liquid) and builds
// poolheat live JSON. Shape matches serve.py fetch_live() core fields used by
// UI / history / policy. serve never talks to the ASIC for live — only this poller.
func FetchLive(s Settings) (map[string]any, error) {
	// Seed RAM cache from disk once per process (after OTA/restart).
	loadLiquidDiskCache(s.DataDir)

	var liquid *float64
	var liquidSrc any
	var v3Err string

	// Throttle V3: use last-good if fresh enough; only re-query :4433 periodically.
	needV3 := liquidCacheVal == nil || time.Since(liquidLastV3Try) >= liquidRefreshEvery
	if needV3 {
		liquidLastV3Try = time.Now()
		// Liquid early on :4433 — before several :4028 sessions (over max connect).
		liquid, liquidSrc, v3Err = fetchLiquidTempC(s.Host)
		if liquid != nil {
			rememberLiquid(s.DataDir, *liquid, fmt.Sprint(liquidSrc))
		}
	} else if liquidCacheVal != nil {
		v := *liquidCacheVal
		liquid = &v
		if liquidCacheSrc != "" {
			liquidSrc = liquidCacheSrc
		} else {
			liquidSrc = "cache"
		}
	}

	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = s.Password
	if c.Password == "" {
		c.Password = api.DefaultAdmin
	}
	c.Timeout = 8 * time.Second

	sumRaw, err := c.Summary()
	if err != nil {
		// Even if summary fails, keep publishing sticky liquid if we have it
		if liquid == nil {
			liquid, liquidSrc = stickyLiquid()
		}
		return nil, fmt.Errorf("summary: %w", err)
	}
	summary := extractPayload(sumRaw, "SUMMARY", "summary")

	status := map[string]any{}
	if stRaw, err := c.Read("status", nil); err == nil {
		status = extractPayload(stRaw, "STATUS", "status")
	} else {
		log.Printf("[miner-poller] status: %v", err)
	}

	devs := []map[string]any{}
	if dRaw, err := c.Read("devs", nil); err == nil {
		devs = extractDevs(dRaw)
	} else {
		log.Printf("[miner-poller] devs: %v", err)
	}

	// PSU optional
	var psuTemp, psuFan, psuPin, psuVin, psuIin *float64
	var psuModel any
	psuMap := map[string]any{}
	if pRaw, err := c.Read("get_psu", nil); err == nil {
		psuMap = extractPayload(pRaw, "PSU", "psu")
		if len(psuMap) == 0 {
			if m, ok := pRaw["Msg"].(map[string]any); ok {
				psuMap = m
			}
		}
		psuTemp = fPtr(psuMap["temp0"])
		psuFan = fPtr(psuMap["fan_speed"])
		psuPin = fPtr(psuMap["pin"])
		// get_psu Vin often ×100 (39200 → 392.0 V); Iin often mA integer
		psuVin = normalizePsuVin(firstNonEmpty(psuMap["vin"], psuMap["Vin"]))
		psuIin = normalizePsuIin(firstNonEmpty(psuMap["iin"], psuMap["Iin"]))
		psuModel = firstNonEmpty(psuMap["model"], psuMap["name"])
	}

	// Prefer V2 fields when present (rare on M63); else V3 / sticky.
	if v2liq, src := extractLiquidTemp(status, summary, psuMap, nil); v2liq != nil {
		liquid, liquidSrc = v2liq, src
		rememberLiquid(s.DataDir, *v2liq, fmt.Sprint(src))
	}
	// Second chance V3 if still empty (after V2 load may free :4433)
	if liquid == nil && needV3 {
		lt2, src2, err2 := fetchLiquidTempC(s.Host)
		if lt2 != nil {
			liquid, liquidSrc = lt2, src2
			rememberLiquid(s.DataDir, *lt2, fmt.Sprint(src2))
			v3Err = ""
		} else if err2 != "" {
			v3Err = err2
		}
	}
	if liquid == nil {
		liquid, liquidSrc = stickyLiquid()
		if liquid == nil && v3Err == "" {
			v3Err = "no liquid reading"
		}
	}

	// Temperature fallback from classic field
	if summary["Chip Temp Max"] == nil && summary["Temperature"] != nil {
		if t := fPtr(summary["Temperature"]); t != nil {
			summary["Chip Temp Max"] = *t
			summary["Chip Temp Avg"] = *t
			summary["Chip Temp Min"] = *t
		}
	}

	nBoards := 4
	if len(devs) > nBoards {
		nBoards = len(devs)
	}
	if nBoards < 3 {
		nBoards = 4 // M63 default
	}

	boards := make([]any, nBoards)
	boardChipMin := make([]any, nBoards)
	boardChipMax := make([]any, nBoards)
	boardChipAvg := make([]any, nBoards)
	upfreq := make([]int, nBoards)
	for i := 0; i < nBoards; i++ {
		if i < len(devs) {
			d := devs[i]
			boards[i] = fOrNil(d["Temperature"])
			boardChipMin[i] = fOrNil(d["Chip Temp Min"])
			boardChipMax[i] = fOrNil(d["Chip Temp Max"])
			boardChipAvg[i] = fOrNil(d["Chip Temp Avg"])
			upfreq[i] = int(asF(d["Upfreq Complete"]))
		} else {
			boards[i] = nil
			boardChipMin[i] = nil
			boardChipMax[i] = nil
			boardChipAvg[i] = nil
			upfreq[i] = 0
		}
	}

	mode := firstNonEmpty(summary["Power Mode"], status["power_mode"])
	modeStr := ""
	if mode != nil {
		modeStr = strings.TrimSpace(fmt.Sprint(mode))
	}
	modeNorm := strings.ToLower(modeStr)

	// hashrate TH/s
	mhs := firstNonEmpty(summary["HS RT"], summary["MHS 1m"], summary["MHS av"])
	var hashrateTH *float64
	if mhs != nil {
		if v := asF(mhs); v > 0 {
			th := v / 1_000_000.0
			hashrateTH = &th
		}
	}

	hs := summary["Hash Stable"]
	hsInt := 0
	if strings.ToLower(fmt.Sprint(hs)) == "true" || fmt.Sprint(hs) == "1" {
		hsInt = 1
	}

	power := fOrNil(summary["Power"])
	powerLimit := fOrNil(summary["Power Limit"])
	powerLimitSet := fOrNil(status["power_limit_set"])

	// hash percent
	var hashPct *float64
	if hp := status["hash_percent"]; hp != nil {
		if v := asF(hp); !math.IsNaN(v) {
			hashPct = &v
		}
	}

	workMeasured := measuredWork(map[string]any{
		"mineroff":    status["mineroff"],
		"mode":        modeStr,
		"mode_norm":   modeNorm,
		"power":       power,
		"hashrate_th": hashrateTH,
	})

	body := map[string]any{
		"ok":                   true,
		"ts":                   time.Now().Format("2006-01-02T15:04:05"),
		"host":                 fmt.Sprintf("%s:%d", s.Host, s.Port),
		"liquid":               ptrVal(liquid),
		"liquid_source":        liquidSrc,
		"liquid_v3_error":      nilIfEmpty(v3Err),
		"env":                  fOrNil(summary["Env Temp"]),
		"chip_min":             fOrNil(summary["Chip Temp Min"]),
		"chip_avg":             fOrNil(summary["Chip Temp Avg"]),
		"chip_max":             fOrNil(summary["Chip Temp Max"]),
		"boards":               boards,
		"board_chip_min":       boardChipMin,
		"board_chip_max":       boardChipMax,
		"board_chip_avg":       boardChipAvg,
		"upfreq":               upfreq,
		"board_count":          nBoards,
		"board_chart_slots":    []int{0, 2},
		"power":                power,
		"mode":                 modeStr,
		"mode_norm":            modeNorm,
		"mineroff":             status["mineroff"],
		"mineroff_reason":      status["mineroff_reason"],
		"power_limit":          powerLimit,
		"power_limit_set":      powerLimitSet,
		"power_pct_reported":   hashPct,
		"work_measured":        workMeasured,
		"mode_measured":        modeNorm,
		"power_limit_measured": firstNonNil(powerLimitSet, powerLimit),
		"freq_avg":             fOrNil(summary["freq_avg"]),
		"hashrate_th":          hashrateTH,
		"mhs_rt":               summary["HS RT"],
		"mhs_1m":               summary["MHS 1m"],
		"mhs_av":               summary["MHS av"],
		"hash_stable":          summary["Hash Stable"],
		"hash_stable_i":        hsInt,
		"elapsed":              summary["Elapsed"],
		"uptime":               summary["Uptime"],
		"psu_temp":             psuTemp,
		"psu_fan":              psuFan,
		"psu_pin":              psuPin,
		"psu_vin":              psuVin,
		"psu_iin":              psuIin,
		"psu_model":            psuModel,
		"inlet": firstNonNil(
			fOrNil(summary["Inlet Temp"]),
			fOrNil(summary["inlet_temp"]),
			fOrNil(status["inlet_temp"]),
		),
		"outlet": firstNonNil(
			fOrNil(summary["Outlet Temp"]),
			fOrNil(summary["outlet_temp"]),
			fOrNil(status["outlet_temp"]),
		),
		"miner_errors": []any{},
		"miner_events": []any{},
		"dry_run":      s.DryRun,
		"source":       "go-miner-poller",
	}

	// Temperature sensors catalog (UI panel "Temperature sensors")
	body["temps"] = buildTempCatalog(
		summary, status, liquid, liquidSrc,
		boards, boardChipMin, boardChipMax, boardChipAvg,
		psuTemp, nBoards,
	)

	// run_status simplified
	rs := runStatus(body)
	body["run_status"] = rs
	body["run_status_en"] = rs
	body["run_status_ru"] = rs

	// t_ctrl: prefer liquid when present, else chip_avg
	if liquid != nil {
		body["t_ctrl"] = liquid
		body["t_ctrl_sensor"] = "liquid"
	} else {
		body["t_ctrl"] = fOrNil(summary["Chip Temp Avg"])
		body["t_ctrl_sensor"] = "chip_avg"
	}

	return body, nil
}

// fetchLiquidTempC reads M63+ coolant via wm-lib V3 (TCP :4433), with retries.
func fetchLiquidTempC(host string) (*float64, any, string) {
	v3 := api.NewV3(host)
	v3.Timeout = 5 * time.Second
	var lastErr string
	for attempt := 0; attempt < 2; attempt++ {
		if attempt > 0 {
			time.Sleep(400 * time.Millisecond)
		}
		// Prefer DeviceInfoMsg so we can parse more field aliases than LiquidTempC.
		msg, err := v3.DeviceInfoMsg("power")
		if err != nil {
			msg, err = v3.DeviceInfoMsg(nil)
		}
		if err != nil {
			// fallback thin helper
			lt, err2 := v3.LiquidTempC()
			if err2 != nil {
				lastErr = err2.Error()
				log.Printf("[miner-poller] v3 liquid host=%s attempt=%d: %v", host, attempt+1, err2)
				continue
			}
			if lt != nil {
				return lt, "v3", ""
			}
			lastErr = err.Error()
			log.Printf("[miner-poller] v3 liquid host=%s attempt=%d: %v", host, attempt+1, err)
			continue
		}
		if lt, src := extractLiquidTemp(nil, nil, nil, msg); lt != nil {
			if src == nil || fmt.Sprint(src) == "" {
				src = "v3"
			}
			return lt, src, ""
		}
		// also try LiquidTempC parse path
		if lt, err2 := v3.LiquidTempC(); err2 == nil && lt != nil {
			return lt, "v3", ""
		}
		lastErr = "v3 power has no liquid-temperature field"
	}
	return nil, nil, lastErr
}

func rememberLiquid(dataDir string, v float64, src string) {
	if v <= 0.05 || v >= 150 {
		return
	}
	vv := v
	liquidCacheVal = &vv
	liquidCacheAt = time.Now()
	if src == "" {
		src = "v3"
	}
	liquidCacheSrc = src
	saveLiquidDiskCache(dataDir, v, src)
}

func stickyLiquid() (*float64, any) {
	if liquidCacheVal == nil {
		return nil, nil
	}
	if time.Since(liquidCacheAt) > liquidStickyMaxAge {
		return nil, nil
	}
	v := *liquidCacheVal
	src := liquidCacheSrc
	if src == "" {
		src = "cache"
	} else if time.Since(liquidCacheAt) > liquidRefreshEvery {
		src = "cache"
	}
	return &v, src
}

func liquidDiskPath(dataDir string) string {
	return filepath.Join(dataDir, "liquid_last.json")
}

func loadLiquidDiskCache(dataDir string) {
	if liquidCacheVal != nil || dataDir == "" {
		return
	}
	b, err := os.ReadFile(liquidDiskPath(dataDir))
	if err != nil {
		return
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return
	}
	v := asF(m["value"])
	if v <= 0.05 || v >= 150 {
		return
	}
	// age from ts if present
	ageOK := true
	if ts, ok := m["unix"].(float64); ok && ts > 0 {
		age := time.Since(time.Unix(int64(ts), 0))
		if age > liquidStickyMaxAge {
			ageOK = false
		} else {
			liquidCacheAt = time.Unix(int64(ts), 0)
		}
	} else {
		liquidCacheAt = time.Now().Add(-liquidRefreshEvery)
	}
	if !ageOK {
		return
	}
	vv := v
	liquidCacheVal = &vv
	if s, ok := m["source"].(string); ok && s != "" {
		liquidCacheSrc = s
	} else {
		liquidCacheSrc = "disk"
	}
	log.Printf("[miner-poller] liquid disk cache: %.1f°C src=%s", v, liquidCacheSrc)
}

func saveLiquidDiskCache(dataDir string, v float64, src string) {
	if dataDir == "" {
		return
	}
	payload := map[string]any{
		"value":  round2(v),
		"source": src,
		"unix":   float64(time.Now().Unix()),
		"ts":     time.Now().Format(time.RFC3339),
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return
	}
	_ = os.WriteFile(liquidDiskPath(dataDir), b, 0o644)
}

// extractLiquidTemp mirrors serve.py _extract_liquid_temp.
// Returns value + source label (status|summary|psu|v3).
func extractLiquidTemp(status, summary, psu, v3msg map[string]any) (*float64, any) {
	type cand struct {
		v   any
		src string
	}
	cands := []cand{
		{status["liquid_temp"], "status"},
		{status["Liquid Temp"], "status"},
		{status["liquid_temperature"], "status"},
		{status["coolant_temp"], "status"},
		{summary["Liquid Temp"], "summary"},
		{summary["liquid_temp"], "summary"},
		{summary["Coolant Temp"], "summary"},
		{psu["liquid_temp"], "psu"},
		{psu["liquid-temperature"], "psu"},
		{psu["temp_liquid"], "psu"},
	}
	if v3msg != nil {
		power, _ := v3msg["power"].(map[string]any)
		if power == nil {
			// some FW nest under Power
			power, _ = v3msg["Power"].(map[string]any)
		}
		if power != nil {
			cands = append(cands,
				cand{power["liquid-temperature"], "v3"},
				cand{power["liquid_temperature"], "v3"},
				cand{power["liquid_temp"], "v3"},
				cand{power["coolant"], "v3"},
				cand{power["Coolant Temp"], "v3"},
			)
		}
		cands = append(cands,
			cand{v3msg["liquid_temp"], "v3"},
			cand{v3msg["liquid-temperature"], "v3"},
		)
	}
	for _, c := range cands {
		if p := fPtr(c.v); p != nil && *p > 0.05 && *p < 150 {
			return p, c.src
		}
	}
	return nil, nil
}

// normalizePsuVin: get_psu often reports centivolts (39200 → 392.0 V).
func normalizePsuVin(raw any) *float64 {
	if raw == nil {
		return nil
	}
	v := asF(raw)
	if v < 0 {
		return nil
	}
	if v > 1000 {
		r := round2(v / 100.0)
		return &r
	}
	r := round2(v)
	return &r
}

// normalizePsuIin: get_psu integer mA ("12515") → A; floats already amps.
func normalizePsuIin(raw any) *float64 {
	if raw == nil {
		return nil
	}
	v := asF(raw)
	if v < 0 {
		return nil
	}
	s := strings.TrimSpace(fmt.Sprint(raw))
	s = strings.ReplaceAll(s, ",", ".")
	// integer / no decimal → milliamps from get_psu
	if s != "" && !strings.Contains(s, ".") && v >= 1 {
		r := round3(v / 1000.0)
		return &r
	}
	r := round3(v)
	return &r
}

func round2(v float64) float64 { return math.Round(v*100) / 100 }
func round3(v float64) float64 { return math.Round(v*1000) / 1000 }

func ptrVal(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// buildTempCatalog mirrors serve.py _build_temp_sensors_catalog (core rows).
func buildTempCatalog(
	summary, status map[string]any,
	liquid *float64, liquidSrc any,
	boards, boardChipMin, boardChipMax, boardChipAvg []any,
	psuTemp *float64,
	nBoards int,
) []map[string]any {
	out := make([]map[string]any, 0, 16)
	add := func(id, group, label, labelRU string, value any, source string, expect bool) {
		v := fPtr(value)
		if v != nil && (*v <= 0.05 || *v > 150) {
			v = nil
		}
		available := v != nil
		if !available && !expect {
			return
		}
		var val any
		if v != nil {
			val = round2(*v)
		}
		out = append(out, map[string]any{
			"id":        id,
			"group":     group,
			"label":     label,
			"label_ru":  labelRU,
			"value":     val,
			"unit":      "°C",
			"source":    source,
			"available": available,
		})
	}

	add("env", "ambient", "Env Temp", "Окружающая (Env)", summary["Env Temp"], "summary", true)
	src := "—"
	if liquidSrc != nil {
		src = fmt.Sprint(liquidSrc)
	}
	// liquid expected on hydro family; list even if missing so UI shows n/a
	add("liquid", "coolant", "Liquid / coolant", "Жидкость / теплоноситель", liquid, src, true)

	for _, key := range []struct{ k, id, lab, labRU string }{
		{"Inlet Temp", "inlet", "Inlet Temp", "Вход (Inlet)"},
		{"Outlet Temp", "outlet", "Outlet Temp", "Выход (Outlet)"},
		{"inlet_temp", "inlet", "Inlet Temp", "Вход (Inlet)"},
		{"outlet_temp", "outlet", "Outlet Temp", "Выход (Outlet)"},
	} {
		raw := summary[key.k]
		if raw == nil {
			raw = status[key.k]
		}
		if raw != nil {
			add(key.id, "coolant", key.lab, key.labRU, raw, "summary/status", true)
		}
	}

	add("chip_min", "chip", "Chip Temp Min", "Чипы min", summary["Chip Temp Min"], "summary", true)
	add("chip_avg", "chip", "Chip Temp Avg", "Чипы avg", summary["Chip Temp Avg"], "summary", true)
	add("chip_max", "chip", "Chip Temp Max", "Чипы max", summary["Chip Temp Max"], "summary", true)

	// Per-slot PCB
	for i := 0; i < nBoards; i++ {
		var pcb any
		if i < len(boards) {
			pcb = boards[i]
		}
		add(fmt.Sprintf("sm%d_pcb", i), "board",
			fmt.Sprintf("SM%d PCB", i), fmt.Sprintf("SM%d PCB", i),
			pcb, "devs", true)
		// chip min/avg/max only when DEVS provides real values
		slotOK := func(v any) bool {
			p := fPtr(v)
			return p != nil && *p > 0.05 && *p <= 150
		}
		var cmin, cavg, cmax any
		if i < len(boardChipMin) {
			cmin = boardChipMin[i]
		}
		if i < len(boardChipAvg) {
			cavg = boardChipAvg[i]
		}
		if i < len(boardChipMax) {
			cmax = boardChipMax[i]
		}
		if slotOK(cmin) || slotOK(cavg) || slotOK(cmax) {
			add(fmt.Sprintf("sm%d_chip_min", i), "board_chip",
				fmt.Sprintf("SM%d Chip Min", i), fmt.Sprintf("SM%d чип min", i),
				cmin, "devs", false)
			add(fmt.Sprintf("sm%d_chip_avg", i), "board_chip",
				fmt.Sprintf("SM%d Chip Avg", i), fmt.Sprintf("SM%d чип avg", i),
				cavg, "devs", false)
			add(fmt.Sprintf("sm%d_chip_max", i), "board_chip",
				fmt.Sprintf("SM%d Chip Max", i), fmt.Sprintf("SM%d чип max", i),
				cmax, "devs", false)
		}
	}

	add("psu", "psu", "PSU temp0", "БП temp0", psuTemp, "get_psu", true)

	// derived board max/min/avg
	pcbVals := []float64{}
	for _, b := range boards {
		if p := fPtr(b); p != nil {
			pcbVals = append(pcbVals, *p)
		}
	}
	if len(pcbVals) > 0 {
		mn, mx, sum := pcbVals[0], pcbVals[0], 0.0
		for _, v := range pcbVals {
			if v < mn {
				mn = v
			}
			if v > mx {
				mx = v
			}
			sum += v
		}
		add("board_max", "board", "Boards max (PCB)", "Платы max (PCB)", mx, "derived", true)
		add("board_min", "board", "Boards min (PCB)", "Платы min (PCB)", mn, "derived", true)
		add("board_avg", "board", "Boards avg (PCB)", "Платы avg (PCB)", sum/float64(len(pcbVals)), "derived", true)
	}

	return out
}

func measuredWork(live map[string]any) string {
	mo := strings.ToLower(strings.TrimSpace(fmt.Sprint(live["mineroff"])))
	h := asF(live["hashrate_th"])
	p := asF(live["power"])
	hashing := h >= 1.0

	if mo == "true" || mo == "1" || mo == "yes" {
		if hashing {
			return "resume"
		}
		return "suspend"
	}
	if mo == "false" || mo == "0" || mo == "no" {
		return "resume"
	}
	mode := strings.ToLower(fmt.Sprint(live["mode_norm"]))
	if mode == "" {
		mode = strings.ToLower(fmt.Sprint(live["mode"]))
	}
	if strings.Contains(mode, "sleep") || mode == "off" || mode == "power_off" {
		if hashing {
			return "resume"
		}
		return "suspend"
	}
	if hashing {
		return "resume"
	}
	if p >= 200 {
		return "resume"
	}
	if p > 0 && p < 50 && !hashing {
		return "suspend"
	}
	if hashing {
		return "resume"
	}
	return "suspend"
}

func runStatus(live map[string]any) string {
	work := measuredWork(live)
	if work == "suspend" {
		return "stopped"
	}
	up := live["upfreq"]
	if arr, ok := up.([]int); ok {
		all := len(arr) > 0
		for _, v := range arr {
			if v == 0 {
				all = false
				break
			}
		}
		if !all {
			return "tuning"
		}
	}
	h := asF(live["hashrate_th"])
	if h < 1 {
		return "starting"
	}
	return "running"
}

// ── helpers ─────────────────────────────────────────────────────────────────

func extractPayload(raw map[string]any, keys ...string) map[string]any {
	if raw == nil {
		return map[string]any{}
	}
	for _, k := range keys {
		if v, ok := raw[k]; ok {
			switch t := v.(type) {
			case map[string]any:
				return t
			case []any:
				if len(t) > 0 {
					if m, ok := t[0].(map[string]any); ok {
						return m
					}
				}
			}
		}
	}
	// Msg dict
	if msg, ok := raw["Msg"].(map[string]any); ok {
		for _, k := range keys {
			if v, ok := msg[k]; ok {
				switch t := v.(type) {
				case map[string]any:
					return t
				case []any:
					if len(t) > 0 {
						if m, ok := t[0].(map[string]any); ok {
							return m
						}
					}
				}
			}
		}
		// sometimes Msg itself is the summary
		if _, has := msg["Power"]; has {
			return msg
		}
		if _, has := msg["Chip Temp Max"]; has {
			return msg
		}
	}
	return map[string]any{}
}

func extractDevs(raw map[string]any) []map[string]any {
	out := []map[string]any{}
	if raw == nil {
		return out
	}
	tryList := func(v any) {
		if arr, ok := v.([]any); ok {
			for _, item := range arr {
				if m, ok := item.(map[string]any); ok {
					out = append(out, m)
				}
			}
		}
	}
	tryList(raw["DEVS"])
	if len(out) == 0 {
		tryList(raw["devs"])
	}
	if len(out) == 0 {
		if msg, ok := raw["Msg"].(map[string]any); ok {
			tryList(msg["DEVS"])
			if len(out) == 0 {
				tryList(msg["devs"])
			}
		}
	}
	return out
}

func asF(v any) float64 {
	if v == nil {
		return 0
	}
	switch t := v.(type) {
	case float64:
		return t
	case int:
		return float64(t)
	case int64:
		return float64(t)
	case string:
		var f float64
		fmt.Sscanf(strings.TrimSpace(t), "%f", &f)
		return f
	case *float64:
		if t == nil {
			return 0
		}
		return *t
	}
	return 0
}

func fPtr(v any) *float64 {
	if v == nil {
		return nil
	}
	f := asF(v)
	// treat missing empty string as nil
	if s, ok := v.(string); ok && strings.TrimSpace(s) == "" {
		return nil
	}
	return &f
}

func fOrNil(v any) any {
	p := fPtr(v)
	if p == nil {
		return nil
	}
	return *p
}

func firstNonEmpty(vals ...any) any {
	for _, v := range vals {
		if v == nil {
			continue
		}
		if s, ok := v.(string); ok && strings.TrimSpace(s) == "" {
			continue
		}
		return v
	}
	return nil
}

func firstNonNil(vals ...any) any {
	for _, v := range vals {
		if v != nil {
			return v
		}
	}
	return nil
}
