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

// ewelinkParseSwitch extracts on/off from CoolKit params (plug / multi / breaker).
func ewelinkParseSwitch(data map[string]any) *bool {
	if data == nil {
		return nil
	}
	if v, ok := data["switch"]; ok {
		s := strings.ToLower(strAny(v))
		if s == "on" || s == "1" || s == "true" {
			b := true
			return &b
		}
		if s == "off" || s == "0" || s == "false" {
			b := false
			return &b
		}
	}
	// multi-outlet
	if arr, ok := data["switches"].([]any); ok {
		anyOn := false
		found := false
		for _, it := range arr {
			m, ok := it.(map[string]any)
			if !ok {
				continue
			}
			found = true
			s := strings.ToLower(strAny(m["switch"]))
			if s == "on" || s == "1" || s == "true" {
				anyOn = true
			}
		}
		if found {
			return &anyOn
		}
	}
	// some FW nest under params
	if p, ok := data["params"].(map[string]any); ok {
		return ewelinkParseSwitch(p)
	}
	// numeric relay
	for _, k := range []string{"relay", "state", "outlet"} {
		if v, ok := data[k]; ok {
			s := strings.ToLower(strAny(v))
			if s == "on" || s == "1" || s == "true" {
				b := true
				return &b
			}
			if s == "off" || s == "0" || s == "false" {
				b := false
				return &b
			}
		}
	}
	return nil
}

// ewelinkParsePower best-effort W/V/A from CoolKit params.
func ewelinkParsePower(data map[string]any) map[string]any {
	if data == nil {
		return nil
	}
	out := map[string]any{}
	// voltage
	for _, k := range []string{"voltage", "voltage_00", "voltage_0"} {
		if v, ok := data[k]; ok {
			if f, ok := asFloat(v); ok {
				if f > 400 {
					f = f / 100
				}
				out["voltage_v"] = f
				break
			}
		}
	}
	// current
	for _, k := range []string{"current", "current_00", "current_0", "supplyCurrent"} {
		if v, ok := data[k]; ok {
			if f, ok := asFloat(v); ok {
				if f > 20 {
					f = f / 100
				}
				out["current_a"] = f
				break
			}
		}
	}
	// power
	for _, k := range []string{"power", "actPow_00", "actPow_0", "supplyPower"} {
		if v, ok := data[k]; ok {
			if f, ok := asFloat(v); ok {
				if f > 5000 {
					f = f / 100
				}
				out["power_w"] = f
				break
			}
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func asFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case float32:
		return float64(t), true
	case int:
		return float64(t), true
	case int64:
		return float64(t), true
	case json.Number:
		f, err := t.Float64()
		return f, err == nil
	case string:
		var f float64
		_, err := fmt.Sscanf(strings.TrimSpace(t), "%f", &f)
		return f, err == nil
	default:
		return 0, false
	}
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

// ewelinkProtoCache remembers diy|lan per IP for mode=auto (thread-safe enough for poller).
var ewelinkProtoCache = map[string]string{}

func ewelinkRespOK(j map[string]any) bool {
	if j == nil {
		return false
	}
	errCode := j["error"]
	return errCode == nil || errCode == 0 || errCode == float64(0) || errCode == "0"
}

func ewelinkPathOrder(mode, ip, devicekey string) []bool {
	// returns ordered useLAN flags
	switch mode {
	case "lan":
		return []bool{true}
	case "diy":
		return []bool{false}
	default: // auto
		cached := ewelinkProtoCache[ip]
		var order []bool
		switch cached {
		case "lan":
			if devicekey != "" {
				order = []bool{true, false}
			} else {
				order = []bool{false}
			}
		case "diy":
			if devicekey != "" {
				order = []bool{false, true}
			} else {
				order = []bool{false}
			}
		default:
			if devicekey != "" {
				order = []bool{true, false} // LAN first when key known
			} else {
				order = []bool{false}
			}
		}
		return order
	}
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
	if mode != "auto" && mode != "diy" && mode != "lan" {
		mode = "auto"
	}
	devicekey := strings.TrimSpace(cfg.EwelinkDeviceKey)
	selfAPI := strings.TrimSpace(cfg.EwelinkAPIKey)
	if mode == "lan" && devicekey == "" {
		return Result{}, fmt.Errorf("eWeLink devicekey empty (LAN encrypt)")
	}
	base := fmt.Sprintf("http://%s:%d/zeroconf", ip, port)
	hdrs := map[string]string{"Content-Type": "application/json"}

	post := func(cmd string, data map[string]any, useLAN bool) (map[string]any, error) {
		withKey := ""
		if useLAN {
			if devicekey == "" {
				return nil, fmt.Errorf("eWeLink devicekey empty (LAN encrypt)")
			}
			withKey = devicekey
		}
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

	remember := func(useLAN bool) {
		if mode == "auto" {
			if useLAN {
				ewelinkProtoCache[ip] = "lan"
			} else {
				ewelinkProtoCache[ip] = "diy"
			}
		}
	}

	paths := ewelinkPathOrder(mode, ip, devicekey)

	if on == nil {
		// status — try each transport.
		// POWR2 / PSF-X67 (uiid 32): POST /zeroconf/statistics returns encrypted
		// {voltage,current,power} — info/getState often empty (SonoffLAN-compatible).
		var last error
		var best *Result
		for _, useLAN := range paths {
			if useLAN {
				_, _ = post("uiActive", map[string]any{"uiActive": 60}, true)
				_, _ = post("sledonline", map[string]any{"sledOnline": "on"}, true)
			}
			type cmdSpec struct {
				cmd  string
				data map[string]any
			}
			cmds := []cmdSpec{
				{"statistics", map[string]any{}},
				{"getState", map[string]any{}},
				{"info", map[string]any{}},
			}
			merged := map[string]any{}
			for _, cs := range cmds {
				j, err := post(cs.cmd, cs.data, useLAN)
				if err != nil {
					last = err
					continue
				}
				data, _ := j["data"].(map[string]any)
				if data == nil {
					data = map[string]any{}
				}
				for k, v := range data {
					merged[k] = v
				}
				sw := ewelinkParseSwitch(merged)
				pwr := ewelinkParsePower(merged)
				if sw != nil || pwr != nil || ewelinkRespOK(j) {
					remember(useLAN)
					r := Result{On: sw, Backend: "ewelink", Power: pwr}
					if sw != nil && pwr != nil {
						return r, nil
					}
					if pwr != nil {
						best = &r
						continue // still try getState for switch
					}
					if sw != nil {
						if best != nil && best.Power != nil {
							best.On = sw
							return *best, nil
						}
						return r, nil
					}
					if best == nil {
						best = &r
					}
				} else {
					last = fmt.Errorf("eWeLink error=%v", j["error"])
				}
			}
			if best != nil && best.Power != nil {
				return *best, nil
			}
		}
		if best != nil {
			return *best, nil
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
	attempts := []struct {
		cmd  string
		data map[string]any
	}{
		{"switch", map[string]any{"switch": sw}},
		{"switches", map[string]any{"switches": []map[string]any{{"outlet": cfg.EwelinkOutlet, "switch": sw}}}},
	}
	var last error
	for _, useLAN := range paths {
		for _, a := range attempts {
			j, err := post(a.cmd, a.data, useLAN)
			if err != nil {
				last = err
				continue
			}
			if ewelinkRespOK(j) {
				remember(useLAN)
				got := *on
				return Result{On: &got, Backend: "ewelink"}, nil
			}
			last = fmt.Errorf("eWeLink error=%v", j["error"])
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
