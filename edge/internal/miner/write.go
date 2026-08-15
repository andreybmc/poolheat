package miner

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/andreybmc/wm-lib/api"
	"github.com/andreybmc/wm-lib/protocol"
)

// File IPC for privileged ASIC writes (serve → poller → ASIC).
// serve never opens :4028/:4433 for writes when poller is alive.
const (
	writeReqFile    = "miner_write_req.json"
	writeResultFile = "miner_write_result.json"
)

// WriteRequest is enqueued by serve (atomic JSON).
type WriteRequest struct {
	ID       string         `json:"id"`
	TS       float64        `json:"ts"`
	Cmd      map[string]any `json:"cmd"`
	Password string         `json:"password,omitempty"`
	Action   string         `json:"action,omitempty"`
	Value    any            `json:"value,omitempty"`
}

// WriteResult is written by poller after executing WriteRequest.
type WriteResult struct {
	ID        string         `json:"id"`
	OK        bool           `json:"ok"`
	Response  map[string]any `json:"response,omitempty"`
	Error     string         `json:"error,omitempty"`
	TS        float64        `json:"ts"`
	Transport string         `json:"transport,omitempty"`
	Action    string         `json:"action,omitempty"`
	Value     any            `json:"value,omitempty"`
}

// ProcessPendingWrite reads miner_write_req.json once and executes it.
// Returns true if a request was handled (success or fail).
//
// Note: config dry_run is policy-only (serve auto zones). Manual / API / Safety
// writes enqueued here always go to the ASIC — never short-circuited by DryRun.
func ProcessPendingWrite(s Settings) bool {
	path := filepath.Join(s.DataDir, writeReqFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var req WriteRequest
	if err := json.Unmarshal(b, &req); err != nil {
		log.Printf("[miner-poller] write-req json: %v", err)
		_ = os.Remove(path)
		return true
	}
	if strings.TrimSpace(req.ID) == "" || req.Cmd == nil {
		log.Printf("[miner-poller] write-req incomplete id=%q", req.ID)
		_ = os.Remove(path)
		return true
	}

	// Consume request first so a crash mid-write does not loop forever.
	_ = os.Remove(path)

	res := executeWrite(s, req)
	if err := writeJSONAtomic(filepath.Join(s.DataDir, writeResultFile), res); err != nil {
		log.Printf("[miner-poller] write-result: %v", err)
	}
	if res.OK {
		log.Printf("[miner-poller] write ok id=%s cmd=%v", req.ID, req.Cmd["cmd"])
	} else {
		log.Printf("[miner-poller] write fail id=%s: %s", req.ID, res.Error)
	}
	return true
}

func executeWrite(s Settings, req WriteRequest) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{
		ID:        req.ID,
		TS:        now,
		Action:    req.Action,
		Value:     req.Value,
		Transport: "v2",
	}

	cname := strings.TrimSpace(fmt.Sprint(req.Cmd["cmd"]))
	if cname == "" {
		res.Error = "missing cmd"
		return res
	}

	pw := strings.TrimSpace(req.Password)
	if pw == "" {
		pw = s.Password
	}
	if pw == "" {
		pw = api.DefaultAdmin
	}

	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = pw
	c.Timeout = 12 * time.Second

	params := map[string]any{}
	for k, v := range req.Cmd {
		if k == "cmd" {
			continue
		}
		params[k] = v
	}

	// Normalize common poolheat cmd aliases → V2 privileged names.
	switch cname {
	case "adjust_power_limit", "set_power_limit", "power_limit":
		cname = "adjust_power_limit"
		if params["power_limit"] == nil {
			if w := firstNonEmpty(params["watts"], params["value"], params["power"]); w != nil {
				params["power_limit"] = fmt.Sprint(w)
			}
		}
	case "set_power_pct", "power_pct":
		cname = "set_power_pct"
		if params["percent"] == nil {
			if p := firstNonEmpty(params["pct"], params["value"]); p != nil {
				params["percent"] = fmt.Sprint(p)
			}
		}
	case "sleep", "suspend":
		cname = "power_off"
	case "resume", "wakeup":
		cname = "power_on"
	case "update_pools", "set_pools":
		// Pools need special transport (NetPacket preferred, V2 flat pool1/worker1/…).
		return executeWritePools(s, req, pw)
	case "set_api_switch", "enable_api_switch", "api_switch":
		return executeAPISwitch(s, req, pw, params)
	case "reboot", "reboot_asic", "system_reboot":
		return executeReboot(s, req, pw)
	case "restart_btminer", "restart", "restart_miner", "restart_cgminer", "btminer_restart":
		return executeRestartMining(s, req, pw)
	case "factory_reset", "factory", "restore_factory", "reset_factory":
		return executeFactoryReset(s, req, pw)
	case "set_customer_sn", "customer_sn", "set_customersn", "customersn",
		"set_customer_msg", "customer_msg":
		return executeSetCustomerSN(s, req, pw, params)
	}

	// Longer timeout for privileged cmds that may stall the API briefly.
	c.Timeout = 20 * time.Second
	resp, err := c.Write(cname, params)
	if err != nil {
		// reboot/restart often drop the TCP link after accept — treat as success
		if isLinkDropAfterWrite(err) && (cname == "reboot" || cname == "restart_btminer" || cname == "restart_cgminer") {
			res.OK = true
			res.Response = map[string]any{
				"STATUS": "S", "Msg": "sent (link dropped — expected)", "transport": "v2",
			}
			res.Transport = "v2"
			return res
		}
		res.Error = err.Error()
		return res
	}
	res.Response = resp
	ok, msg := minerWriteOK(resp)
	res.OK = ok
	if !ok {
		if msg != "" {
			res.Error = msg
		} else {
			res.Error = "miner rejected command"
		}
	}
	return res
}

// executeReboot — vendor-aware full device reboot.
//
//   whatsminer: NetPacket :8889 (cmd 8) → V2 privileged reboot
//   ipollo:     LuCI /admin/ipollo_system/ipollo_reboot (sysauth) → CGMiner restart nudge
//   goldshell:  HTTP JWT + GET /mcb/reboot → CGMiner restart nudge
//   antminer/cgminer/avalon: CGMiner "restart"/"quit" (best-effort full reset)
//
// Optional req.Cmd overrides: host|ip, port, vendor|api_vendor, password.
func executeReboot(s Settings, req WriteRequest, password string) WriteResult {
	host, port, pw, vendor := writeTarget(s, req, password)
	// Override Settings for Whatsminer helpers that still read s.Host
	s.Host, s.Port, s.Password = host, port, pw

	log.Printf("[miner-poller] reboot target host=%s port=%d vendor=%q", host, port, vendor)

	switch vendor {
	case "ipollo":
		return executeRebootIpollo(s, req, host, port, pw)
	case "goldshell":
		return executeRebootGoldshell(s, req, host, port, pw)
	case "antminer", "avalon", "cgminer":
		return executeRebootCGMiner(s, req, host, port, vendor)
	}

	// Default / whatsminer path
	return executeRebootWhatsminer(s, req, pw)
}

func executeRebootIpollo(s Settings, req WriteRequest, host string, port int, password string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value}
	var lastErr error

	// 1) LuCI full OS reboot (stock UI button)
	if resp, err := ipolloLuCIReboot(host, password); err == nil {
		res.OK = true
		res.Transport = "ipollo-luci"
		res.Response = resp
		return res
	} else {
		lastErr = err
		log.Printf("[miner-poller] ipollo luci reboot %s: %v", host, err)
	}

	// 2) CGMiner process restart (device stays up; mining restarts — better than nothing)
	if resp, err := cgminerRestartMining(host, port); err == nil {
		res.OK = true
		res.Transport = "cgminer"
		res.Response = resp
		if res.Response == nil {
			res.Response = map[string]any{}
		}
		res.Response["note"] = "cgminer restart (LuCI full reboot failed)"
		res.Response["luci_error"] = fmt.Sprint(lastErr)
		return res
	} else {
		log.Printf("[miner-poller] ipollo cgminer restart %s: %v", host, err)
		if lastErr == nil {
			lastErr = err
		} else {
			lastErr = fmt.Errorf("luci: %v; cgminer: %v", lastErr, err)
		}
	}

	res.Error = lastErr.Error()
	res.Transport = "ipollo"
	return res
}

func executeRebootGoldshell(s Settings, req WriteRequest, host string, port int, password string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value}
	var lastErr error

	// 1) Cloud-box HTTP /mcb/reboot (JWT)
	if resp, err := goldshellHTTPReboot(host, password); err == nil {
		res.OK = true
		res.Transport = "goldshell-http"
		res.Response = resp
		return res
	} else {
		lastErr = err
		log.Printf("[miner-poller] goldshell http reboot %s: %v", host, err)
	}

	// 2) CGMiner on :4028
	if resp, err := cgminerRestartMining(host, port); err == nil {
		res.OK = true
		res.Transport = "cgminer"
		res.Response = resp
		if res.Response == nil {
			res.Response = map[string]any{}
		}
		res.Response["note"] = "cgminer restart (HTTP /mcb/reboot failed)"
		res.Response["http_error"] = fmt.Sprint(lastErr)
		return res
	} else {
		log.Printf("[miner-poller] goldshell cgminer restart %s: %v", host, err)
		if lastErr == nil {
			lastErr = err
		} else {
			lastErr = fmt.Errorf("http: %v; cgminer: %v", lastErr, err)
		}
	}

	res.Error = lastErr.Error()
	res.Transport = "goldshell"
	return res
}

func executeRebootCGMiner(s Settings, req WriteRequest, host string, port int, vendor string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value, Transport: "cgminer"}
	resp, err := cgminerRestartMining(host, port)
	if err != nil {
		res.Error = err.Error()
		return res
	}
	res.OK = true
	res.Response = resp
	if res.Response == nil {
		res.Response = map[string]any{}
	}
	res.Response["vendor"] = vendor
	return res
}

// executeRebootWhatsminer — original NetPacket → V2 path.
func executeRebootWhatsminer(s Settings, req WriteRequest, password string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value}
	// 1) NetPacket reboot (WMT cmd 8)
	if protocol.ProbePort(s.Host, protocol.DefaultPort, 2*time.Second) {
		type cred struct{ acc, pw string }
		tryCreds := []cred{{"super", "super"}, {"admin", password}}
		if password != "" && password != "super" {
			tryCreds = append(tryCreds, cred{"super", password})
		}
		for _, cr := range tryCreds {
			if strings.TrimSpace(cr.pw) == "" {
				continue
			}
			np := protocol.NewClient(s.Host)
			np.Account = cr.acc
			np.Password = cr.pw
			np.Timeout = 15 * time.Second
			resp, err := np.Reboot()
			if err != nil {
				// link drop after reboot command is normal
				if isLinkDropAfterWrite(err) {
					res.OK = true
					res.Transport = "netpacket"
					res.Response = map[string]any{
						"STATUS": "S", "Msg": "reboot sent (link dropped)", "transport": "netpacket",
					}
					return res
				}
				log.Printf("[miner-poller] netpacket reboot %s: %v", cr.acc, err)
				continue
			}
			if resp != nil && resp.OK {
				res.OK = true
				res.Transport = "netpacket"
				res.Response = map[string]any{
					"STATUS": "S", "Msg": "ok", "transport": "netpacket",
					"account": cr.acc,
				}
				return res
			}
			// Non-OK status: do NOT claim success (same false-OK class as factory).
			if resp != nil {
				log.Printf("[miner-poller] netpacket reboot %s status=%d %s",
					cr.acc, resp.Status, resp.StatusText)
			}
		}
	}
	// 2) V2 privileged reboot
	resp, err := cV2WriteTimeout(s, password, "reboot", nil, 20*time.Second)
	if err != nil {
		if isLinkDropAfterWrite(err) {
			res.OK = true
			res.Transport = "v2"
			res.Response = map[string]any{
				"STATUS": "S", "Msg": "reboot sent (link dropped)", "transport": "v2",
			}
			return res
		}
		res.Error = err.Error()
		res.Transport = "v2"
		return res
	}
	res.Response = resp
	res.Transport = "v2"
	ok, msg := minerWriteOK(resp)
	res.OK = ok
	if !ok {
		res.Error = msg
		if res.Error == "" {
			res.Error = "reboot rejected"
		}
	}
	return res
}

// executeSetCustomerSN writes CustomerSn on ASIC (EEPROM custom data).
// Preferred: NetPacket :8889 cmd 30 (WhatsMinerTool path).
// Fallback: V2 set_customer_msg key=CustomerSn.
// Empty sn erases CustomerSn (same as WMT erase-sn).
func executeSetCustomerSN(s Settings, req WriteRequest, password string, params map[string]any) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value}

	sn := strings.TrimSpace(fmt.Sprint(firstNonEmpty(
		params["sn"], params["val"], params["value"], params["customer_sn"],
		params["CustomerSn"], params["customersn"], req.Value,
	)))
	// allow empty to clear
	if sn == "<nil>" {
		sn = ""
	}
	// sanitize length (WMT / EEPROM practical limit)
	if len(sn) > 64 {
		sn = sn[:64]
	}

	var npLastErr error
	if protocol.ProbePort(s.Host, protocol.DefaultPort, 2*time.Second) {
		type cred struct{ acc, pw string }
		tryCreds := []cred{
			{"super", "super"},
			{"admin", password},
			{"super", password},
			{"admin", "admin"},
		}
		seen := map[string]bool{}
		for _, cr := range tryCreds {
			cr.acc = strings.TrimSpace(cr.acc)
			cr.pw = strings.TrimSpace(cr.pw)
			if cr.acc == "" || cr.pw == "" {
				continue
			}
			key := cr.acc + "\x00" + cr.pw
			if seen[key] {
				continue
			}
			seen[key] = true
			np := protocol.NewClient(s.Host)
			np.Account = cr.acc
			np.Password = cr.pw
			np.Timeout = 15 * time.Second
			resp, err := np.SetCustomerSN(sn)
			if err != nil {
				npLastErr = err
				if isAuthOrPwdErr(err) {
					log.Printf("[miner-poller] customer_sn auth %s: %v", cr.acc, err)
					continue
				}
				log.Printf("[miner-poller] customer_sn netpacket %s: %v", cr.acc, err)
				continue
			}
			if resp != nil && resp.OK {
				msg := "CustomerSn set"
				if sn == "" {
					msg = "CustomerSn cleared"
				}
				res.OK = true
				res.Transport = "netpacket"
				res.Response = map[string]any{
					"STATUS": "S", "Msg": msg, "transport": "netpacket",
					"account": cr.acc, "cmd": 30, "customer_sn": sn,
				}
				return res
			}
			if resp != nil {
				npLastErr = fmt.Errorf("status=%s", resp.StatusText)
				log.Printf("[miner-poller] customer_sn netpacket %s status=%s", cr.acc, resp.StatusText)
			}
		}
	}

	// V2 fallback: set_customer_msg key=CustomerSn val=...
	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = password
	c.Timeout = 15 * time.Second
	resp, err := c.Write("set_customer_msg", map[string]any{
		"key": "CustomerSn",
		"val": sn,
	})
	if err != nil {
		if npLastErr != nil {
			res.Error = fmt.Sprintf("netpacket: %v; v2: %v", npLastErr, err)
		} else {
			res.Error = err.Error()
		}
		return res
	}
	res.Response = resp
	res.Transport = "v2"
	ok, msg := minerWriteOK(resp)
	res.OK = ok
	if !ok {
		if msg != "" {
			res.Error = msg
		} else if npLastErr != nil {
			res.Error = fmt.Sprintf("v2 rejected; netpacket: %v", npLastErr)
		} else {
			res.Error = "set CustomerSn rejected"
		}
	}
	return res
}

// executeFactoryReset — WhatsMinerTool NetPacket cmd 10 (preferred), then V2.
//
// Lab Peak: Restore Factory Settings = cmd 10 empty + KEY1 auth.
// On-device: restore-factory-settings (wipes pools/passwords, reboot).
// If DisableFactoryMode=1, cmd 10 may ACK but do nothing — enable param 24=1 first.
//
// IMPORTANT: never report OK on non-success NetPacket status or dial/timeout
// errors (previous bug: any resp != nil → OK while ASIC never reset).
func executeFactoryReset(s Settings, req WriteRequest, password string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value}

	var npLastErr error
	var npLastStatus string

	if protocol.ProbePort(s.Host, protocol.DefaultPort, 2*time.Second) {
		type cred struct{ acc, pw string }
		tryCreds := []cred{
			{"super", "super"},
			{"admin", password},
			{"super", password},
			{"admin", "admin"},
			{"admin", "super"},
		}
		seen := map[string]bool{}
		for _, cr := range tryCreds {
			cr.acc = strings.TrimSpace(cr.acc)
			cr.pw = strings.TrimSpace(cr.pw)
			if cr.acc == "" || cr.pw == "" {
				continue
			}
			key := cr.acc + "\x00" + cr.pw
			if seen[key] {
				continue
			}
			seen[key] = true

			np := protocol.NewClient(s.Host)
			np.Account = cr.acc
			np.Password = cr.pw
			np.Timeout = 25 * time.Second

			// 1) Allow factory ops when blocked (DisableFactoryMode → 0).
			if fr, err := np.SetFactoryMode(true); err != nil {
				if !isAuthOrPwdErr(err) {
					log.Printf("[miner-poller] factory_mode enable %s: %v", cr.acc, err)
				} else {
					npLastErr = err
					log.Printf("[miner-poller] factory_reset auth %s: %v", cr.acc, err)
					continue
				}
			} else if fr != nil && !fr.OK {
				log.Printf("[miner-poller] factory_mode enable %s status=%s", cr.acc, fr.StatusText)
			}

			// 2) NetPacket cmd 10 — Restore Factory Settings
			resp, err := np.FactoryReset()
			if err != nil {
				npLastErr = err
				// Only peer-close after the write counts as accepted.
				if isLinkDropAccepted(err) {
					log.Printf("[miner-poller] netpacket factory_reset %s: link drop (accepted)", cr.acc)
					res.OK = true
					res.Transport = "netpacket"
					res.Response = map[string]any{
						"STATUS": "S", "Msg": "factory_reset sent (link dropped)",
						"transport": "netpacket", "account": cr.acc,
						"cmd": 10,
					}
					return res
				}
				log.Printf("[miner-poller] netpacket factory_reset %s: %v", cr.acc, err)
				if isAuthOrPwdErr(err) {
					continue
				}
				continue
			}
			if resp == nil {
				npLastErr = fmt.Errorf("empty response")
				continue
			}
			npLastStatus = resp.StatusText
			log.Printf("[miner-poller] netpacket factory_reset %s status=%d ok=%v text=%q",
				cr.acc, resp.Status, resp.OK, resp.StatusText)

			// Require real success ACK (status 0 / OK). Non-OK was previously
			// mis-reported as WRITE … OK while the unit never reset.
			if resp.OK && (resp.Status == 0 || resp.StatusText == "ok" || resp.StatusText == "") {
				// Lab: unit drops off LAN shortly after. Best-effort second push
				// (some FW ACK then need a nudge — ignore errors).
				_, _ = np.FactoryReset()

				res.OK = true
				res.Transport = "netpacket"
				res.Response = map[string]any{
					"STATUS": "S", "Msg": "factory_reset accepted",
					"transport": "netpacket", "account": cr.acc,
					"status": resp.Status, "status_text": resp.StatusText,
					"cmd": 10,
				}
				return res
			}

			npLastErr = fmt.Errorf("netpacket status=%d %s", resp.Status, resp.StatusText)
			// Wrong password / permission → try next credential set.
			if resp.Status == uint16(protocol.StatusIncorrectPwd) ||
				resp.Status == uint16(protocol.StatusNeedChangePwd) {
				continue
			}
		}
	} else {
		log.Printf("[miner-poller] factory_reset: NetPacket :%d not open on %s — try V2",
			protocol.DefaultPort, s.Host)
	}

	// 3) V2 privileged factory_reset (needs Miner API Switch ON)
	resp, err := cV2WriteTimeout(s, password, "factory_reset", nil, 25*time.Second)
	if err != nil {
		if isLinkDropAccepted(err) {
			res.OK = true
			res.Transport = "v2"
			res.Response = map[string]any{
				"STATUS": "S", "Msg": "factory_reset sent (link dropped)", "transport": "v2",
			}
			return res
		}
		msg := err.Error()
		if npLastErr != nil {
			msg = fmt.Sprintf("netpacket: %v; v2: %s", npLastErr, msg)
		}
		if npLastStatus != "" {
			msg = fmt.Sprintf("%s (last netpacket status: %s)", msg, npLastStatus)
		}
		res.Error = msg
		res.Transport = "v2"
		return res
	}
	res.Response = resp
	res.Transport = "v2"
	ok, msg := minerWriteOK(resp)
	res.OK = ok
	if !ok {
		res.Error = msg
		if res.Error == "" {
			res.Error = "factory_reset rejected"
		}
		if npLastErr != nil {
			res.Error = fmt.Sprintf("%s (netpacket earlier: %v)", res.Error, npLastErr)
		}
	}
	return res
}

// executeRestartMining restarts the mining process (not always full OS reboot).
//
//   whatsminer: V2 restart_btminer / restart_cgminer
//   ipollo / goldshell / antminer / avalon / cgminer: CGMiner {"command":"restart"}
//
// Optional req.Cmd overrides: host|ip, port, vendor|api_vendor, password.
func executeRestartMining(s Settings, req WriteRequest, password string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{ID: req.ID, TS: now, Action: req.Action, Value: req.Value, Transport: "v2"}

	host, port, pw, vendor := writeTarget(s, req, password)
	s.Host, s.Port, s.Password = host, port, pw

	log.Printf("[miner-poller] restart_mining target host=%s port=%d vendor=%q", host, port, vendor)

	switch vendor {
	case "ipollo", "goldshell", "antminer", "avalon", "cgminer":
		resp, err := cgminerRestartMining(host, port)
		if err != nil {
			// Goldshell: try HTTP reboot as last resort for "restart" when :4028 down
			if vendor == "goldshell" {
				if r2, err2 := goldshellHTTPReboot(host, pw); err2 == nil {
					res.OK = true
					res.Transport = "goldshell-http"
					res.Response = r2
					if res.Response == nil {
						res.Response = map[string]any{}
					}
					res.Response["note"] = "full reboot via /mcb/reboot (cgminer restart unavailable)"
					return res
				}
			}
			if vendor == "ipollo" {
				if r2, err2 := ipolloLuCIReboot(host, pw); err2 == nil {
					res.OK = true
					res.Transport = "ipollo-luci"
					res.Response = r2
					if res.Response == nil {
						res.Response = map[string]any{}
					}
					res.Response["note"] = "full reboot via LuCI (cgminer restart unavailable)"
					return res
				}
			}
			res.Error = err.Error()
			res.Transport = "cgminer"
			return res
		}
		res.OK = true
		res.Transport = "cgminer"
		res.Response = resp
		return res
	}

	// Whatsminer (default)
	var lastErr error
	for _, cmd := range []string{"restart_btminer", "restart_cgminer"} {
		resp, err := cV2WriteTimeout(s, pw, cmd, nil, 25*time.Second)
		if err != nil {
			lastErr = err
			if isLinkDropAfterWrite(err) {
				res.OK = true
				res.Response = map[string]any{
					"STATUS": "S", "Msg": cmd + " sent (link dropped)", "cmd": cmd,
				}
				return res
			}
			log.Printf("[miner-poller] %s: %v", cmd, err)
			continue
		}
		ok, msg := minerWriteOK(resp)
		if ok {
			res.OK = true
			res.Response = resp
			if res.Response == nil {
				res.Response = map[string]any{}
			}
			res.Response["cmd"] = cmd
			return res
		}
		lastErr = fmt.Errorf("%s", msg)
		log.Printf("[miner-poller] %s rejected: %s", cmd, msg)
	}
	if lastErr != nil {
		res.Error = lastErr.Error()
	} else {
		res.Error = "restart_btminer failed"
	}
	return res
}

func cV2WriteTimeout(s Settings, password, cmd string, params map[string]any, to time.Duration) (map[string]any, error) {
	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = password
	if c.Password == "" {
		c.Password = api.DefaultAdmin
	}
	c.Timeout = to
	if params == nil {
		params = map[string]any{}
	}
	return c.Write(cmd, params)
}

// isLinkDropAfterWrite — legacy helper used by several write paths.
// Prefer isLinkDropAccepted for destructive cmds (factory/reboot).
func isLinkDropAfterWrite(err error) bool {
	return isLinkDropAccepted(err)
}

// isLinkDropAccepted is true only when the peer almost certainly closed the
// TCP session after receiving the command (unit rebooting / factory wipe).
// Dial failures, "connection refused", and empty/timeout-before-write must
// NOT be treated as success — that caused false "WRITE factory_reset OK".
func isLinkDropAccepted(err error) bool {
	if err == nil {
		return false
	}
	low := strings.ToLower(err.Error())
	// Never accept pure dial / unreachable errors.
	if strings.Contains(low, "dial") ||
		strings.Contains(low, "connection refused") ||
		strings.Contains(low, "no route") ||
		strings.Contains(low, "network is unreachable") ||
		strings.Contains(low, "empty response") {
		return false
	}
	for _, t := range []string{
		"connection reset",
		"broken pipe",
		"forcibly closed",
		"use of closed",
		"wsasend",
		"wsarecv",
	} {
		if strings.Contains(low, t) {
			return true
		}
	}
	// EOF after a write often means the unit cut the link on accept.
	if strings.Contains(low, "eof") {
		return true
	}
	// Read i/o timeout *after* send can happen when the unit freezes mid-reboot.
	// Only accept when the error looks like a read deadline, not dial.
	if (strings.Contains(low, "i/o timeout") || strings.Contains(low, "timeout")) &&
		(strings.Contains(low, "read") || strings.Contains(low, "wait")) {
		return true
	}
	return false
}

func isAuthOrPwdErr(err error) bool {
	if err == nil {
		return false
	}
	low := strings.ToLower(err.Error())
	return strings.Contains(low, "incorrect password") ||
		strings.Contains(low, "handshake failed") ||
		strings.Contains(low, "auth") ||
		strings.Contains(low, "need change pwd")
}

// executeWritePools applies stratum pools via NetPacket :8889 (WhatsMinerTool)
// or classic V2 update_pools with flat pool1/worker1/passwd1… params.
// serve sends {"cmd":"update_pools","pools":[{url,user,pass},…]} — never nested
// "pools" into V2 as-is (ASIC rejects that).
func executeWritePools(s Settings, req WriteRequest, password string) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{
		ID:     req.ID,
		TS:     now,
		Action: req.Action,
		Value:  req.Value,
	}

	slots := extractPoolSlots(req.Cmd)
	if len(slots) == 0 {
		res.Error = "update_pools: no pool slots (need url+user)"
		return res
	}
	restart := true
	if v, ok := req.Cmd["restart_mining"]; ok {
		restart = asBool(v, true)
	} else if v, ok := req.Cmd["restart"]; ok {
		restart = asBool(v, true)
	}
	coin := strings.TrimSpace(fmt.Sprint(firstNonEmpty(req.Cmd["coin_type"], req.Cmd["coin"])))
	if coin == "<nil>" {
		coin = ""
	}

	// 1) NetPacket SET_POOLS (cmd 2) — same path as WhatsMinerTool / UniversalMiner
	// Credentials: WMT defaults super/super (not the public API password).
	if protocol.ProbePort(s.Host, protocol.DefaultPort, 2*time.Second) {
		npPools := make([]map[string]string, 0, len(slots))
		for i, sl := range slots {
			if sl.url == "" && sl.user == "" {
				continue
			}
			npPools = append(npPools, map[string]string{
				"url":      sl.url,
				"user":     sl.user,
				"password": sl.pass,
				"index":    fmt.Sprint(i),
			})
		}
		type cred struct{ acc, pw string }
		tryCreds := []cred{{"super", "super"}, {"admin", password}}
		if password != "" && password != "super" {
			tryCreds = append(tryCreds, cred{"super", password})
		}
		for _, cr := range tryCreds {
			if strings.TrimSpace(cr.pw) == "" {
				continue
			}
			np := protocol.NewClient(s.Host)
			np.Timeout = 15 * time.Second
			np.Account = cr.acc
			np.Password = cr.pw
			resp, err := np.SetPools(npPools)
			if err != nil {
				log.Printf("[miner-poller] netpacket SetPools %s: %v", cr.acc, err)
				continue
			}
			if resp == nil || !resp.OK {
				if resp != nil {
					log.Printf("[miner-poller] netpacket SetPools %s status=%s", cr.acc, resp.StatusText)
				}
				continue
			}
			out := map[string]any{
				"STATUS":    "S",
				"Msg":       "ok",
				"transport": "netpacket",
				"pools_n":   len(npPools),
			}
			if coin != "" {
				if _, errC := np.Request(protocol.CmdSetCoin, []byte(coin)); errC == nil {
					out["coin_type"] = coin
				} else {
					out["coin_error"] = errC.Error()
				}
			}
			if restart {
				if _, errR := cV2Write(s, password, "restart_btminer", nil); errR != nil {
					out["restart_error"] = errR.Error()
					out["restart_mining"] = false
				} else {
					out["restart_mining"] = true
				}
			}
			res.OK = true
			res.Transport = "netpacket"
			res.Response = out
			return res
		}
		log.Printf("[miner-poller] netpacket SetPools failed all creds — try V2")
	}

	// 2) Classic V2 update_pools — flat pool1/worker1/passwd1 …
	params := map[string]any{}
	for i := 0; i < 3; i++ {
		n := i + 1
		url, user, pass := "", "", "x"
		if i < len(slots) {
			url, user, pass = slots[i].url, slots[i].user, slots[i].pass
		}
		if pass == "" {
			pass = "x"
		}
		params[fmt.Sprintf("pool%d", n)] = url
		params[fmt.Sprintf("worker%d", n)] = user
		params[fmt.Sprintf("passwd%d", n)] = pass
	}
	resp, err := cV2Write(s, password, "update_pools", params)
	if err != nil {
		res.Error = err.Error()
		res.Transport = "v2"
		return res
	}
	res.Response = resp
	res.Transport = "v2"
	ok, msg := minerWriteOK(resp)
	res.OK = ok
	if !ok {
		if msg != "" {
			res.Error = msg
		} else {
			res.Error = "miner rejected update_pools"
		}
		return res
	}
	if restart {
		if _, errR := cV2Write(s, password, "restart_btminer", nil); errR != nil {
			if res.Response == nil {
				res.Response = map[string]any{}
			}
			res.Response["restart_error"] = errR.Error()
			res.Response["restart_mining"] = false
		} else if res.Response != nil {
			res.Response["restart_mining"] = true
		}
	}
	if res.Response != nil {
		res.Response["transport"] = "v2"
	}
	return res
}

type poolSlot struct {
	url, user, pass string
}

func extractPoolSlots(cmd map[string]any) []poolSlot {
	if cmd == nil {
		return nil
	}
	// Already flat V2 style?
	if v, ok := cmd["pool1"]; ok && strings.TrimSpace(fmt.Sprint(v)) != "" {
		out := make([]poolSlot, 0, 3)
		for i := 1; i <= 3; i++ {
			url := strings.TrimSpace(fmt.Sprint(cmd[fmt.Sprintf("pool%d", i)]))
			user := strings.TrimSpace(fmt.Sprint(firstNonEmpty(
				cmd[fmt.Sprintf("worker%d", i)],
				cmd[fmt.Sprintf("user%d", i)],
			)))
			pass := strings.TrimSpace(fmt.Sprint(firstNonEmpty(
				cmd[fmt.Sprintf("passwd%d", i)],
				cmd[fmt.Sprintf("pass%d", i)],
				cmd[fmt.Sprintf("password%d", i)],
			)))
			if pass == "" || pass == "<nil>" {
				pass = "x"
			}
			if url == "<nil>" {
				url = ""
			}
			if user == "<nil>" {
				user = ""
			}
			if url == "" && user == "" {
				continue
			}
			out = append(out, poolSlot{url: url, user: user, pass: pass})
		}
		return out
	}
	raw := cmd["pools"]
	list, ok := raw.([]any)
	if !ok {
		// json may decode as []map after round-trip from some clients
		if arr, ok2 := raw.([]map[string]any); ok2 {
			for _, m := range arr {
				list = append(list, m)
			}
			ok = true
		}
	}
	if !ok || len(list) == 0 {
		return nil
	}
	out := make([]poolSlot, 0, 3)
	for _, item := range list {
		if len(out) >= 3 {
			break
		}
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		url := strings.TrimSpace(fmt.Sprint(firstNonEmpty(m["url"], m["pool"], m["URL"])))
		user := strings.TrimSpace(fmt.Sprint(firstNonEmpty(m["user"], m["worker"], m["User"])))
		pass := strings.TrimSpace(fmt.Sprint(firstNonEmpty(m["pass"], m["password"], m["passwd"], m["Pass"])))
		if url == "<nil>" {
			url = ""
		}
		if user == "<nil>" {
			user = ""
		}
		if pass == "" || pass == "<nil>" {
			pass = "x"
		}
		if url == "" && user == "" {
			continue
		}
		out = append(out, poolSlot{url: url, user: user, pass: pass})
	}
	return out
}

func cV2Write(s Settings, password, cmd string, params map[string]any) (map[string]any, error) {
	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = password
	if c.Password == "" {
		c.Password = api.DefaultAdmin
	}
	c.Timeout = 12 * time.Second
	if params == nil {
		params = map[string]any{}
	}
	return c.Write(cmd, params)
}

func asBool(v any, def bool) bool {
	if v == nil {
		return def
	}
	switch t := v.(type) {
	case bool:
		return t
	case float64:
		return t != 0
	case int:
		return t != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(t))
		if s == "" {
			return def
		}
		return s == "1" || s == "true" || s == "yes" || s == "on"
	default:
		s := strings.ToLower(strings.TrimSpace(fmt.Sprint(v)))
		if s == "" || s == "<nil>" {
			return def
		}
		return s == "1" || s == "true" || s == "yes" || s == "on"
	}
}

// executeAPISwitch enables Miner API Switch via NetPacket :8889 (WMT path).
func executeAPISwitch(s Settings, req WriteRequest, password string, params map[string]any) WriteResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := WriteResult{
		ID:        req.ID,
		TS:        now,
		Action:    req.Action,
		Value:     req.Value,
		Transport: "netpacket",
	}
	on := true
	if v := firstNonEmpty(params["enable"], params["on"], params["value"], params["api_switch"]); v != nil {
		on = asBool(v, true)
	}
	if !protocol.ProbePort(s.Host, protocol.DefaultPort, 2*time.Second) {
		res.Error = "netpacket :8889 not reachable for api_switch"
		return res
	}
	type cred struct{ acc, pw string }
	tryCreds := []cred{{"super", "super"}, {"admin", password}}
	if password != "" && password != "super" {
		tryCreds = append(tryCreds, cred{"super", password})
	}
	var lastErr string
	for _, cr := range tryCreds {
		if strings.TrimSpace(cr.pw) == "" {
			continue
		}
		np := protocol.NewClient(s.Host)
		np.Timeout = 12 * time.Second
		np.Account = cr.acc
		np.Password = cr.pw
		resp, err := np.SetAPISwitch(on)
		if err != nil {
			lastErr = err.Error()
			continue
		}
		if resp != nil && resp.OK {
			res.OK = true
			res.Response = map[string]any{
				"STATUS":    "S",
				"Msg":       "ok",
				"transport": "netpacket",
				"enabled":   on,
			}
			return res
		}
		if resp != nil {
			lastErr = resp.StatusText
		}
	}
	if lastErr == "" {
		lastErr = "SetAPISwitch failed"
	}
	res.Error = lastErr
	return res
}

// minerWriteOK mirrors serve.py _miner_cmd_result loosely.
func minerWriteOK(resp map[string]any) (bool, string) {
	if resp == nil {
		return false, "empty response"
	}
	// encrypted opaque — treat as ok
	if resp["STATUS"] == nil && resp["status"] == nil && resp["Msg"] == nil && resp["msg"] == nil {
		if resp["enc"] != nil || resp["data"] != nil {
			return true, ""
		}
	}
	status := resp["STATUS"]
	if status == nil {
		status = resp["status"]
	}
	msg := resp["Msg"]
	if msg == nil {
		msg = resp["msg"]
	}
	if arr, ok := status.([]any); ok && len(arr) > 0 {
		if m, ok := arr[0].(map[string]any); ok {
			status = firstNonEmpty(m["STATUS"], m["status"])
			if msg == nil {
				msg = firstNonEmpty(m["Msg"], m["msg"])
			}
		}
	}
	st := strings.ToUpper(strings.TrimSpace(fmt.Sprint(status)))
	msgS := strings.TrimSpace(fmt.Sprint(msg))
	if st == "E" || st == "F" || st == "N" || st == "ERROR" || st == "FAIL" || st == "FAILED" {
		return false, msgS
	}
	low := strings.ToLower(msgS)
	for _, t := range []string{"error", "fail", "invalid", "denied", "reject", "can't access write", "enc json load"} {
		if strings.Contains(low, t) && low != "ok" && low != "success" {
			return false, msgS
		}
	}
	return true, msgS
}
