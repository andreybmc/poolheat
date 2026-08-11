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
	switch strings.ToLower(cname) {
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
