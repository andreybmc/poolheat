package backend

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
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
	ver := proto.Version34
	if cfg.TuyaVersion > 0 && cfg.TuyaVersion < 3.4 {
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
			return Result{}, err
		}
		defer cl.Close()
		dps, err := tuyaStatus(cl, ver, devID)
		if err != nil {
			return Result{}, err
		}
		cur := dpsBool(dps, switchDPS)
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
	// Prefer configured switch DPS, then common switch indices (never 18/19/20 metering).
	keys := []string{
		strconv.Itoa(switchDPS),
		"1", "101", "102", "103",
	}
	seen := map[string]bool{}
	for _, k := range keys {
		if k == "" || seen[k] {
			continue
		}
		seen[k] = true
		if v, ok := dps[k]; ok && v != nil {
			b := asBoolAny(v)
			return &b
		}
	}
	// last resort: first bool-like value among small keys
	for k, v := range dps {
		if v == nil {
			continue
		}
		// skip obvious metering keys
		if k == "18" || k == "19" || k == "20" || k == "4" || k == "5" || k == "6" {
			continue
		}
		switch v.(type) {
		case bool, int, int64, float64, float32:
			b := asBoolAny(v)
			return &b
		}
	}
	return nil
}

func tuyaDPSPower(dps map[string]any) map[string]any {
	if dps == nil {
		return nil
	}
	// 18=mA, 19=0.1W, 20=0.1V
	get := func(k string) (float64, bool) {
		v, ok := dps[k]
		if !ok {
			return 0, false
		}
		return asFloatAny(v)
	}
	out := map[string]any{}
	if i, ok := get("18"); ok {
		out["current_a"] = round3(i / 1000)
	} else if i, ok := get("4"); ok {
		out["current_a"] = round3(i / 1000)
	}
	if p, ok := get("19"); ok {
		out["power_w"] = round2(p / 10)
	} else if p, ok := get("5"); ok {
		out["power_w"] = round2(p / 10)
	}
	if v, ok := get("20"); ok {
		out["voltage_v"] = round2(v / 10)
	} else if v, ok := get("6"); ok {
		out["voltage_v"] = round2(v / 10)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}
