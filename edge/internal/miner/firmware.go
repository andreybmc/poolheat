package miner

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/andreybmc/wm-lib/protocol"
)

// File IPC: firmware flash + export log (NetPacket :8889 via wm-lib).
// serve never opens :8889 / never loads whatsminer.
const (
	fwFlashReqFile    = "firmware_flash_req.json"
	fwFlashStatusFile = "firmware_flash_status.json"
	exportLogReqFile  = "export_log_req.json"
	exportLogResultFile = "export_log_result.json"
	exportLogDir      = "export_logs"
)

var (
	fwFlashMu   sync.Mutex
	fwFlashBusy bool
)

// ProcessPendingFirmwareFlash runs one flash job if requested.
// Long-running (minutes); call from main loop (blocks live during flash).
func ProcessPendingFirmwareFlash(s Settings) bool {
	path := filepath.Join(s.DataDir, fwFlashReqFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var req map[string]any
	if json.Unmarshal(b, &req) != nil {
		_ = os.Remove(path)
		return true
	}
	_ = os.Remove(path)

	fwFlashMu.Lock()
	if fwFlashBusy {
		fwFlashMu.Unlock()
		writeFlashStatus(s.DataDir, map[string]any{
			"busy":  true,
			"stage": "busy",
			"error": "firmware flash already in progress",
			"ok":    false,
			"id":    str(req["id"]),
		})
		return true
	}
	fwFlashBusy = true
	fwFlashMu.Unlock()
	defer func() {
		fwFlashMu.Lock()
		fwFlashBusy = false
		fwFlashMu.Unlock()
	}()

	runFirmwareFlash(s, req)
	return true
}

func runFirmwareFlash(s Settings, req map[string]any) {
	id := str(req["id"])
	filePath := str(firstNonEmpty(req["path"], req["file"], req["firmware_path"]))
	platform := str(req["platform"])
	if platform == "" {
		platform = "h616"
	}
	pw := str(req["password"])
	if pw == "" {
		pw = s.Password
	}
	filename := str(req["filename"])
	if filename == "" && filePath != "" {
		filename = filepath.Base(filePath)
	}

	writeFlashStatus(s.DataDir, map[string]any{
		"busy":        true,
		"pct":         0,
		"stage":       "prepare",
		"error":       nil,
		"result":      nil,
		"id":          id,
		"filename":    filename,
		"started_at":  time.Now().Format("2006-01-02T15:04:05"),
		"finished_at": nil,
		"status_log":  []string{},
	})

	if filePath == "" {
		failFlash(s.DataDir, id, "missing firmware path")
		return
	}
	if _, err := os.Stat(filePath); err != nil {
		failFlash(s.DataDir, id, fmt.Sprintf("firmware file: %v", err))
		return
	}

	writeFlashStatusMerge(s.DataDir, map[string]any{"stage": "extract", "pct": 2})
	img, meta, err := protocol.LoadFirmwareFile(filePath, platform)
	if err != nil {
		failFlash(s.DataDir, id, fmt.Sprintf("extract: %v", err))
		return
	}
	log.Printf("[miner-poller] firmware extract id=%s platform=%s bytes=%d meta=%v",
		id, platform, len(img), meta)
	// Sanity: WMT lab container ~12MB; multi-GB means extract failed → ASIC RST mid-upload
	if len(img) > 40_000_000 {
		failFlash(s.DataDir, id, fmt.Sprintf(
			"image too large (%d B) after extract — wrong package slice? platform=%s meta=%v",
			len(img), platform, meta))
		return
	}
	if len(img) < 100_000 {
		failFlash(s.DataDir, id, fmt.Sprintf("image too small (%d B) after extract", len(img)))
		return
	}
	writeFlashStatusMerge(s.DataDir, map[string]any{
		"stage":   "auth",
		"pct":     4,
		"extract": meta,
		"bytes":   len(img),
	})

	if !protocol.ProbePort(s.Host, protocol.DefaultPort, 3*time.Second) {
		failFlash(s.DataDir, id, fmt.Sprintf("netpacket :8889 not reachable on %s", s.Host))
		return
	}

	// Try WMT default super/super first, then API password (same as WhatsMinerTool).
	// Do NOT retry another account after mid-upload RST (ASIC may be wedged).
	type cred struct{ acc, pw string }
	tryCreds := []cred{{"super", "super"}, {"admin", pw}}
	if pw != "" && pw != "super" {
		tryCreds = append(tryCreds, cred{"super", pw})
	}

	var statusLog []string
	var out map[string]any
	var lastErr error
	okFlash := false
	var lastProgWrite time.Time
	for _, cr := range tryCreds {
		if strings.TrimSpace(cr.pw) == "" {
			continue
		}
		writeFlashStatusMerge(s.DataDir, map[string]any{
			"stage":       "auth",
			"pct":         4,
			"status_text": fmt.Sprintf("netpacket as %s…", cr.acc),
		})
		np := protocol.NewClient(s.Host)
		np.Account = cr.acc
		np.Password = cr.pw
		np.Timeout = 10 * time.Minute
		statusLog = nil
		streamStarted := false
		out, lastErr = np.UpdateFirmware(img,
			protocol.WithFirmwarePoll(true),
			protocol.WithFirmwarePollAttempts(45),
			protocol.WithFirmwareProgress(func(stage string, pct float64, extra map[string]any) {
				if stage == "upload" || stage == "cmd7" {
					streamStarted = true
				}
				// Throttle disk writes — blocking JSON on flash/USB caused TCP stalls → RST
				now := time.Now()
				if stage == "upload" && !lastProgWrite.IsZero() && now.Sub(lastProgWrite) < 500*time.Millisecond {
					return
				}
				lastProgWrite = now
				st := map[string]any{
					"busy":  true,
					"stage": stage,
					"pct":   pct,
					"id":    id,
				}
				if extra != nil {
					if t, ok := extra["status_text"]; ok && t != nil {
						txt := truncStr(fmt.Sprint(t), 240)
						st["status_text"] = txt
						statusLog = append(statusLog, txt)
						if len(statusLog) > 40 {
							statusLog = statusLog[len(statusLog)-40:]
						}
						st["status_log"] = append([]string{}, statusLog...)
					}
				}
				writeFlashStatusMerge(s.DataDir, st)
			}),
		)
		if lastErr != nil {
			log.Printf("[miner-poller] firmware auth/upload %s: %v", cr.acc, lastErr)
			// After stream started (cmd7/upload), do not burn other creds — RST is not "wrong password"
			if streamStarted || strings.Contains(strings.ToLower(lastErr.Error()), "upload:") {
				break
			}
			continue
		}
		okFlash = true
		break
	}
	if !okFlash {
		msg := "netpacket firmware failed"
		if lastErr != nil {
			msg = lastErr.Error()
		}
		failFlash(s.DataDir, id, msg)
		return
	}
	ok := false
	if out != nil {
		if v, okb := out["ok"].(bool); okb {
			ok = v
		}
	}
	upgrade := ""
	if out != nil {
		upgrade = fmt.Sprint(out["upgrade"])
	}
	stage := "done"
	if ok && strings.Contains(strings.ToLower(upgrade), "success") {
		stage = "success"
	} else if !ok {
		stage = "error"
	}
	errMsg := any(nil)
	if !ok {
		if out != nil && out["error"] != nil {
			errMsg = out["error"]
		} else {
			errMsg = "firmware flash failed"
		}
	}
	writeFlashStatus(s.DataDir, map[string]any{
		"busy":        false,
		"pct":         100,
		"stage":       stage,
		"error":       errMsg,
		"result":      out,
		"ok":          ok,
		"id":          id,
		"filename":    filename,
		"extract":     meta,
		"bytes":       len(img),
		"upgrade":     upgrade,
		"status_log":  statusLog,
		"transport":   "netpacket",
		"finished_at": time.Now().Format("2006-01-02T15:04:05"),
	})
	if ok {
		log.Printf("[miner-poller] firmware flash ok id=%s file=%s bytes=%d upgrade=%s",
			id, filename, len(img), upgrade)
	} else {
		log.Printf("[miner-poller] firmware flash fail id=%s: %v", id, errMsg)
	}
	// Drop staged cache under DATA/miner_fw_cache/<id>/ after attempt
	if asBool(req["cleanup_cache"], true) {
		cleanupFirmwareCache(req)
	}
}

func cleanupFirmwareCache(req map[string]any) {
	cacheDir := str(req["cache_dir"])
	if cacheDir == "" {
		// …/miner_fw_cache/<id>/image.bin → parent dir
		p := str(firstNonEmpty(req["path"], req["file"], req["firmware_path"]))
		if p != "" {
			cacheDir = filepath.Dir(p)
		}
	}
	if cacheDir == "" {
		return
	}
	// safety: only remove …/miner_fw_cache/<req_id>
	if filepath.Base(filepath.Dir(cacheDir)) != "miner_fw_cache" {
		log.Printf("[miner-poller] skip cache cleanup (not under miner_fw_cache): %s", cacheDir)
		return
	}
	if err := os.RemoveAll(cacheDir); err != nil {
		log.Printf("[miner-poller] cache cleanup %s: %v", cacheDir, err)
	} else {
		log.Printf("[miner-poller] cache cleaned %s", cacheDir)
	}
}

func failFlash(dataDir, id, msg string) {
	writeFlashStatus(dataDir, map[string]any{
		"busy":        false,
		"stage":       "error",
		"error":       msg,
		"ok":          false,
		"id":          id,
		"pct":         0,
		"finished_at": time.Now().Format("2006-01-02T15:04:05"),
	})
	log.Printf("[miner-poller] firmware flash fail: %s", msg)
}

func writeFlashStatus(dataDir string, st map[string]any) {
	_ = writeJSONAtomic(filepath.Join(dataDir, fwFlashStatusFile), st)
}

func writeFlashStatusMerge(dataDir string, patch map[string]any) {
	path := filepath.Join(dataDir, fwFlashStatusFile)
	cur := map[string]any{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, &cur)
	}
	for k, v := range patch {
		cur[k] = v
	}
	if cur["busy"] == nil {
		cur["busy"] = true
	}
	_ = writeJSONAtomic(path, cur)
}

// ProcessPendingExportLog handles export_log_req.json (NetPacket cmd 20).
func ProcessPendingExportLog(s Settings) bool {
	path := filepath.Join(s.DataDir, exportLogReqFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var req map[string]any
	if json.Unmarshal(b, &req) != nil {
		_ = os.Remove(path)
		return true
	}
	_ = os.Remove(path)

	id := str(req["id"])
	pw := str(req["password"])
	if pw == "" {
		pw = s.Password
	}
	now := float64(time.Now().UnixNano()) / 1e9
	res := map[string]any{
		"id": id,
		"ts": now,
		"ok": false,
	}

	if !protocol.ProbePort(s.Host, protocol.DefaultPort, 3*time.Second) {
		res["error"] = fmt.Sprintf("netpacket :8889 not reachable on %s", s.Host)
		_ = writeJSONAtomic(filepath.Join(s.DataDir, exportLogResultFile), res)
		return true
	}
	type cred struct{ acc, pw string }
	tryCreds := []cred{{"super", "super"}, {"admin", pw}}
	if pw != "" && pw != "super" {
		tryCreds = append(tryCreds, cred{"super", pw})
	}
	var body []byte
	var resp *protocol.Response
	var expErr error
	okExp := false
	for _, cr := range tryCreds {
		if strings.TrimSpace(cr.pw) == "" {
			continue
		}
		np := protocol.NewClient(s.Host)
		np.Account = cr.acc
		np.Password = cr.pw
		np.Timeout = 60 * time.Second
		body, resp, expErr = np.ExportLog()
		if expErr != nil {
			log.Printf("[miner-poller] export-log %s: %v", cr.acc, expErr)
			continue
		}
		okExp = true
		break
	}
	if !okExp {
		if expErr != nil {
			res["error"] = expErr.Error()
		} else {
			res["error"] = "export-log failed"
		}
		_ = writeJSONAtomic(filepath.Join(s.DataDir, exportLogResultFile), res)
		return true
	}
	dir := filepath.Join(s.DataDir, exportLogDir)
	_ = os.MkdirAll(dir, 0o755)
	name := fmt.Sprintf("miner-export-%s.bin", time.Now().Format("20060102-150405"))
	outPath := filepath.Join(dir, name)
	if err := os.WriteFile(outPath, body, 0o644); err != nil {
		res["error"] = err.Error()
		_ = writeJSONAtomic(filepath.Join(s.DataDir, exportLogResultFile), res)
		return true
	}
	// detect gzip magic
	gz := len(body) >= 2 && body[0] == 0x1f && body[1] == 0x8b
	if gz {
		gzPath := strings.TrimSuffix(outPath, ".bin") + ".bin.gz"
		if err := os.Rename(outPath, gzPath); err == nil {
			outPath = gzPath
			name = filepath.Base(gzPath)
		}
	}
	res["ok"] = true
	if resp != nil {
		res["netpacket_ok"] = resp.OK
	}
	res["path"] = outPath
	res["filename"] = name
	res["bytes"] = len(body)
	res["transport"] = "netpacket"
	_ = writeJSONAtomic(filepath.Join(s.DataDir, exportLogResultFile), res)
	log.Printf("[miner-poller] export-log ok %s (%d B)", name, len(body))
	return true
}

