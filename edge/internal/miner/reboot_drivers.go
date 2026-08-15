package miner

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Vendor-specific reboot / restart for CGMiner-class ASICs (iPollo, Goldshell)
// and shared helpers. Whatsminer stays on NetPacket + V2 in write.go.

// writeTarget resolves host/port/password/vendor for a privileged write.
// Optional overrides in req.Cmd: host|ip, port, vendor|api_vendor, password.
func writeTarget(s Settings, req WriteRequest, password string) (host string, port int, pw, vendor string) {
	host = strings.TrimSpace(s.Host)
	port = s.Port
	pw = strings.TrimSpace(password)
	if pw == "" {
		pw = strings.TrimSpace(s.Password)
	}

	if h := strAny(req.Cmd["host"]); h != "" {
		host = h
	} else if h := strAny(req.Cmd["ip"]); h != "" {
		host = h
	}
	if p, ok := asInt(req.Cmd["port"]); ok && p > 0 {
		port = p
	}
	if v := strAny(req.Cmd["vendor"]); v != "" {
		vendor = normalizeVendorID(v)
	} else if v := strAny(req.Cmd["api_vendor"]); v != "" {
		vendor = normalizeVendorID(v)
	}
	if p := strAny(req.Cmd["password"]); p != "" {
		pw = p
	}

	if m := findManagedByHost(s.DataDir, host); m != nil {
		if vendor == "" {
			vendor = normalizeVendorID(m.Vendor)
		}
		if port <= 0 && m.Port > 0 {
			port = m.Port
		}
		if mp := strings.TrimSpace(m.Password); mp != "" {
			// Prefer inventory password when write used default/empty
			if pw == "" || pw == "admin" {
				pw = mp
			}
		}
	}
	if vendor == "" {
		vendor = detectVendorHint(s.DataDir, host)
	}
	if vendor == "" {
		vendor = probeVendorHTTP(host)
	}
	if port <= 0 {
		port = 4028
	}
	return host, port, pw, vendor
}

func normalizeVendorID(v string) string {
	s := strings.ToLower(strings.TrimSpace(v))
	switch s {
	case "microbt", "whatsminer":
		return "whatsminer"
	case "bitmain", "ant", "antminer":
		return "antminer"
	case "canaan", "avalon", "avalonminer":
		return "avalon"
	case "gs", "ckbox", "ckbminer", "goldshell":
		return "goldshell"
	case "ipol", "toomuchpower", "ipollo":
		return "ipollo"
	case "cgminer":
		return "cgminer"
	default:
		return s
	}
}

func findManagedByHost(dataDir, host string) *ManagedMiner {
	host = strings.TrimSpace(host)
	if host == "" {
		return nil
	}
	// strip port if present
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	f := LoadManaged(dataDir)
	for i := range f.Miners {
		mh := strings.TrimSpace(f.Miners[i].Host)
		if mh == "" {
			continue
		}
		if h, _, err := net.SplitHostPort(mh); err == nil {
			mh = h
		}
		if mh == host {
			m := f.Miners[i]
			return &m
		}
	}
	return nil
}

// detectVendorHint — managed inventory, then live_cache / fleet_live.
func detectVendorHint(dataDir, host string) string {
	if m := findManagedByHost(dataDir, host); m != nil {
		if v := normalizeVendorID(m.Vendor); v != "" {
			return v
		}
	}
	// live_cache.json
	if b, err := os.ReadFile(filepath.Join(dataDir, "live_cache.json")); err == nil {
		var live map[string]any
		if json.Unmarshal(b, &live) == nil {
			lh := strAny(live["host"])
			if h, _, err := net.SplitHostPort(lh); err == nil {
				lh = h
			}
			if lh == "" || lh == host {
				if v := normalizeVendorID(strAny(live["vendor"])); v != "" {
					return v
				}
				if v := normalizeVendorID(strAny(live["api_vendor"])); v != "" {
					return v
				}
			}
		}
	}
	// fleet_live.json — shape {miners:{ip:{live:{…}}}} or {by_host:{…}}
	if b, err := os.ReadFile(filepath.Join(dataDir, "fleet_live.json")); err == nil {
		var root map[string]any
		if json.Unmarshal(b, &root) == nil {
			for _, key := range []string{"miners", "by_host", "hosts"} {
				m, _ := root[key].(map[string]any)
				if m == nil {
					continue
				}
				entry, _ := m[host].(map[string]any)
				if entry == nil {
					continue
				}
				if live, ok := entry["live"].(map[string]any); ok {
					if v := normalizeVendorID(strAny(live["vendor"])); v != "" {
						return v
					}
					if v := normalizeVendorID(strAny(live["api_vendor"])); v != "" {
						return v
					}
				}
				if v := normalizeVendorID(strAny(entry["vendor"])); v != "" {
					return v
				}
			}
		}
	}
	return ""
}

// probeVendorHTTP — cheap fingerprint on :80 for goldshell / ipollo.
func probeVendorHTTP(host string) string {
	host = strings.TrimSpace(host)
	if host == "" {
		return ""
	}
	client := &http.Client{Timeout: 3 * time.Second}
	// Goldshell cloud-box: /mcb/status → {"model":"Goldshell-…"}
	if resp, err := client.Get(fmt.Sprintf("http://%s/mcb/status", host)); err == nil {
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
		low := strings.ToLower(string(body))
		if strings.Contains(low, "goldshell") || strings.Contains(low, `"model"`) &&
			(strings.Contains(low, "ckbox") || strings.Contains(low, "ck-box") ||
				strings.Contains(low, "doge") || strings.Contains(low, "kd") ||
				strings.Contains(low, "hs-box") || strings.Contains(low, "byte")) {
			return "goldshell"
		}
		var js map[string]any
		if json.Unmarshal(body, &js) == nil {
			if m := strAny(js["model"]); strings.Contains(strings.ToLower(m), "goldshell") ||
				strings.Contains(strings.ToLower(m), "ck") {
				return "goldshell"
			}
		}
	}
	// iPollo LuCI landing
	if resp, err := client.Get(fmt.Sprintf("http://%s/cgi-bin/luci/", host)); err == nil {
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 16<<10))
		low := strings.ToLower(string(body))
		if strings.Contains(low, "ipollo") || strings.Contains(low, "ipolloeth") ||
			strings.Contains(low, "ipollo_main") {
			return "ipollo"
		}
	}
	return ""
}

// ─── CGMiner JSON API (:4028) ───────────────────────────────────────────────

// cgminerRPC sends {"command":cmd,"parameter":param} to host:port.
// Returns parsed JSON (or nil) and error. Link-drop after write is returned as err.
func cgminerRPC(host string, port int, command, parameter string, timeout time.Duration) (map[string]any, error) {
	host = strings.TrimSpace(host)
	if host == "" {
		return nil, fmt.Errorf("cgminer: empty host")
	}
	if port <= 0 {
		port = 4028
	}
	if timeout <= 0 {
		timeout = 12 * time.Second
	}
	addr := net.JoinHostPort(host, fmt.Sprintf("%d", port))
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))

	payload := map[string]any{"command": command}
	if parameter != "" {
		payload["parameter"] = parameter
	}
	b, _ := json.Marshal(payload)
	// CGMiner expects trailing null or newline
	b = append(b, 0)
	if _, err := conn.Write(b); err != nil {
		return nil, err
	}
	raw, err := io.ReadAll(io.LimitReader(conn, 256<<10))
	if err != nil {
		return nil, err
	}
	// strip trailing nulls
	raw = []byte(strings.TrimRight(string(raw), "\x00\r\n\t "))
	if len(raw) == 0 {
		return nil, fmt.Errorf("cgminer: empty response")
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		// some firmwares return STATUS array only
		var arr any
		if err2 := json.Unmarshal(raw, &arr); err2 != nil {
			return nil, fmt.Errorf("cgminer json: %w", err)
		}
		return map[string]any{"raw": arr}, nil
	}
	return out, nil
}

func cgminerStatusOK(resp map[string]any) bool {
	if resp == nil {
		return false
	}
	// classic: STATUS:[{STATUS:"S",…}]
	if st, ok := resp["STATUS"].([]any); ok && len(st) > 0 {
		if m, ok := st[0].(map[string]any); ok {
			s := strings.ToUpper(strAny(m["STATUS"]))
			if s == "S" || s == "I" {
				return true
			}
			if s == "E" || s == "F" {
				return false
			}
		}
	}
	if s := strings.ToUpper(strAny(resp["STATUS"])); s == "S" || s == "I" {
		return true
	}
	// no explicit error → treat as ok when we got JSON after write
	if resp["STATUS"] == nil && resp["Msg"] == nil {
		return true
	}
	return false
}

// cgminerRestartMining — process restart (not full OS reboot).
func cgminerRestartMining(host string, port int) (map[string]any, error) {
	// Prefer "restart"; some forks only accept "quit" (watchdog restarts miner).
	var lastErr error
	for _, cmd := range []string{"restart", "quit"} {
		resp, err := cgminerRPC(host, port, cmd, "", 15*time.Second)
		if err != nil {
			lastErr = err
			if isLinkDropAccepted(err) {
				return map[string]any{
					"STATUS": "S", "Msg": cmd + " sent (link dropped)",
					"transport": "cgminer", "cmd": cmd,
				}, nil
			}
			log.Printf("[miner-poller] cgminer %s %s:%d: %v", cmd, host, port, err)
			continue
		}
		if cgminerStatusOK(resp) || resp != nil {
			if resp == nil {
				resp = map[string]any{}
			}
			resp["transport"] = "cgminer"
			resp["cmd"] = cmd
			return resp, nil
		}
		lastErr = fmt.Errorf("cgminer %s rejected", cmd)
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("cgminer restart failed")
	}
	return nil, lastErr
}

// ─── iPollo LuCI ───────────────────────────────────────────────────────────

// ipolloLuCIReboot logs into LuCI and hits ipollo_system/ipollo_reboot.
// Confirmed path from stock UI: /cgi-bin/luci/admin/ipollo_system/ipollo_reboot
func ipolloLuCIReboot(host, password string) (map[string]any, error) {
	host = strings.TrimSpace(host)
	if host == "" {
		return nil, fmt.Errorf("ipollo: empty host")
	}
	type cred struct{ user, pw string }
	cands := []cred{
		{"root", password},
		{"root", "root"},
		{"admin", password},
		{"admin", "admin"},
	}
	if password == "" {
		cands = []cred{{"root", "root"}, {"admin", "admin"}}
	}
	seen := map[string]bool{}
	var lastErr error
	for _, cr := range cands {
		cr.user = strings.TrimSpace(cr.user)
		cr.pw = strings.TrimSpace(cr.pw)
		if cr.user == "" || cr.pw == "" {
			continue
		}
		key := cr.user + "\x00" + cr.pw
		if seen[key] {
			continue
		}
		seen[key] = true
		cookie, err := ipolloLuCILogin(host, cr.user, cr.pw)
		if err != nil || cookie == "" {
			if err != nil {
				lastErr = err
			} else {
				lastErr = fmt.Errorf("login failed user=%s", cr.user)
			}
			continue
		}
		// Paths seen on V1 / G1 LuCI builds
		paths := []string{
			"/cgi-bin/luci/admin/ipollo_system/ipollo_reboot",
			"/cgi-bin/luci/admin/system/reboot",
			"/cgi-bin/luci/admin/ipollo_system/reboot",
		}
		for _, p := range paths {
			err := ipolloLuCIGet(host, p, cookie, 12*time.Second)
			if err != nil {
				// peer close / timeout after accept = reboot started
				if isLinkDropAccepted(err) {
					return map[string]any{
						"STATUS": "S", "Msg": "reboot sent (link dropped)",
						"transport": "ipollo-luci", "path": p, "user": cr.user,
					}, nil
				}
				lastErr = err
				log.Printf("[miner-poller] ipollo luci reboot %s %s: %v", host, p, err)
				continue
			}
			return map[string]any{
				"STATUS": "S", "Msg": "reboot accepted",
				"transport": "ipollo-luci", "path": p, "user": cr.user,
			}, nil
		}
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("ipollo luci reboot failed")
	}
	return nil, lastErr
}

func ipolloLuCILogin(host, user, password string) (string, error) {
	client := &http.Client{
		Timeout: 6 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	form := url.Values{}
	form.Set("luci_username", user)
	form.Set("luci_password", password)
	req, err := http.NewRequest(http.MethodPost,
		fmt.Sprintf("http://%s/cgi-bin/luci/", host),
		strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
	// Parse Set-Cookie for sysauth=
	for _, c := range resp.Cookies() {
		if strings.EqualFold(c.Name, "sysauth") && c.Value != "" {
			return "sysauth=" + c.Value, nil
		}
	}
	// raw header fallback
	sc := resp.Header.Get("Set-Cookie")
	if i := strings.Index(sc, "sysauth="); i >= 0 {
		rest := sc[i:]
		if j := strings.Index(rest, ";"); j > 0 {
			return rest[:j], nil
		}
		return rest, nil
	}
	return "", fmt.Errorf("no sysauth cookie")
}

func ipolloLuCIGet(host, path, cookie string, timeout time.Duration) error {
	client := &http.Client{
		Timeout: timeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	req, err := http.NewRequest(http.MethodGet, fmt.Sprintf("http://%s%s", host, path), nil)
	if err != nil {
		return err
	}
	req.Header.Set("Cookie", cookie)
	req.Header.Set("Accept", "text/html,application/json,*/*")
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 8<<10))
	// 2xx / 3xx = accepted (reboot page or redirect)
	if resp.StatusCode >= 200 && resp.StatusCode < 400 {
		return nil
	}
	return fmt.Errorf("http %d", resp.StatusCode)
}

// ─── Goldshell HTTP (/mcb/* + JWT) ─────────────────────────────────────────

// goldshellHTTPReboot authenticates (JWT) then GET /mcb/reboot.
// Defaults: admin / 123456789 (Goldshell factory).
func goldshellHTTPReboot(host, password string) (map[string]any, error) {
	host = strings.TrimSpace(host)
	if host == "" {
		return nil, fmt.Errorf("goldshell: empty host")
	}
	type cred struct{ user, pw string }
	cands := []cred{
		{"admin", password},
		{"admin", "123456789"},
		{"admin", "admin"},
		{"root", password},
	}
	if password == "" {
		cands = []cred{{"admin", "123456789"}, {"admin", "admin"}}
	}
	seen := map[string]bool{}
	var lastErr error
	for _, cr := range cands {
		cr.user = strings.TrimSpace(cr.user)
		cr.pw = strings.TrimSpace(cr.pw)
		if cr.user == "" || cr.pw == "" {
			continue
		}
		key := cr.user + "\x00" + cr.pw
		if seen[key] {
			continue
		}
		seen[key] = true
		tok, err := goldshellLogin(host, cr.user, cr.pw)
		if err != nil || tok == "" {
			if err != nil {
				lastErr = err
			} else {
				lastErr = fmt.Errorf("empty JWT user=%s", cr.user)
			}
			continue
		}
		err = goldshellMCBGet(host, "reboot", tok, 15*time.Second)
		if err != nil {
			if isLinkDropAccepted(err) {
				return map[string]any{
					"STATUS": "S", "Msg": "reboot sent (link dropped)",
					"transport": "goldshell-http", "user": cr.user,
				}, nil
			}
			lastErr = err
			log.Printf("[miner-poller] goldshell reboot %s: %v", host, err)
			continue
		}
		return map[string]any{
			"STATUS": "S", "Msg": "reboot accepted",
			"transport": "goldshell-http", "user": cr.user,
		}, nil
	}
	// Some FW allow unauthenticated reboot (rare) — try bare GET
	if err := goldshellMCBGet(host, "reboot", "", 10*time.Second); err == nil || isLinkDropAccepted(err) {
		return map[string]any{
			"STATUS": "S", "Msg": "reboot sent",
			"transport": "goldshell-http", "auth": "none",
		}, nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("goldshell reboot failed")
	}
	return nil, lastErr
}

func goldshellLogin(host, user, password string) (string, error) {
	client := &http.Client{Timeout: 6 * time.Second}
	// logout first (session hygiene, same as pyasic)
	_, _ = client.Get(fmt.Sprintf("http://%s/user/logout", host))

	tryLogin := func(u string) (string, error) {
		resp, err := client.Get(u)
		if err != nil {
			return "", err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
		var js map[string]any
		if err := json.Unmarshal(body, &js); err != nil {
			return "", fmt.Errorf("login json: %w", err)
		}
		// "JWT Token" (pyasic) or common variants
		for _, k := range []string{"JWT Token", "JWTToken", "token", "jwt", "Token"} {
			if t := strAny(js[k]); t != "" {
				return t, nil
			}
		}
		return "", fmt.Errorf("no JWT in login response")
	}

	// plain password
	q := url.Values{}
	q.Set("username", user)
	q.Set("password", password)
	q.Set("cipher", "false")
	tok, err := tryLogin(fmt.Sprintf("http://%s/user/login?%s", host, q.Encode()))
	if err == nil && tok != "" {
		return tok, nil
	}
	// encrypted default hash used by some FW (pyasic fallback for admin)
	if user == "admin" {
		q2 := url.Values{}
		q2.Set("username", "admin")
		q2.Set("password", "bbad7537f4c8b6ea31eea0b3d760e257")
		q2.Set("cipher", "true")
		if tok2, err2 := tryLogin(fmt.Sprintf("http://%s/user/login?%s", host, q2.Encode())); err2 == nil && tok2 != "" {
			return tok2, nil
		}
	}
	if err != nil {
		return "", err
	}
	return "", fmt.Errorf("goldshell login failed")
}

func goldshellMCBGet(host, command, token string, timeout time.Duration) error {
	client := &http.Client{Timeout: timeout}
	req, err := http.NewRequest(http.MethodGet,
		fmt.Sprintf("http://%s/mcb/%s", host, command), nil)
	if err != nil {
		return err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	snip := strings.TrimSpace(string(body))
	if len(snip) > 80 {
		snip = snip[:80]
	}
	// 401/403 with body still may mean auth fail
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return fmt.Errorf("http %d auth: %s", resp.StatusCode, snip)
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 400 {
		return nil
	}
	return fmt.Errorf("http %d: %s", resp.StatusCode, snip)
}
