package backend

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/rkosegi/tuya-proto/client"
	"github.com/rkosegi/tuya-proto/dto"
	"github.com/rkosegi/tuya-proto/proto"
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
	// Protocol 3.5+ (AES-GCM) — Go tuya-proto session is incomplete; use tinytuya.
	if verF >= 3.45 {
		return controlTuyaPython(ctx, on, cfg, verF)
	}
	ver := proto.Version34
	if verF > 0 && verF < 3.4 {
		ver = proto.Version31
	}
	switchDPS := cfg.TuyaSwitchDPS
	if switchDPS <= 0 {
		switchDPS = 1
	}

	// respect parent timeout if any
	timeout := 8 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		if rem := time.Until(dl); rem > time.Second && rem < timeout {
			timeout = rem
		}
	}

	// status: single session
	if on == nil {
		cl, err := tuyaConnect(ver, ip, key, timeout)
		if err != nil {
			// common: device is 3.5 but config still says 3.4 → EOF on handshake
			if isTuyaProtoMismatch(err) {
				return controlTuyaPython(ctx, on, cfg, 3.5)
			}
			return Result{}, err
		}
		defer cl.Close()
		dps, err := tuyaStatus(cl, ver, devID)
		if err != nil {
			if isTuyaProtoMismatch(err) {
				return controlTuyaPython(ctx, on, cfg, 3.5)
			}
			return Result{}, err
		}
		cur := dpsBool(dps, switchDPS)
		// Auto-hint light switch DPS 20 when configured 1 missing but 20 is bool
		if cur == nil {
			if b := dpsBoolKey(dps, "20"); b != nil {
				cur = b
			}
		}
		out := Result{On: cur, Backend: "tuya", Extra: map[string]any{"dps": dps, "device_id": devID}}
		if pm := tuyaDPSPower(dps); pm != nil {
			out.Power = pm
		}
		return out, nil
	}

	want := *on
	// Never skip on set: enforce_desired must re-send after external SmartLife toggle.
	dpsKey := strconv.Itoa(switchDPS)
	// Prefer bool (tinytuya default); if re-read disagrees, retry with 0/1 int.
	tryVals := []any{want}
	if want {
		tryVals = append(tryVals, 1)
	} else {
		tryVals = append(tryVals, 0)
	}

	var (
		got        bool
		lastDPS    map[string]any
		lastSetErr error
	)
	for i, val := range tryVals {
		// Fresh connect per attempt (tinytuya style; avoids stale session after CONTROL).
		cl, err := tuyaConnect(ver, ip, key, timeout)
		if err != nil {
			lastSetErr = err
			continue
		}
		// optional pre-status (ignore error — device may be busy)
		if dps0, err0 := tuyaStatus(cl, ver, devID); err0 == nil {
			lastDPS = dps0
		}

		cmd := proto.CmdIdTypeControlNew
		if ver == proto.Version31 {
			cmd = proto.CmdIdTypeControl
		}
		// tinytuya 3.4 CONTROL: payload = "3.4" + 12×0x00 + JSON
		// Without the version header the plug often ACKs but keeps old DPS.
		wire, err := tuyaControlWire(ver, devID, dpsKey, val)
		if err != nil {
			cl.Close()
			lastSetErr = err
			continue
		}
		if err := cl.Send(cmd, wire); err != nil {
			cl.Close()
			lastSetErr = fmt.Errorf("Tuya set: %w", err)
			continue
		}
		// CONTROL_NEW: empty ACK (retcode 0), then often STATUS push (cmd 8).
		// Drain up to 2 packets for the async DPS update before re-query.
		for drain := 0; drain < 2; drain++ {
			var msg map[string]any
			if err := cl.Read(&msg); err != nil {
				break
			}
			if d := extractDPS(msg); d != nil {
				lastDPS = d
				if b := dpsBool(d, switchDPS); b != nil && *b == want {
					got = *b
					cl.Close()
					out := Result{On: &got, Backend: "tuya", Extra: map[string]any{
						"dps": lastDPS, "device_id": devID,
						"set_attempt": i + 1, "set_val": fmt.Sprint(val), "via": "status_push",
					}}
					if pm := tuyaDPSPower(lastDPS); pm != nil {
						out.Power = pm
					}
					return out, nil
				}
			}
		}

		// Confirm with re-read; short settle then up to 3 queries.
		got = false
		confirmed := false
		for attempt := 0; attempt < 3; attempt++ {
			time.Sleep(time.Duration(250+attempt*150) * time.Millisecond)
			dps2, err2 := tuyaStatus(cl, ver, devID)
			if err2 != nil {
				cl.Close()
				cl2, errC := tuyaConnect(ver, ip, key, timeout)
				if errC != nil {
					lastSetErr = fmt.Errorf("after set: re-read connect: %w", errC)
					cl = nil
					break
				}
				cl = cl2
				dps2, err2 = tuyaStatus(cl, ver, devID)
				if err2 != nil {
					lastSetErr = fmt.Errorf("after set: re-read: %w", err2)
					continue
				}
			}
			lastDPS = dps2
			if b := dpsBool(dps2, switchDPS); b != nil {
				got = *b
				if got == want {
					confirmed = true
					break
				}
			}
		}
		if cl != nil {
			cl.Close()
		}
		if confirmed {
			out := Result{On: &got, Backend: "tuya", Extra: map[string]any{
				"dps": lastDPS, "device_id": devID,
				"set_attempt": i + 1, "set_val": fmt.Sprint(val),
			}}
			if pm := tuyaDPSPower(lastDPS); pm != nil {
				out.Power = pm
			}
			return out, nil
		}
		lastSetErr = fmt.Errorf("after set dps=%s val=%v still reported=%v want=%v", dpsKey, val, got, want)
	}
	// Handshake / protocol mismatch (often real device is 3.5) → tinytuya
	if lastSetErr != nil && isTuyaProtoMismatch(lastSetErr) {
		return controlTuyaPython(ctx, on, cfg, 3.5)
	}
	// Wrong switch DPS for lights (1 empty, real is 20)
	if lastSetErr != nil && switchDPS == 1 {
		cfg2 := cfg
		cfg2.TuyaSwitchDPS = 20
		if r2, e2 := controlTuyaPython(ctx, on, cfg2, verF); e2 == nil {
			if r2.Extra == nil {
				r2.Extra = map[string]any{}
			}
			r2.Extra["tuya_switch_dps_auto"] = 20
			return r2, nil
		}
	}
	out := Result{On: &got, Backend: "tuya", Extra: map[string]any{"dps": lastDPS, "device_id": devID}}
	if pm := tuyaDPSPower(lastDPS); pm != nil {
		out.Power = pm
	}
	if lastSetErr == nil {
		lastSetErr = fmt.Errorf("Tuya set failed")
	}
	return out, lastSetErr
}

func tuyaConnect(ver proto.Version, ip, key string, timeout time.Duration) (client.BlockingClient, error) {
	cl := client.NewBlockingClient(
		ver,
		ip,
		[]byte(key),
		client.WithTimeout(timeout),
		client.WithReadTimeout(timeout),
		client.WithWriteTimeout(timeout),
		client.WithLogger(slog.Default().With("backend", "tuya", "ip", ip)),
	)
	if err := cl.Connect(); err != nil {
		return nil, fmt.Errorf("Tuya connect %s: %w", ip, err)
	}
	return cl, nil
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
		strings.Contains(s, "unexpected payload") ||
		strings.Contains(s, "check device key or version")
}

// controlTuyaPython — tinytuya CLI for 3.5+ and handshake fallbacks.
func controlTuyaPython(ctx context.Context, on *bool, cfg config.DeviceCfg, verF float64) (Result, error) {
	ip := strings.TrimSpace(cfg.IP)
	devID := strings.TrimSpace(cfg.DeviceID)
	key := strings.TrimSpace(cfg.TuyaLocalKey)
	switchDPS := cfg.TuyaSwitchDPS
	if switchDPS <= 0 {
		switchDPS = 1
	}
	script := findTuyaLanCtl()
	if script == "" {
		return Result{}, fmt.Errorf(
			"Tuya v%.1f needs tinytuya helper (tuya_lan_ctl.py) — reinstall poolheat",
			verF,
		)
	}
	if switchDPS <= 0 {
		if cfg.IsLight() {
			switchDPS = 20
		} else {
			switchDPS = 1
		}
	}
	args := []string{
		script,
		"status",
		"--ip", ip,
		"--id", devID,
		"--key", key,
		"--version", fmt.Sprintf("%.1f", verF),
		"--dps", strconv.Itoa(switchDPS),
	}
	brightDPS := cfg.BrightDPS()
	if brightDPS > 0 {
		args = append(args, "--bright-dps", strconv.Itoa(brightDPS))
	}
	modeDPS := cfg.ModeDPS()
	if modeDPS > 0 {
		args = append(args, "--mode-dps", strconv.Itoa(modeDPS))
	}
	isSet := on != nil || cfg.SetBrightness != nil || cfg.SetMode != nil
	if isSet {
		args[1] = "set"
		if on != nil {
			args = append(args, "--on", strconv.FormatBool(*on))
		} else if cfg.SetBrightness != nil && *cfg.SetBrightness > 0 {
			// dimming without explicit on → turn on
			args = append(args, "--on", "true")
		}
		if cfg.SetBrightness != nil {
			args = append(args, "--brightness", strconv.Itoa(*cfg.SetBrightness))
		}
		if cfg.SetMode != nil && strings.TrimSpace(*cfg.SetMode) != "" {
			args = append(args, "--mode", strings.TrimSpace(*cfg.SetMode))
		}
	}
	// timeout from context
	timeout := 10 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		if rem := time.Until(dl); rem > 2*time.Second {
			timeout = rem
		}
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(cctx, "python3", args...)
	out, err := cmd.CombinedOutput()
	line := strings.TrimSpace(string(out))
	// last non-empty line should be JSON
	if i := strings.LastIndex(line, "\n"); i >= 0 {
		line = strings.TrimSpace(line[i+1:])
	}
	var resp map[string]any
	if jerr := json.Unmarshal([]byte(line), &resp); jerr != nil {
		if err != nil {
			return Result{}, fmt.Errorf("Tuya tinytuya: %w · %s", err, truncate(string(out), 160))
		}
		return Result{}, fmt.Errorf("Tuya tinytuya bad json: %s", truncate(string(out), 160))
	}
	if ok, _ := resp["ok"].(bool); !ok {
		msg, _ := resp["error"].(string)
		if msg == "" {
			msg = line
		}
		// auto-retry lights: switch DPS 20 when 1 fails empty
		if on == nil && switchDPS == 1 && !strings.Contains(strings.ToLower(msg), "missing") {
			cfg2 := cfg
			cfg2.TuyaSwitchDPS = 20
			if r2, e2 := controlTuyaPython(ctx, on, cfg2, verF); e2 == nil && r2.On != nil {
				if r2.Extra == nil {
					r2.Extra = map[string]any{}
				}
				r2.Extra["tuya_switch_dps_auto"] = 20
				return r2, nil
			}
		}
		return Result{}, fmt.Errorf("Tuya tinytuya: %s", msg)
	}
	var onPtr *bool
	if v, ok := resp["on"]; ok && v != nil {
		b := asBoolAny(v)
		onPtr = &b
	}
	dps, _ := resp["dps"].(map[string]any)
	// if still nil on and dps has 20 bool — use it
	if onPtr == nil {
		if b := dpsBoolKey(dps, "20"); b != nil {
			onPtr = b
		} else {
			onPtr = dpsBool(dps, switchDPS)
		}
	}
	outR := Result{
		On:      onPtr,
		Backend: "tuya",
		Extra: map[string]any{
			"dps":       dps,
			"device_id": devID,
			"via":       "tinytuya",
			"version":   verF,
		},
	}
	if pm := tuyaDPSPower(dps); pm != nil {
		outR.Power = pm
	}
	// Light telemetry: brightness 0–1000 raw → 0–100 %, mode string
	attachLightTelemetry(outR.Extra, dps, cfg, resp)
	return outR, nil
}

// attachLightTelemetry fills brightness_pct / mode / colour_data from DPS or helper resp.
func attachLightTelemetry(extra map[string]any, dps map[string]any, cfg config.DeviceCfg, resp map[string]any) {
	if extra == nil {
		return
	}
	// helper may already return brightness 0-1000 as "brightness"
	if br, ok := resp["brightness"]; ok && br != nil {
		if n, ok := asFloatAny(br); ok {
			extra["brightness_raw"] = int(n)
			// if helper sent percent already (≤100), keep; else scale from 1000
			pct := int(n)
			if n > 100 {
				pct = int(n*100.0/1000.0 + 0.5)
			}
			if pct < 0 {
				pct = 0
			}
			if pct > 100 {
				pct = 100
			}
			extra["brightness_pct"] = pct
		}
	} else if dps != nil {
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
	}
	if m, ok := resp["mode"].(string); ok && m != "" {
		extra["mode"] = m
	} else if dps != nil {
		mdps := cfg.ModeDPS()
		if mdps > 0 {
			if m, ok := dps[strconv.Itoa(mdps)].(string); ok && m != "" {
				extra["mode"] = m
			}
		}
	}
	if dps != nil {
		if c, ok := dps["25"]; ok {
			extra["colour_data"] = c
		}
		if c, ok := dps["24"]; ok {
			extra["colour_data"] = c
		}
	}
	if sd, ok := resp["switch_dps"]; ok {
		extra["switch_dps_used"] = sd
	}
}

func findTuyaLanCtl() string {
	cands := []string{
		"/opt/lib/poolheat/tuya_lan_ctl.py",
		"tuya_lan_ctl.py",
	}
	// relative to executable
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		cands = append([]string{
			filepath.Join(dir, "tuya_lan_ctl.py"),
			filepath.Join(dir, "../lib/poolheat/tuya_lan_ctl.py"),
			"/opt/lib/poolheat/tuya_lan_ctl.py",
		}, cands...)
	}
	for _, p := range cands {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func dpsBoolKey(dps map[string]any, key string) *bool {
	if dps == nil {
		return nil
	}
	v, ok := dps[key]
	if !ok || v == nil {
		return nil
	}
	// only treat true bool / 0/1 as switch — skip large numbers (voltage)
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

// tuyaControlWire builds CONTROL payload bytes (tinytuya-compatible).
//
//	v3.4+: "3.4"+12×0x00 + {"protocol":5,"t":int,"data":{"dps":{...}}}
//	v3.1:  JSON {"devId","uid","t","dps":{...}}
//
// Without the 3.4 version header, plugs often return retcode=0 but leave DPS unchanged
// → "after set dps=1 val=true still reported=false".
func tuyaControlWire(ver proto.Version, devID, dpsKey string, val any) (string, error) {
	dps := map[string]any{dpsKey: val}
	var obj map[string]any
	if ver == proto.Version31 {
		obj = map[string]any{
			"devId": devID,
			"uid":   "",
			"t":     time.Now().Unix(),
			"dps":   dps,
		}
	} else {
		obj = map[string]any{
			"protocol": 5,
			"t":        time.Now().Unix(),
			"data":     map[string]any{"dps": dps},
		}
	}
	// compact JSON (tinytuya strips spaces)
	raw, err := json.Marshal(obj)
	if err != nil {
		return "", err
	}
	if ver == proto.Version31 {
		return string(raw), nil
	}
	// PROTOCOL_34_HEADER = b"3.4" + 12 * b"\x00"
	hdr := make([]byte, 0, 15+len(raw))
	hdr = append(hdr, []byte(ver.String())...) // "3.4"
	hdr = append(hdr, make([]byte, 12)...)
	hdr = append(hdr, raw...)
	return string(hdr), nil
}

func tuyaStatus(cl client.BlockingClient, ver proto.Version, devID string) (map[string]any, error) {
	var err error
	if ver == proto.Version34 {
		// empty object — no version header (tinytuya NO_PROTOCOL_HEADER_CMDS)
		err = cl.Send(proto.CmdIdTypeDpQueryNew, map[string]any{})
	} else {
		err = cl.Send(proto.CmdIdTypeDpQuery, dto.DpQueryRequest{GwId: devID, DevId: devID})
	}
	if err != nil {
		return nil, fmt.Errorf("Tuya status send: %w", err)
	}
	// Prefer generic map: responses may be {"dps":...} or protocol-4 wrapper.
	var raw map[string]any
	if err := cl.Read(&raw); err != nil {
		return nil, fmt.Errorf("Tuya status read: %w", err)
	}
	if d := extractDPS(raw); d != nil {
		return d, nil
	}
	// empty ACK sometimes arrives first — not a full status
	if len(raw) == 0 {
		var raw2 map[string]any
		if err := cl.Read(&raw2); err == nil {
			if d := extractDPS(raw2); d != nil {
				return d, nil
			}
			if len(raw2) > 0 {
				return raw2, nil
			}
		}
	}
	if len(raw) > 0 {
		return raw, nil
	}
	return nil, fmt.Errorf("Tuya status: empty response")
}

// extractDPS pulls switch map from status or protocol-4/5 wrappers.
func extractDPS(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	if d, ok := m["dps"].(map[string]any); ok && len(d) > 0 {
		return d
	}
	if data, ok := m["data"].(map[string]any); ok {
		if d, ok := data["dps"].(map[string]any); ok && len(d) > 0 {
			return d
		}
	}
	return nil
}

func dpsBool(dps map[string]any, switchDPS int) *bool {
	if dps == nil {
		return nil
	}
	// Prefer configured switch DPS, then common switch / light indices.
	// Note: DPS 20 is voltage on plugs but switch on many lights/dimmers —
	// only accept bool / 0/1 (see dpsBoolKey).
	keys := []string{
		strconv.Itoa(switchDPS),
		"1", "20", "101", "102", "103", "2",
	}
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
	// last resort: first bool-like 0/1 value among non-metering keys
	for k, v := range dps {
		if v == nil {
			continue
		}
		// skip obvious metering keys (numeric only)
		if k == "18" || k == "19" || k == "4" || k == "5" || k == "6" {
			continue
		}
		if b := dpsBoolKey(dps, k); b != nil {
			return b
		}
	}
	return nil
}

func tuyaDPSPower(dps map[string]any) map[string]any {
	if dps == nil {
		return nil
	}
	// 18=mA, 19=0.1W, 20=0.1V on plugs — skip if value is bool (light switch)
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
	if v, ok := getNum("20"); ok && v > 50 { // voltage ~220, not switch 0/1
		out["voltage_v"] = round2(v / 10)
	} else if v, ok := getNum("6"); ok {
		out["voltage_v"] = round2(v / 10)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}
