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
	if s.DryRun {
		res.OK = true
		res.Response = map[string]any{"STATUS": "S", "Msg": "dry_run"}
		res.Transport = "dry_run"
		return res
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
	case "set_pools":
		cname = "update_pools"
	}

	resp, err := c.Write(cname, params)
	if err != nil {
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
