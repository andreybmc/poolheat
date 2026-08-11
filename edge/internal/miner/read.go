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
)

// File IPC for on-demand ASIC reads (UI / external serve → poller → :4028).
// Architecture: poller owns miner TCP; UI only enqueues jobs and reads results.
//
//	miner_read_req.json  → poller executes → miner_read_result.json
const (
	readReqFile    = "miner_read_req.json"
	readResultFile = "miner_read_result.json"
)

// ReadRequest is enqueued by serve/UI (atomic JSON).
type ReadRequest struct {
	ID       string         `json:"id"`
	TS       float64        `json:"ts"`
	Cmd      map[string]any `json:"cmd"`
	Password string         `json:"password,omitempty"`
}

// ReadResult is written by poller after executing ReadRequest.
type ReadResult struct {
	ID        string         `json:"id"`
	OK        bool           `json:"ok"`
	Response  map[string]any `json:"response,omitempty"`
	Error     string         `json:"error,omitempty"`
	TS        float64        `json:"ts"`
	Transport string         `json:"transport,omitempty"`
}

// ProcessPendingRead handles one miner_read_req.json (pools, summary, …).
// Returns true if a request was consumed.
func ProcessPendingRead(s Settings) bool {
	path := filepath.Join(s.DataDir, readReqFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var req ReadRequest
	if err := json.Unmarshal(b, &req); err != nil {
		log.Printf("[miner-poller] read-req json: %v", err)
		_ = os.Remove(path)
		return true
	}
	if strings.TrimSpace(req.ID) == "" || req.Cmd == nil {
		log.Printf("[miner-poller] read-req incomplete id=%q", req.ID)
		_ = os.Remove(path)
		return true
	}
	_ = os.Remove(path)

	res := executeRead(s, req)
	if err := writeJSONAtomic(filepath.Join(s.DataDir, readResultFile), res); err != nil {
		log.Printf("[miner-poller] read-result: %v", err)
	}
	if res.OK {
		log.Printf("[miner-poller] read ok id=%s cmd=%v", req.ID, req.Cmd["cmd"])
	} else {
		log.Printf("[miner-poller] read fail id=%s: %s", req.ID, res.Error)
	}
	return true
}

func executeRead(s Settings, req ReadRequest) ReadResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := ReadResult{
		ID:        req.ID,
		TS:        now,
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
	c.Timeout = 10 * time.Second

	// Strip cmd; remaining keys are V2 params (rare for read).
	params := map[string]any{}
	for k, v := range req.Cmd {
		if k == "cmd" {
			continue
		}
		params[k] = v
	}

	// Normalize aliases used by UI / legacy serve.
	low := strings.ToLower(cname)
	switch low {
	case "pools", "get_pools", "pool":
		cname = "pools"
	case "summary", "get_summary":
		cname = "summary"
	case "status", "get_status":
		cname = "status"
	case "devs", "get_devs", "devdetails":
		cname = "devs"
	case "get_psu", "psu":
		cname = "get_psu"
	case "get_version", "version":
		cname = "get_version"
	case "get_miner_info", "miner_info":
		cname = "get_miner_info"
	case "get_error_code", "error_code", "edevs":
		// keep as-is if already a known V2 name
		if strings.EqualFold(cname, "error_code") {
			cname = "get_error_code"
		}
	case "get.device.info", "device_info", "v3_device_info", "get_device_info":
		// API v3 TCP :4433 — PCB SN / detect-hash-rate / liquid identity
		return executeV3DeviceInfo(s, req)
	case "get.device.custom_data", "device_custom_data", "get_device_custom_data",
		"custom_data", "get_customer_sn", "customer_sn", "get_customer_msg":
		// CustomerSn (EEPROM custom_data) — V3 preferred, V2 get_customer_msg fallback
		return executeCustomerSNRead(s, req, pw)
	}

	resp, err := c.Read(cname, params)
	if err != nil {
		res.Error = err.Error()
		return res
	}
	if resp == nil {
		res.Error = "empty response"
		return res
	}
	// Surface STATUS=E / Code errors so UI does not treat empty payload as success.
	if st, _ := resp["STATUS"].(string); strings.EqualFold(st, "E") || strings.EqualFold(st, "F") {
		msg := fmt.Sprint(resp["Msg"])
		if msg == "" || msg == "<nil>" {
			msg = fmt.Sprint(resp["Description"])
		}
		if msg == "" || msg == "<nil>" {
			msg = fmt.Sprintf("miner STATUS=%s", st)
		}
		res.Error = msg
		res.Response = resp
		res.Transport = "v2"
		return res
	}
	res.OK = true
	res.Response = resp
	res.Transport = "v2"
	return res
}

// executeV3DeviceInfo runs get.device.info over :4433 (EEPROM SN, detect-hash-rate).
func executeV3DeviceInfo(s Settings, req ReadRequest) ReadResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := ReadResult{ID: req.ID, TS: now, Transport: "v3"}
	v3 := api.NewV3(s.Host)
	v3.Timeout = 10 * time.Second
	// optional param filter from cmd.param (e.g. "power", "miner")
	var param any
	if p, ok := req.Cmd["param"]; ok {
		param = p
	}
	msg, err := v3.DeviceInfoMsg(param)
	if err != nil {
		// retry full info once
		msg, err = v3.DeviceInfoMsg(nil)
	}
	if err != nil {
		res.Error = err.Error()
		return res
	}
	if msg == nil {
		res.Error = "empty v3 device.info msg"
		return res
	}
	// Shape like V2 so serve miner_cmd(...).get("Msg") works
	res.OK = true
	res.Response = map[string]any{
		"STATUS": "S",
		"Msg":    msg,
		"Code":   0,
	}
	res.Transport = "v3"
	return res
}

// executeCustomerSNRead fetches CustomerSn via V3 get.device.custom_data,
// falling back to V2 get_customer_msg (token may be required on some FW).
func executeCustomerSNRead(s Settings, req ReadRequest, password string) ReadResult {
	now := float64(time.Now().UnixNano()) / 1e9
	res := ReadResult{ID: req.ID, TS: now, Transport: "v3"}

	// Prefer V3 get.device.custom_data (:4433)
	v3 := api.NewV3(s.Host)
	v3.Timeout = 10 * time.Second
	if raw, err := v3.Get("get.device.custom_data"); err == nil && raw != nil {
		msg := extractCustomDataMsg(raw)
		if msg != nil {
			sn := pickCustomerSN(msg)
			res.OK = true
			res.Transport = "v3"
			res.Response = map[string]any{
				"STATUS":      "S",
				"Msg":         msg,
				"Code":        0,
				"customer_sn": sn,
				"CustomerSn":  sn,
			}
			return res
		}
		// some FW return top-level fields without nested msg
		if sn := pickCustomerSN(raw); sn != "" {
			res.OK = true
			res.Transport = "v3"
			res.Response = map[string]any{
				"STATUS":      "S",
				"Msg":         raw,
				"Code":        0,
				"customer_sn": sn,
				"CustomerSn":  sn,
			}
			return res
		}
	}

	// V2 fallback: get_customer_msg (may need write-token)
	c := api.NewV2(s.Host)
	c.Port = s.Port
	c.Password = password
	c.Timeout = 10 * time.Second
	resp, err := c.Write("get_customer_msg", nil)
	if err != nil {
		resp, err = c.Read("get_customer_msg", nil)
	}
	if err != nil {
		res.Error = err.Error()
		return res
	}
	if resp == nil {
		res.Error = "empty get_customer_msg"
		return res
	}
	if st, _ := resp["STATUS"].(string); strings.EqualFold(st, "E") || strings.EqualFold(st, "F") {
		msg := fmt.Sprint(resp["Msg"])
		if msg == "" || msg == "<nil>" {
			msg = fmt.Sprint(resp["Description"])
		}
		if msg == "" || msg == "<nil>" {
			msg = fmt.Sprintf("miner STATUS=%s", st)
		}
		res.Error = msg
		res.Response = resp
		res.Transport = "v2"
		return res
	}
	// Msg may be dict with CustomerSn / msg0..msg9
	var sn string
	if m, ok := resp["Msg"].(map[string]any); ok {
		sn = pickCustomerSN(m)
	} else {
		sn = pickCustomerSN(resp)
	}
	res.OK = true
	res.Transport = "v2"
	res.Response = resp
	if res.Response == nil {
		res.Response = map[string]any{}
	}
	res.Response["customer_sn"] = sn
	res.Response["CustomerSn"] = sn
	return res
}

func extractCustomDataMsg(raw map[string]any) map[string]any {
	if raw == nil {
		return nil
	}
	if m, ok := raw["msg"].(map[string]any); ok {
		return m
	}
	if m, ok := raw["Msg"].(map[string]any); ok {
		return m
	}
	// sometimes the whole response is the payload
	if _, ok := raw["CustomerSn"]; ok {
		return raw
	}
	if _, ok := raw["customer_sn"]; ok {
		return raw
	}
	return nil
}

func pickCustomerSN(m map[string]any) string {
	if m == nil {
		return ""
	}
	for _, k := range []string{"CustomerSn", "customer_sn", "customersn", "CustomerSN", "sn"} {
		if v, ok := m[k]; ok && v != nil {
			s := strings.TrimSpace(fmt.Sprint(v))
			if s != "" && s != "<nil>" {
				return s
			}
		}
	}
	return ""
}
