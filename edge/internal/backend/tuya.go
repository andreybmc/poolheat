package backend

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/backend/tuyalan"
	"github.com/andreybmc/poolheat/edge/internal/config"
)

func controlTuya(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	ip := strings.TrimSpace(cfg.IP)
	if ip == "" {
		return Result{}, fmt.Errorf("Tuya IP не настроен")
	}
	devID := strings.TrimSpace(cfg.DeviceID)
	if devID == "" {
		return Result{}, fmt.Errorf("Tuya device_id не настроен")
	}
	key := strings.TrimSpace(cfg.TuyaLocalKey)
	if key == "" {
		return Result{}, fmt.Errorf("Tuya: нужен local_key (обновите через UI login)")
	}
	verF := cfg.TuyaVersion
	if verF <= 0 {
		verF = 3.4
	}
	switchDPS := cfg.TuyaSwitchDPS
	if switchDPS <= 0 {
		if cfg.IsLight() {
			switchDPS = 20
		} else {
			switchDPS = 1
		}
	}
	timeout := 8 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		if rem := time.Until(dl); rem > time.Second && rem < timeout {
			timeout = rem
		}
	}

	run := func(ver float64, dpsN int) (Result, error) {
		dev := &tuyalan.Device{
			IP:      ip,
			ID:      devID,
			Key:     key,
			Version: ver,
			Timeout: timeout,
		}
		if on == nil {
			dps, err := dev.Status(ctx)
			if err != nil {
				return Result{}, err
			}
			cur := dpsBool(dps, dpsN)
			if cur == nil {
				if b := dpsBoolKey(dps, "20"); b != nil {
					cur = b
				}
			}
			out := Result{On: cur, Backend: "tuya", Extra: map[string]any{
				"dps": dps, "device_id": devID, "via": "go", "version": ver,
			}}
			if pm := tuyaDPSPower(dps); pm != nil {
				out.Power = pm
			}
			attachLightTelemetry(out.Extra, dps, cfg, nil)
			return out, nil
		}

		want := *on
		dpsMap := map[string]any{strconv.Itoa(dpsN): want}
		if cfg.SetBrightness != nil {
			raw := *cfg.SetBrightness
			if raw < 0 {
				raw = 0
			}
			if raw <= 100 {
				raw = raw * 10 // UI 0–100 % → Tuya white 0–1000
			}
			if raw > 1000 {
				raw = 1000
			}
			bdps := cfg.BrightDPS()
			if bdps <= 0 {
				bdps = 22
			}
			dpsMap[strconv.Itoa(bdps)] = raw
			// many bulbs ignore DPS 22 unless work mode is white
			if cfg.SetMode == nil && cfg.ModeDPS() > 0 {
				dpsMap[strconv.Itoa(cfg.ModeDPS())] = "white"
			}
		}
		if cfg.SetMode != nil && strings.TrimSpace(*cfg.SetMode) != "" {
			mode := strings.ToLower(strings.TrimSpace(*cfg.SetMode))
			if mode == "color" {
				mode = "colour"
			}
			mdps := cfg.ModeDPS()
			if mdps <= 0 {
				mdps = 21
			}
			dpsMap[strconv.Itoa(mdps)] = mode
		}
		var lastDPS map[string]any
		var lastErr error
		// bool first; some plugs want 1/0
		attempts := []map[string]any{dpsMap}
		alt := copyDPS(dpsMap)
		if want {
			alt[strconv.Itoa(dpsN)] = 1
		} else {
			alt[strconv.Itoa(dpsN)] = 0
		}
		attempts = append(attempts, alt)
		for i, payload := range attempts {
			dps, err := dev.SetDPS(ctx, payload)
			if err != nil {
				lastErr = err
				continue
			}
			lastDPS = dps
			if b := dpsBool(dps, dpsN); b != nil && *b == want {
				out := Result{On: b, Backend: "tuya", Extra: map[string]any{
					"dps": dps, "device_id": devID, "via": "go",
					"version": ver, "set_attempt": i + 1,
					"set_dps": payload,
				}}
				if pm := tuyaDPSPower(dps); pm != nil {
					out.Power = pm
				}
				attachLightTelemetry(out.Extra, dps, cfg, nil)
				// if re-read missed brightness, still echo what we sent
				if cfg.SetBrightness != nil && out.Extra["brightness_pct"] == nil {
					out.Extra["brightness_pct"] = *cfg.SetBrightness
				}
				return out, nil
			}
			lastErr = fmt.Errorf("after set dps=%v want=%v", payload, want)
		}
		got := false
		if b := dpsBool(lastDPS, dpsN); b != nil {
			got = *b
		}
		out := Result{On: &got, Backend: "tuya", Extra: map[string]any{
			"dps": lastDPS, "device_id": devID, "via": "go",
		}}
		if lastErr == nil {
			lastErr = fmt.Errorf("Tuya set failed")
		}
		return out, lastErr
	}

	out, err := run(verF, switchDPS)
	if err != nil && isTuyaProtoMismatch(err) && verF < 3.45 {
		// device is often 3.5 while UI still says 3.4
		if out2, err2 := run(3.5, switchDPS); err2 == nil {
			if out2.Extra == nil {
				out2.Extra = map[string]any{}
			}
			out2.Extra["version_auto"] = 3.5
			return out2, nil
		}
	}
	if err != nil && switchDPS == 1 && on != nil {
		if out2, err2 := run(verF, 20); err2 == nil {
			if out2.Extra == nil {
				out2.Extra = map[string]any{}
			}
			out2.Extra["tuya_switch_dps_auto"] = 20
			return out2, nil
		}
	}
	return out, err
}

func isTuyaProtoMismatch(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "eof") ||
		strings.Contains(s, "connection reset") ||
		strings.Contains(s, "i/o timeout") ||
		strings.Contains(s, "broken pipe") ||
		strings.Contains(s, "hmac") ||
		strings.Contains(s, "gcm") ||
		strings.Contains(s, "wrong key or version") ||
		strings.Contains(s, "unexpected payload")
}

func dpsBoolKey(dps map[string]any, key string) *bool {
	if dps == nil {
		return nil
	}
	v, ok := dps[key]
	if !ok || v == nil {
		return nil
	}
	switch t := v.(type) {
	case bool:
		b := t
		return &b
	case float64:
		if t == 0 || t == 1 {
			b := t == 1
			return &b
		}
	case int:
		if t == 0 || t == 1 {
			b := t == 1
			return &b
		}
	case int64:
		if t == 0 || t == 1 {
			b := t == 1
			return &b
		}
	case string:
		b := asBoolAny(t)
		return &b
	}
	return nil
}

func dpsBool(dps map[string]any, switchDPS int) *bool {
	if dps == nil {
		return nil
	}
	keys := []string{strconv.Itoa(switchDPS), "1", "20", "101", "102", "103", "2"}
	seen := map[string]bool{}
	for _, k := range keys {
		if k == "" || seen[k] {
			continue
		}
		seen[k] = true
		if b := dpsBoolKey(dps, k); b != nil {
			return b
		}
	}
	for k, v := range dps {
		if v == nil || k == "18" || k == "19" || k == "4" || k == "5" || k == "6" {
			continue
		}
		if b := dpsBoolKey(dps, k); b != nil {
			return b
		}
	}
	return nil
}

func attachLightTelemetry(extra map[string]any, dps map[string]any, cfg config.DeviceCfg, _ map[string]any) {
	if extra == nil || dps == nil {
		return
	}
	bdps := cfg.BrightDPS()
	if bdps > 0 {
		if n, ok := asFloatAny(dps[strconv.Itoa(bdps)]); ok {
			extra["brightness_raw"] = int(n)
			pct := int(n*100.0/1000.0 + 0.5)
			if pct < 0 {
				pct = 0
			}
			if pct > 100 {
				pct = 100
			}
			extra["brightness_pct"] = pct
		}
	}
	mdps := cfg.ModeDPS()
	if mdps > 0 {
		if m, ok := dps[strconv.Itoa(mdps)].(string); ok && m != "" {
			extra["mode"] = m
		}
	}
	if c, ok := dps["25"]; ok {
		extra["colour_data"] = c
	}
	if c, ok := dps["24"]; ok {
		extra["colour_data"] = c
	}
}

func copyDPS(in map[string]any) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func tuyaDPSPower(dps map[string]any) map[string]any {
	if dps == nil {
		return nil
	}
	getNum := func(k string) (float64, bool) {
		v, ok := dps[k]
		if !ok || v == nil {
			return 0, false
		}
		if _, isBool := v.(bool); isBool {
			return 0, false
		}
		return asFloatAny(v)
	}
	out := map[string]any{}
	if i, ok := getNum("18"); ok {
		out["current_a"] = round3(i / 1000)
	} else if i, ok := getNum("4"); ok {
		out["current_a"] = round3(i / 1000)
	}
	if p, ok := getNum("19"); ok {
		out["power_w"] = round2(p / 10)
	} else if p, ok := getNum("5"); ok {
		out["power_w"] = round2(p / 10)
	}
	if v, ok := getNum("20"); ok && v > 50 {
		out["voltage_v"] = round2(v / 10)
	} else if v, ok := getNum("6"); ok {
		out["voltage_v"] = round2(v / 10)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}
