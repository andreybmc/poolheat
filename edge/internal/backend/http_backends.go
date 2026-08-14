package backend

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"crypto/rand"
	"encoding/base64"
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

// CoolKit LAN: AES-128-CBC, key=MD5(devicekey), PKCS7. Matches ui/ewelink_lan.py.
func ewelinkPKCS7Pad(b []byte, block int) []byte {
	n := block - (len(b) % block)
	if n == 0 {
		n = block
	}
	out := make([]byte, len(b)+n)
	copy(out, b)
	for i := len(b); i < len(out); i++ {
		out[i] = byte(n)
	}
	return out
}

func ewelinkEncrypt(dataObj map[string]any, devicekey string) (ctB64, ivB64 string, err error) {
	plain, err := json.Marshal(dataObj)
	if err != nil {
		return "", "", err
	}
	sum := md5.Sum([]byte(devicekey))
	block, err := aes.NewCipher(sum[:])
	if err != nil {
		return "", "", err
	}
	iv := make([]byte, aes.BlockSize)
	if _, err := rand.Read(iv); err != nil {
		return "", "", err
	}
	padded := ewelinkPKCS7Pad(plain, aes.BlockSize)
	ct := make([]byte, len(padded))
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(ct, padded)
	return base64.StdEncoding.EncodeToString(ct), base64.StdEncoding.EncodeToString(iv), nil
}

func ewelinkDecrypt(dataB64, ivB64, devicekey string) (map[string]any, error) {
	ct, err := base64.StdEncoding.DecodeString(dataB64)
	if err != nil {
		return nil, err
	}
	iv, err := base64.StdEncoding.DecodeString(ivB64)
	if err != nil {
		return nil, err
	}
	sum := md5.Sum([]byte(devicekey))
	block, err := aes.NewCipher(sum[:])
	if err != nil {
		return nil, err
	}
	if len(ct) == 0 || len(ct)%aes.BlockSize != 0 || len(iv) != aes.BlockSize {
		return nil, fmt.Errorf("eWeLink decrypt: bad block size")
	}
	plain := make([]byte, len(ct))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, ct)
	// PKCS7 unpad (best-effort)
	if n := int(plain[len(plain)-1]); n >= 1 && n <= 16 && n <= len(plain) {
		plain = plain[:len(plain)-n]
	}
	plain = bytes.TrimRight(plain, "\x02")
	var out map[string]any
	if err := json.Unmarshal(plain, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func ewelinkBuildPayload(devID, selfAPI string, data map[string]any, devicekey string) ([]byte, error) {
	if selfAPI == "" {
		selfAPI = "123"
	}
	seq := fmt.Sprintf("%d", time.Now().UnixMilli())
	payload := map[string]any{
		"sequence":   seq,
		"deviceid":   devID,
		"selfApikey": selfAPI,
		"data":       data,
	}
	if strings.TrimSpace(devicekey) != "" {
		ct, iv, err := ewelinkEncrypt(data, devicekey)
		if err != nil {
			return nil, err
		}
		payload["encrypt"] = true
		payload["data"] = ct
		payload["iv"] = iv
	}
	return json.Marshal(payload)
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
	mode := strings.ToLower(strings.TrimSpace(cfg.EwelinkMode))
	if mode == "" {
		mode = "auto"
	}
	devicekey := strings.TrimSpace(cfg.EwelinkDeviceKey)
	selfAPI := strings.TrimSpace(cfg.EwelinkAPIKey)
	useKey := false
	switch mode {
	case "lan":
		if devicekey == "" {
			return Result{}, fmt.Errorf("eWeLink devicekey empty (LAN encrypt)")
		}
		useKey = true
	case "diy":
		useKey = false
	default: // auto
		useKey = devicekey != ""
	}
	key := ""
	if useKey {
		key = devicekey
	}
	base := fmt.Sprintf("http://%s:%d/zeroconf", ip, port)
	hdrs := map[string]string{"Content-Type": "application/json"}

	post := func(cmd string, data map[string]any, withKey string) (map[string]any, error) {
		body, err := ewelinkBuildPayload(devID, selfAPI, data, withKey)
		if err != nil {
			return nil, err
		}
		code, text, err := doHTTP(ctx, "POST", base+"/"+cmd, body, hdrs)
		if err != nil {
			return nil, err
		}
		if code >= 400 {
			return nil, fmt.Errorf("eWeLink %s HTTP %d", cmd, code)
		}
		var j map[string]any
		if err := json.Unmarshal([]byte(text), &j); err != nil {
			return nil, fmt.Errorf("eWeLink %s: bad JSON", cmd)
		}
		// decrypt response when encrypted
		if withKey != "" {
			if ds, ok := j["data"].(string); ok {
				iv, _ := j["iv"].(string)
				if ds != "" && iv != "" {
					if dec, err := ewelinkDecrypt(ds, iv, withKey); err == nil {
						j["data"] = dec
					}
				}
			}
		}
		return j, nil
	}

	if on == nil {
		// status: info / getState
		var last error
		for _, cmd := range []string{"info", "getState"} {
			j, err := post(cmd, map[string]any{}, key)
			if err != nil {
				last = err
				// auto: try DIY if encrypt failed
				if mode == "auto" && key != "" {
					if j2, err2 := post(cmd, map[string]any{}, ""); err2 == nil {
						j = j2
						err = nil
					}
				}
				if err != nil {
					continue
				}
			}
			data, _ := j["data"].(map[string]any)
			sw := strAny(data["switch"])
			if sw == "" {
				if arr, ok := data["switches"].([]any); ok && len(arr) > 0 {
					if m0, ok := arr[0].(map[string]any); ok {
						sw = strAny(m0["switch"])
					}
				}
			}
			b := sw == "on" || sw == "1" || strings.EqualFold(sw, "true")
			// if no switch field, leave unknown only when data empty
			if sw == "" && len(data) == 0 {
				last = fmt.Errorf("eWeLink info: no switch state")
				continue
			}
			return Result{On: &b, Backend: "ewelink"}, nil
		}
		if last != nil {
			return Result{}, last
		}
		return Result{}, fmt.Errorf("eWeLink status failed")
	}

	sw := "off"
	if *on {
		sw = "on"
	}
	// try single-channel then multi
	attempts := []struct {
		cmd  string
		data map[string]any
	}{
		{"switch", map[string]any{"switch": sw}},
		{"switches", map[string]any{"switches": []map[string]any{{"outlet": cfg.EwelinkOutlet, "switch": sw}}}},
	}
	var last error
	for _, a := range attempts {
		j, err := post(a.cmd, a.data, key)
		if err != nil {
			last = err
			continue
		}
		errCode := j["error"]
		if errCode == nil || errCode == 0 || errCode == float64(0) || errCode == "0" {
			got := *on
			return Result{On: &got, Backend: "ewelink"}, nil
		}
		last = fmt.Errorf("eWeLink error=%v", errCode)
	}
	// auto fallback: DIY if encrypt failed
	if mode == "auto" && key != "" {
		j, err := post("switch", map[string]any{"switch": sw}, "")
		if err == nil {
			errCode := j["error"]
			if errCode == nil || errCode == 0 || errCode == float64(0) || errCode == "0" {
				got := *on
				return Result{On: &got, Backend: "ewelink"}, nil
			}
		} else {
			last = err
		}
	}
	if last != nil {
		return Result{}, last
	}
	return Result{}, fmt.Errorf("eWeLink set failed")
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
