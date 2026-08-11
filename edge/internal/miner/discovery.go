package miner

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/andreybmc/wm-lib/api"
)

const (
	scanReqFile    = "miner_scan_req.json"
	scanResultFile = "miner_scan_result.json"
	scanStatusFile = "miner_scan_status.json"
)

// ScanRequest is enqueued by serve for manual discovery.
type ScanRequest struct {
	ID     string   `json:"id"`
	TS     float64  `json:"ts"`
	Ranges []string `json:"ranges,omitempty"` // override config ranges if set
}

// ScanResult is written after a scan completes.
type ScanResult struct {
	ID        string `json:"id"`
	OK        bool   `json:"ok"`
	Error     string `json:"error,omitempty"`
	Probed    int    `json:"probed"`
	Found     int    `json:"found"`
	ScanMS    int    `json:"scan_ms"`
	UpdatedTS string `json:"updated_ts"`
	TS        float64 `json:"ts"`
}

// ProcessPendingScan runs one manual scan if miner_scan_req.json exists.
func ProcessPendingScan(s Settings) bool {
	path := filepath.Join(s.DataDir, scanReqFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var req ScanRequest
	if err := json.Unmarshal(b, &req); err != nil {
		log.Printf("[miner-poller] scan-req json: %v", err)
		_ = os.Remove(path)
		return true
	}
	_ = os.Remove(path)
	if strings.TrimSpace(req.ID) == "" {
		req.ID = fmt.Sprintf("scan_%d", time.Now().UnixNano())
	}

	cfg := LoadPollerConfig(s.DataDir)
	ranges := req.Ranges
	if len(ranges) == 0 {
		ranges = cfg.Discovery.Ranges
	}
	writeScanStatus(s.DataDir, map[string]any{
		"running": true, "id": req.ID, "phase": "scanning", "ts": time.Now().Format(time.RFC3339),
	})
	res := runDiscovery(s.DataDir, cfg, ranges, s.Password)
	res.ID = req.ID
	res.TS = float64(time.Now().UnixNano()) / 1e9
	_ = writeJSONAtomic(filepath.Join(s.DataDir, scanResultFile), res)
	writeScanStatus(s.DataDir, map[string]any{
		"running": false, "id": req.ID, "phase": "idle",
		"last_ok": res.OK, "found": res.Found, "probed": res.Probed,
		"ts": time.Now().Format(time.RFC3339),
	})
	if res.OK {
		log.Printf("[miner-poller] scan ok id=%s probed=%d found=%d ms=%d", req.ID, res.Probed, res.Found, res.ScanMS)
	} else {
		log.Printf("[miner-poller] scan fail id=%s: %s", req.ID, res.Error)
	}
	return true
}

// MaybeAutoDiscovery runs scheduled discovery when enabled and interval elapsed.
func MaybeAutoDiscovery(s Settings, last *time.Time) {
	cfg := LoadPollerConfig(s.DataDir)
	if !cfg.Discovery.Enabled || len(cfg.Discovery.Ranges) == 0 {
		return
	}
	iv := time.Duration(cfg.Discovery.IntervalSec) * time.Second
	if iv < time.Minute {
		iv = time.Minute
	}
	if last != nil && !last.IsZero() && time.Since(*last) < iv {
		return
	}
	log.Printf("[miner-poller] auto-discovery start ranges=%v", cfg.Discovery.Ranges)
	res := runDiscovery(s.DataDir, cfg, cfg.Discovery.Ranges, s.Password)
	if last != nil {
		*last = time.Now()
	}
	log.Printf("[miner-poller] auto-discovery done probed=%d found=%d ms=%d err=%v",
		res.Probed, res.Found, res.ScanMS, res.Error)
}

func writeScanStatus(dataDir string, v map[string]any) {
	_ = writeJSONAtomic(filepath.Join(dataDir, scanStatusFile), v)
}

func runDiscovery(dataDir string, cfg PollerConfigFile, ranges []string, defaultPW string) ScanResult {
	t0 := time.Now()
	res := ScanResult{OK: true}
	if len(ranges) == 0 {
		res.OK = false
		res.Error = "no scan ranges configured"
		res.ScanMS = int(time.Since(t0).Milliseconds())
		return res
	}

	hosts, err := expandRanges(ranges)
	if err != nil {
		res.OK = false
		res.Error = err.Error()
		res.ScanMS = int(time.Since(t0).Milliseconds())
		return res
	}
	ignore := map[string]bool{}
	for _, ip := range cfg.Discovery.IgnoreIPs {
		ignore[strings.TrimSpace(ip)] = true
	}
	var filtered []string
	for _, h := range hosts {
		if ignore[h] {
			continue
		}
		filtered = append(filtered, h)
	}
	hosts = filtered
	res.Probed = len(hosts)

	timeout := time.Duration(cfg.Discovery.ProbeTimeoutMs) * time.Millisecond
	if timeout < 100*time.Millisecond {
		timeout = 500 * time.Millisecond
	}
	conc := cfg.Discovery.Concurrency
	if conc < 1 {
		conc = 32
	}

	passwords := cfg.Discovery.Passwords
	if len(passwords) == 0 {
		passwords = []string{"admin"}
	}
	if defaultPW != "" {
		// prefer configured password first
		passwords = uniqueStrings(append([]string{defaultPW}, passwords...))
	}

	type hit struct {
		m DiscoveredMiner
	}
	var (
		mu   sync.Mutex
		hits []DiscoveredMiner
		wg   sync.WaitGroup
		sem  = make(chan struct{}, conc)
	)
	for _, ip := range hosts {
		ip := ip
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			if m, ok := probeHost(ip, timeout, passwords); ok {
				mu.Lock()
				hits = append(hits, m)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	// merge with previous discovered (preserve first_seen, ignored)
	prev := LoadDiscovered(dataDir)
	prevByIP := map[string]DiscoveredMiner{}
	for _, p := range prev.Miners {
		prevByIP[p.IP] = p
	}
	now := time.Now().Format("2006-01-02T15:04:05")
	// start from previous ignored entries so they don't reappear as fresh
	out := make([]DiscoveredMiner, 0, len(hits)+len(prev.Miners))
	seen := map[string]bool{}
	for _, h := range hits {
		if p, ok := prevByIP[h.IP]; ok {
			if p.Ignored {
				h.Ignored = true
			}
			if p.FirstSeen != "" {
				h.FirstSeen = p.FirstSeen
			} else {
				h.FirstSeen = now
			}
		} else {
			h.FirstSeen = now
		}
		h.LastSeen = now
		out = append(out, h)
		seen[h.IP] = true
	}
	// keep recently seen ignored that weren't in this scan batch? optional drop
	for _, p := range prev.Miners {
		if seen[p.IP] {
			continue
		}
		if p.Ignored {
			out = append(out, p)
		}
	}
	// sort by IP
	sortDiscovered(out)

	f := DiscoveredFile{
		Version:   1,
		UpdatedTS: now,
		ScanMS:    int(time.Since(t0).Milliseconds()),
		Probed:    res.Probed,
		Found:     len(hits),
		Miners:    out,
	}
	if err := SaveDiscovered(dataDir, f); err != nil {
		res.OK = false
		res.Error = err.Error()
	}
	res.Found = len(hits)
	res.ScanMS = f.ScanMS
	res.UpdatedTS = now
	return res
}

func uniqueStrings(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		s = strings.TrimSpace(s)
		if s == "" || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

func sortDiscovered(ms []DiscoveredMiner) {
	// simple insertion by IP numeric
	for i := 1; i < len(ms); i++ {
		j := i
		for j > 0 && ipLess(ms[j].IP, ms[j-1].IP) {
			ms[j], ms[j-1] = ms[j-1], ms[j]
			j--
		}
	}
}

func ipLess(a, b string) bool {
	aa := net.ParseIP(a)
	bb := net.ParseIP(b)
	if aa == nil || bb == nil {
		return a < b
	}
	return string(aa.To16()) < string(bb.To16())
}

// probeHost tries Whatsminer then Antminer fingerprints.
func probeHost(ip string, timeout time.Duration, passwords []string) (DiscoveredMiner, bool) {
	t0 := time.Now()
	// 1) Whatsminer classic API :4028 (wm-lib V2)
	if m, ok := probeWhatsminer(ip, 4028, timeout, passwords); ok {
		m.RTTMs = int(time.Since(t0).Milliseconds())
		return m, true
	}
	// 2) Antminer HTTP :80
	if m, ok := probeAntminerHTTP(ip, 80, timeout); ok {
		m.RTTMs = int(time.Since(t0).Milliseconds())
		return m, true
	}
	return DiscoveredMiner{}, false
}

func probeWhatsminer(ip string, port int, timeout time.Duration, passwords []string) (DiscoveredMiner, bool) {
	// Quick TCP reachability first (fail fast on closed ports)
	addr := net.JoinHostPort(ip, fmt.Sprintf("%d", port))
	c0, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return DiscoveredMiner{}, false
	}
	_ = c0.Close()

	// Use wm-lib V2 (encrypted JSON) — raw {"cmd":...} is ignored on modern FW.
	// Only meaningful on classic ASIC API port (4028); 4433 is V3 binary framing.
	if port != 4028 && port != 4029 {
		return DiscoveredMiner{}, false
	}
	to := timeout
	if to < 800*time.Millisecond {
		to = 800 * time.Millisecond
	}
	if to > 3*time.Second {
		to = 3 * time.Second
	}
	pw := "admin"
	if len(passwords) > 0 && strings.TrimSpace(passwords[0]) != "" {
		pw = passwords[0]
	}
	cl := api.NewV2(ip)
	cl.Port = port
	cl.Password = pw
	cl.Timeout = to
	resp, err := cl.Read("get_version", nil)
	if err != nil || resp == nil {
		return DiscoveredMiner{}, false
	}
	// STATUS S/E still means Whatsminer stack answered
	st, _ := resp["STATUS"].(string)
	if st == "" && resp["Msg"] == nil && resp["Code"] == nil {
		return DiscoveredMiner{}, false
	}
	m := DiscoveredMiner{
		IP:     ip,
		Vendor: "whatsminer",
		Port:   port,
		Status: "online",
	}
	if msg, ok := resp["Msg"].(map[string]any); ok {
		m.MinerType = strAny(msg["miner_type"])
		m.FW = strAny(msg["fw_ver"])
		m.Platform = strAny(msg["platform"])
	}
	// optional MAC
	if info, err2 := cl.Read("get_miner_info", nil); err2 == nil && info != nil {
		if msg, ok := info["Msg"].(map[string]any); ok {
			m.MAC = strAny(msg["mac"])
		}
	}
	return m, true
}

func probeAntminerHTTP(ip string, port int, timeout time.Duration) (DiscoveredMiner, bool) {
	// Quick TCP first
	addr := net.JoinHostPort(ip, fmt.Sprintf("%d", port))
	c, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return DiscoveredMiner{}, false
	}
	_ = c.Close()

	client := &http.Client{Timeout: timeout}
	url := fmt.Sprintf("http://%s/", ip)
	if port != 80 {
		url = fmt.Sprintf("http://%s:%d/", ip, port)
	}
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return DiscoveredMiner{}, false
	}
	req.Header.Set("User-Agent", "poolheat-discovery/1.0")
	resp, err := client.Do(req)
	if err != nil {
		return DiscoveredMiner{}, false
	}
	defer resp.Body.Close()
	// read limited body
	lr := io.LimitReader(resp.Body, 64*1024)
	body, _ := io.ReadAll(lr)
	low := strings.ToLower(string(body))
	title := ""
	if i := strings.Index(low, "<title>"); i >= 0 {
		j := strings.Index(low[i:], "</title>")
		if j > 0 {
			title = strings.TrimSpace(string(body[i+7 : i+j]))
		}
	}
	isAnt := strings.Contains(low, "antminer") ||
		strings.Contains(low, "bitmain") ||
		strings.Contains(strings.ToLower(title), "antminer") ||
		strings.Contains(low, "cgi-bin/luci") && strings.Contains(low, "miner") // some images
	// stronger: common Bitmain login page
	if !isAnt && (strings.Contains(low, "miner type") || strings.Contains(low, "cloud service")) {
		// weak — don't accept without antminer/bitmain
	}
	if !isAnt {
		return DiscoveredMiner{}, false
	}
	mt := title
	if mt == "" {
		mt = "Antminer"
	}
	return DiscoveredMiner{
		IP:        ip,
		Vendor:    "antminer",
		Port:      port,
		MinerType:  mt,
		Status:    "online",
	}, true
}

func strAny(v any) string {
	if v == nil {
		return ""
	}
	switch t := v.(type) {
	case string:
		return strings.TrimSpace(t)
	default:
		return strings.TrimSpace(fmt.Sprint(t))
	}
}

// expandRanges: CIDR, start-end, single IP (same as fleet.py).
func expandRanges(ranges []string) ([]string, error) {
	seen := map[string]bool{}
	var out []string
	for _, r := range ranges {
		r = strings.TrimSpace(r)
		if r == "" {
			continue
		}
		ips, err := expandOneRange(r)
		if err != nil {
			return nil, fmt.Errorf("range %q: %w", r, err)
		}
		for _, ip := range ips {
			if seen[ip] {
				continue
			}
			seen[ip] = true
			out = append(out, ip)
		}
	}
	// safety cap — Keenetic / large ranges
	const maxHosts = 4096
	if len(out) > maxHosts {
		return nil, fmt.Errorf("too many hosts (%d > %d) — shrink ranges", len(out), maxHosts)
	}
	return out, nil
}

func expandOneRange(s string) ([]string, error) {
	if strings.Contains(s, "/") {
		_, network, err := net.ParseCIDR(s)
		if err != nil {
			return nil, err
		}
		var ips []string
		for ip := network.IP.Mask(network.Mask); network.Contains(ip); incIP(ip) {
			// skip network & broadcast for IPv4 /24+ style
			ips = append(ips, ip.String())
			if len(ips) > 4096 {
				break
			}
		}
		// drop network address and often broadcast
		if len(ips) >= 2 {
			// first is network; last may be broadcast
			ips = ips[1:]
			if len(ips) >= 1 {
				last := net.ParseIP(ips[len(ips)-1])
				if last != nil && last.To4() != nil {
					// remove broadcast if classic
					ips = ips[:len(ips)-1]
				}
			}
		}
		return ips, nil
	}
	if strings.Contains(s, "-") {
		parts := strings.SplitN(s, "-", 2)
		start := net.ParseIP(strings.TrimSpace(parts[0]))
		end := net.ParseIP(strings.TrimSpace(parts[1]))
		if start == nil || end == nil {
			return nil, fmt.Errorf("invalid IP range")
		}
		s4, e4 := start.To4(), end.To4()
		if s4 == nil || e4 == nil {
			return nil, fmt.Errorf("only IPv4 ranges supported")
		}
		si := ipToUint(s4)
		ei := ipToUint(e4)
		if ei < si {
			si, ei = ei, si
		}
		if ei-si > 4096 {
			return nil, fmt.Errorf("range too large")
		}
		var ips []string
		for i := si; i <= ei; i++ {
			ips = append(ips, uintToIP(i).String())
		}
		return ips, nil
	}
	ip := net.ParseIP(s)
	if ip == nil {
		return nil, fmt.Errorf("invalid IP")
	}
	return []string{ip.String()}, nil
}

func incIP(ip net.IP) {
	for j := len(ip) - 1; j >= 0; j-- {
		ip[j]++
		if ip[j] > 0 {
			break
		}
	}
}

func ipToUint(ip net.IP) uint32 {
	ip = ip.To4()
	return uint32(ip[0])<<24 | uint32(ip[1])<<16 | uint32(ip[2])<<8 | uint32(ip[3])
}

func uintToIP(n uint32) net.IP {
	return net.IPv4(byte(n>>24), byte(n>>16), byte(n>>8), byte(n))
}

