package miner

import (
	"fmt"
	"log"
	"math"
	"strings"
	"time"

	"github.com/andreybmc/wm-lib/api"
)

// FetchLive polls Whatsminer public API (V2 :4028) and builds poolheat live JSON.
// Shape matches serve.py fetch_live() core fields used by UI / history / policy.
func FetchLive(s Settings) (map[string]any, error) {
	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = s.Password
	if c.Password == "" {
		c.Password = api.DefaultAdmin
	}
	c.Timeout = 8 * time.Second

	sumRaw, err := c.Summary()
	if err != nil {
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
	if pRaw, err := c.Read("get_psu", nil); err == nil {
		psu := extractPayload(pRaw, "PSU", "psu")
		if len(psu) == 0 {
			if m, ok := pRaw["Msg"].(map[string]any); ok {
				psu = m
			}
		}
		psuTemp = fPtr(psu["temp0"])
		psuFan = fPtr(psu["fan_speed"])
		psuPin = fPtr(psu["pin"])
		psuVin = fPtr(psu["vin"])
		if psuVin == nil {
			psuVin = fPtr(psu["Vin"])
		}
		psuIin = fPtr(psu["iin"])
		if psuIin == nil {
			psuIin = fPtr(psu["Iin"])
		}
		psuModel = firstNonEmpty(psu["model"], psu["name"])
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

	liquid := fPtr(status["liquid_temp"])
	if liquid == nil {
		liquid = fPtr(summary["Liquid Temp"])
	}
	if liquid == nil {
		liquid = fPtr(summary["liquid_temp"])
	}
	liquidSrc := any(nil)
	if liquid != nil {
		if status["liquid_temp"] != nil {
			liquidSrc = "status"
		} else {
			liquidSrc = "summary"
		}
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
		"liquid":               liquid,
		"liquid_source":        liquidSrc,
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

	// run_status simplified
	rs := runStatus(body)
	body["run_status"] = rs
	body["run_status_en"] = rs
	body["run_status_ru"] = rs

	// t_ctrl default liquid
	body["t_ctrl"] = liquid
	body["t_ctrl_sensor"] = "liquid"

	return body, nil
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
