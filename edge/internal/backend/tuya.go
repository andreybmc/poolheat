package backend

import (
	"context"
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
	timeout := 6 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		if rem := time.Until(dl); rem > time.Second && rem < timeout {
			timeout = rem
		}
	}

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
		return Result{}, fmt.Errorf("Tuya connect %s: %w", ip, err)
	}
	defer cl.Close()

	dps, err := tuyaStatus(cl, ver, devID)
	if err != nil {
		return Result{}, err
	}
	cur := dpsBool(dps, switchDPS)
	if on == nil {
		out := Result{On: cur, Backend: "tuya", Extra: map[string]any{"dps": dps, "device_id": devID}}
		if pm := tuyaDPSPower(dps); pm != nil {
			out.Power = pm
		}
		return out, nil
	}
	want := *on
	if cur != nil && *cur == want {
		out := Result{On: cur, Backend: "tuya", Skipped: true, Reason: "already_in_state", Extra: map[string]any{"dps": dps}}
		if pm := tuyaDPSPower(dps); pm != nil {
			out.Power = pm
		}
		return out, nil
	}

	// set DPS
	dpsKey := strconv.Itoa(switchDPS)
	payload := map[string]any{
		"dps": map[string]any{dpsKey: want},
		"t":   time.Now().Unix(),
	}
	if ver == proto.Version31 {
		payload["gwId"] = devID
		payload["devId"] = devID
		payload["uid"] = ""
	}
	cmd := proto.CmdIdTypeControlNew
	if ver == proto.Version31 {
		cmd = proto.CmdIdTypeControl
	}
	if err := cl.Send(cmd, payload); err != nil {
		return Result{}, fmt.Errorf("Tuya set: %w", err)
	}
	// some devices reply; ignore empty
	var discard map[string]any
	_ = cl.Read(&discard)

	// re-read status
	got := want
	if dps2, err2 := tuyaStatus(cl, ver, devID); err2 == nil {
		dps = dps2
		if b := dpsBool(dps2, switchDPS); b != nil {
			got = *b
		}
	}
	out := Result{On: &got, Backend: "tuya", Extra: map[string]any{"dps": dps, "device_id": devID}}
	if pm := tuyaDPSPower(dps); pm != nil {
		out.Power = pm
	}
	return out, nil
}

func tuyaStatus(cl client.BlockingClient, ver proto.Version, devID string) (map[string]any, error) {
	var err error
	if ver == proto.Version34 {
		err = cl.Send(proto.CmdIdTypeDpQueryNew, map[string]any{})
	} else {
		err = cl.Send(proto.CmdIdTypeDpQuery, dto.DpQueryRequest{GwId: devID, DevId: devID})
	}
	if err != nil {
		return nil, fmt.Errorf("Tuya status send: %w", err)
	}
	var resp dto.DpQueryResponse
	if err := cl.Read(&resp); err != nil {
		return nil, fmt.Errorf("Tuya status read: %w", err)
	}
	out := map[string]any{}
	for k, v := range resp.Dps {
		out[k] = v
	}
	return out, nil
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
