package backend

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/config"
)

func httpClient(ctx context.Context) *http.Client {
	to := 6 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		if rem := time.Until(dl); rem > time.Second {
			to = rem
		}
	}
	return &http.Client{Timeout: to}
}

func doHTTP(ctx context.Context, method, url string, body []byte, headers map[string]string) (int, string, error) {
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, rdr)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("User-Agent", "poolheat-devices-poller/1.0")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := httpClient(ctx).Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b), nil
}

func controlShelly(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	ip := strings.TrimSpace(cfg.IP)
	if ip == "" {
		return Result{}, fmt.Errorf("Shelly IP не настроен")
	}
	ch := cfg.ShellyChannel
	gen := strings.ToLower(cfg.ShellyGen)
	if gen == "" {
		gen = "auto"
	}

	// Gen1 helpers
	gen1Status := func() (*bool, error) {
		code, text, err := doHTTP(ctx, "GET", fmt.Sprintf("http://%s/relay/%d", ip, ch), nil, nil)
		if err != nil {
			return nil, err
		}
		if code >= 400 {
			return nil, fmt.Errorf("shelly gen1 HTTP %d", code)
		}
		var j map[string]any
		if err := json.Unmarshal([]byte(text), &j); err != nil {
			// some return ison=true plain
			if strings.Contains(text, "ison") && strings.Contains(text, "true") {
				b := true
				return &b, nil
			}
			if strings.Contains(text, "ison") && strings.Contains(text, "false") {
				b := false
				return &b, nil
			}
			return nil, err
		}
		b := asBoolAny(j["ison"])
		return &b, nil
	}
	gen1Set := func(want bool) error {
		t := "off"
		if want {
			t = "on"
		}
		code, _, err := doHTTP(ctx, "GET", fmt.Sprintf("http://%s/relay/%d?turn=%s", ip, ch, t), nil, nil)
		if err != nil {
			return err
		}
		if code >= 400 {
			return fmt.Errorf("shelly gen1 set HTTP %d", code)
		}
		return nil
	}

	// Gen2 RPC
	gen2Status := func() (*bool, error) {
		code, text, err := doHTTP(ctx, "GET", fmt.Sprintf("http://%s/rpc/Switch.GetStatus?id=%d", ip, ch), nil, nil)
		if err != nil {
			return nil, err
		}
		if code >= 400 {
			return nil, fmt.Errorf("shelly gen2 HTTP %d", code)
		}
		var j map[string]any
		if err := json.Unmarshal([]byte(text), &j); err != nil {
			return nil, err
		}
		b := asBoolAny(j["output"])
		return &b, nil
	}
	gen2Set := func(want bool) error {
		body, _ := json.Marshal(map[string]any{"id": ch, "on": want})
		code, _, err := doHTTP(ctx, "POST", fmt.Sprintf("http://%s/rpc/Switch.Set", ip), body, map[string]string{"Content-Type": "application/json"})
		if err != nil {
			return err
		}
		if code >= 400 {
			return fmt.Errorf("shelly gen2 set HTTP %d", code)
		}
		return nil
	}

	tryGen := func(g string) (Result, error) {
		var (
			st  *bool
			err error
		)
		if g == "1" {
			st, err = gen1Status()
		} else {
			st, err = gen2Status()
		}
		if err != nil {
			return Result{}, err
		}
		if on == nil {
			return Result{On: st, Backend: "shelly", Extra: map[string]any{"gen": g}}, nil
		}
		want := *on
		if st != nil && *st == want {
			return Result{On: st, Backend: "shelly", Skipped: true, Reason: "already_in_state"}, nil
		}
		if g == "1" {
			err = gen1Set(want)
		} else {
			err = gen2Set(want)
		}
		if err != nil {
			return Result{}, err
		}
		got := want
		return Result{On: &got, Backend: "shelly", Extra: map[string]any{"gen": g}}, nil
	}

	if gen == "1" || gen == "gen1" {
		return tryGen("1")
	}
	if gen == "2" || gen == "gen2" {
		return tryGen("2")
	}
	// auto: try gen2 then gen1
	if r, err := tryGen("2"); err == nil {
		return r, nil
	}
	return tryGen("1")
}

func controlEwelink(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	ip := strings.TrimSpace(cfg.IP)
	devID := strings.TrimSpace(cfg.DeviceID)
	if ip == "" || devID == "" {
		return Result{}, fmt.Errorf("eWeLink: ip и device_id обязательны")
	}
	port := cfg.EwelinkPort
	if port <= 0 {
		port = 8081
	}
	base := fmt.Sprintf("http://%s:%d/zeroconf", ip, port)
	if on == nil {
		body, _ := json.Marshal(map[string]any{"deviceid": devID, "data": map[string]any{}})
		code, text, err := doHTTP(ctx, "POST", base+"/info", body, map[string]string{"Content-Type": "application/json"})
		if err != nil {
			return Result{}, err
		}
		if code >= 400 {
			return Result{}, fmt.Errorf("eWeLink info HTTP %d", code)
		}
		var j map[string]any
		_ = json.Unmarshal([]byte(text), &j)
		data, _ := j["data"].(map[string]any)
		sw := strAny(data["switch"])
		b := sw == "on"
		return Result{On: &b, Backend: "ewelink"}, nil
	}
	sw := "off"
	if *on {
		sw = "on"
	}
	body, _ := json.Marshal(map[string]any{
		"deviceid": devID,
		"data":     map[string]any{"switch": sw},
	})
	code, _, err := doHTTP(ctx, "POST", base+"/switch", body, map[string]string{"Content-Type": "application/json"})
	if err != nil {
		return Result{}, err
	}
	if code >= 400 {
		return Result{}, fmt.Errorf("eWeLink switch HTTP %d", code)
	}
	got := *on
	return Result{On: &got, Backend: "ewelink"}, nil
}

func controlWebhook(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	if on == nil {
		// status unknown for pure webhook
		return Result{On: nil, Backend: "webhook"}, nil
	}
	url := cfg.WebhookOffURL
	bodyStr := cfg.WebhookBodyOff
	if *on {
		url = cfg.WebhookOnURL
		bodyStr = cfg.WebhookBodyOn
	}
	if strings.TrimSpace(url) == "" {
		return Result{}, fmt.Errorf("webhook URL empty for target state")
	}
	method := "GET"
	if strings.ToUpper(cfg.WebhookMethod) == "POST" {
		method = "POST"
	}
	hdrs := map[string]string{}
	if cfg.WebhookHeaders != "" {
		var h map[string]string
		if json.Unmarshal([]byte(cfg.WebhookHeaders), &h) == nil {
			for k, v := range h {
				hdrs[k] = v
			}
		}
	}
	var body []byte
	if method == "POST" && bodyStr != "" {
		body = []byte(bodyStr)
		if _, ok := hdrs["Content-Type"]; !ok {
			hdrs["Content-Type"] = "application/json"
		}
	}
	code, _, err := doHTTP(ctx, method, url, body, hdrs)
	if err != nil {
		return Result{}, err
	}
	if code >= 400 {
		return Result{}, fmt.Errorf("webhook HTTP %d", code)
	}
	got := *on
	return Result{On: &got, Backend: "webhook"}, nil
}

func controlHA(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	base := strings.TrimRight(strings.TrimSpace(cfg.HAURL), "/")
	entity := strings.TrimSpace(cfg.HAEntityID)
	token := cfg.HAToken
	if base == "" || entity == "" {
		return Result{}, fmt.Errorf("HA: url и entity_id обязательны")
	}
	hdrs := map[string]string{
		"Authorization": "Bearer " + token,
		"Content-Type":  "application/json",
	}
	if on == nil {
		code, text, err := doHTTP(ctx, "GET", base+"/api/states/"+entity, nil, hdrs)
		if err != nil {
			return Result{}, err
		}
		if code >= 400 {
			return Result{}, fmt.Errorf("HA state HTTP %d", code)
		}
		var j map[string]any
		if err := json.Unmarshal([]byte(text), &j); err != nil {
			return Result{}, err
		}
		st := strings.ToLower(strAny(j["state"]))
		b := st == "on" || st == "open" || st == "true"
		out := Result{On: &b, Backend: "homeassistant"}
		if attrs, ok := j["attributes"].(map[string]any); ok {
			if pm := haAttrsPower(attrs); pm != nil {
				out.Power = pm
			}
		}
		return out, nil
	}
	svc := "turn_off"
	if *on {
		svc = "turn_on"
	}
	// domain from entity_id
	domain := "switch"
	if i := strings.Index(entity, "."); i > 0 {
		domain = entity[:i]
	}
	body, _ := json.Marshal(map[string]any{"entity_id": entity})
	code, _, err := doHTTP(ctx, "POST", fmt.Sprintf("%s/api/services/%s/%s", base, domain, svc), body, hdrs)
	if err != nil {
		return Result{}, err
	}
	if code >= 400 {
		return Result{}, fmt.Errorf("HA service HTTP %d", code)
	}
	got := *on
	return Result{On: &got, Backend: "homeassistant"}, nil
}

func controlXiaomi(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	// Minimal: not fully implemented in Go yet — clear error so poller marks offline.
	// UI Test still works via Python serve.
	_ = ctx
	_ = on
	_ = cfg
	return Result{}, fmt.Errorf("Xiaomi backend: use UI Test (Python) — Go miIO pending")
}

func haAttrsPower(attrs map[string]any) map[string]any {
	out := map[string]any{}
	for _, k := range []string{"current_power_w", "power", "power_w", "current_consumption", "load_power"} {
		if v, ok := asFloatAny(attrs[k]); ok {
			out["power_w"] = round2(v)
			break
		}
	}
	for _, k := range []string{"voltage", "voltage_v"} {
		if v, ok := asFloatAny(attrs[k]); ok {
			out["voltage_v"] = round2(v)
			break
		}
	}
	for _, k := range []string{"current", "current_a", "amperage"} {
		if v, ok := asFloatAny(attrs[k]); ok {
			out["current_a"] = round3(v)
			break
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func strAny(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}
