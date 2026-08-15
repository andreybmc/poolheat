package miner

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

const (
	managedFile    = "miners_managed.json"
	discoveredFile = "miners_discovered.json"
	pollerCfgFile  = "miner_poller_config.json"
)

// DiscoveryCfg — network scan settings (independent of live poll).
type DiscoveryCfg struct {
	Enabled        bool     `json:"enabled"`
	Ranges         []string `json:"ranges"`
	IntervalSec    int      `json:"interval_sec"`
	ProbeTimeoutMs int      `json:"probe_timeout_ms"`
	Concurrency    int      `json:"concurrency"`
	Ports          []int    `json:"ports"` // probe candidates; vendor-specific used inside
	Passwords      []string `json:"passwords"`
	IgnoreIPs      []string `json:"ignore_ips"`
}

// PollCfg — managed inventory polling (parallel).
type PollCfg struct {
	MaxParallel     int `json:"max_parallel"`
	IntervalSec     int `json:"interval_sec"` // 0 = use global poll_interval from config.json
	PerMinerTimeoutS int `json:"per_miner_timeout_sec"`
}

// PollerConfigFile is miner_poller_config.json.
type PollerConfigFile struct {
	Version   int          `json:"version"`
	Discovery DiscoveryCfg `json:"discovery"`
	Poll      PollCfg      `json:"poll"`
}

func DefaultPollerConfig() PollerConfigFile {
	return PollerConfigFile{
		Version: 1,
		Discovery: DiscoveryCfg{
			Enabled:        false,
			Ranges:         []string{},
			IntervalSec:    600,
			ProbeTimeoutMs: 500,
			Concurrency:    48,
			Ports:          []int{4028, 80, 4433},
			Passwords:      []string{"admin"},
			IgnoreIPs:      []string{},
		},
		Poll: PollCfg{
			MaxParallel:     4,
			IntervalSec:     0,
			PerMinerTimeoutS: 15, // Whatsminer + multi-vendor fleet; 8s caused false offline
		},
	}
}

func LoadPollerConfig(dataDir string) PollerConfigFile {
	out := DefaultPollerConfig()
	path := filepath.Join(dataDir, pollerCfgFile)
	var raw PollerConfigFile
	if err := jsonutil.LoadJSON(path, &raw); err != nil || raw.Version == 0 && len(raw.Discovery.Ranges) == 0 && !raw.Discovery.Enabled {
		// try partial map load
		var m map[string]any
		if err2 := jsonutil.LoadJSON(path, &m); err2 != nil || m == nil {
			return out
		}
		mergePollerConfigMap(&out, m)
		return normalizePollerConfig(out)
	}
	if raw.Version > 0 || raw.Discovery.Enabled || len(raw.Discovery.Ranges) > 0 || raw.Poll.MaxParallel > 0 {
		out = mergePollerConfig(out, raw)
	}
	return normalizePollerConfig(out)
}

func SavePollerConfig(dataDir string, cfg PollerConfigFile) error {
	cfg = normalizePollerConfig(cfg)
	cfg.Version = 1
	return jsonutil.SaveAtomic(filepath.Join(dataDir, pollerCfgFile), cfg)
}

func mergePollerConfig(base, over PollerConfigFile) PollerConfigFile {
	if over.Version > 0 {
		base.Version = over.Version
	}
	base.Discovery.Enabled = over.Discovery.Enabled
	if len(over.Discovery.Ranges) > 0 {
		base.Discovery.Ranges = over.Discovery.Ranges
	}
	if over.Discovery.IntervalSec > 0 {
		base.Discovery.IntervalSec = over.Discovery.IntervalSec
	}
	if over.Discovery.ProbeTimeoutMs > 0 {
		base.Discovery.ProbeTimeoutMs = over.Discovery.ProbeTimeoutMs
	}
	if over.Discovery.Concurrency > 0 {
		base.Discovery.Concurrency = over.Discovery.Concurrency
	}
	if len(over.Discovery.Ports) > 0 {
		base.Discovery.Ports = over.Discovery.Ports
	}
	if len(over.Discovery.Passwords) > 0 {
		base.Discovery.Passwords = over.Discovery.Passwords
	}
	if over.Discovery.IgnoreIPs != nil {
		base.Discovery.IgnoreIPs = over.Discovery.IgnoreIPs
	}
	if over.Poll.MaxParallel > 0 {
		base.Poll.MaxParallel = over.Poll.MaxParallel
	}
	if over.Poll.IntervalSec > 0 {
		base.Poll.IntervalSec = over.Poll.IntervalSec
	}
	if over.Poll.PerMinerTimeoutS > 0 {
		base.Poll.PerMinerTimeoutS = over.Poll.PerMinerTimeoutS
	}
	return base
}

func mergePollerConfigMap(out *PollerConfigFile, m map[string]any) {
	if d, ok := m["discovery"].(map[string]any); ok {
		if v, ok := d["enabled"].(bool); ok {
			out.Discovery.Enabled = v
		}
		if v, ok := d["ranges"].([]any); ok {
			var rs []string
			for _, x := range v {
				if s, ok := x.(string); ok && strings.TrimSpace(s) != "" {
					rs = append(rs, strings.TrimSpace(s))
				}
			}
			out.Discovery.Ranges = rs
		}
		if v, ok := asInt(d["interval_sec"]); ok {
			out.Discovery.IntervalSec = v
		}
		if v, ok := asInt(d["probe_timeout_ms"]); ok {
			out.Discovery.ProbeTimeoutMs = v
		}
		if v, ok := asInt(d["concurrency"]); ok {
			out.Discovery.Concurrency = v
		}
	}
	if p, ok := m["poll"].(map[string]any); ok {
		if v, ok := asInt(p["max_parallel"]); ok {
			out.Poll.MaxParallel = v
		}
	}
}

func normalizePollerConfig(c PollerConfigFile) PollerConfigFile {
	if c.Discovery.IntervalSec < 60 {
		c.Discovery.IntervalSec = 60
	}
	if c.Discovery.IntervalSec > 86400 {
		c.Discovery.IntervalSec = 86400
	}
	if c.Discovery.ProbeTimeoutMs < 100 {
		c.Discovery.ProbeTimeoutMs = 100
	}
	if c.Discovery.ProbeTimeoutMs > 5000 {
		c.Discovery.ProbeTimeoutMs = 5000
	}
	if c.Discovery.Concurrency < 1 {
		c.Discovery.Concurrency = 16
	}
	if c.Discovery.Concurrency > 128 {
		c.Discovery.Concurrency = 128
	}
	if len(c.Discovery.Ports) == 0 {
		c.Discovery.Ports = []int{4028, 80, 4433}
	}
	if c.Poll.MaxParallel < 1 {
		c.Poll.MaxParallel = 1
	}
	if c.Poll.MaxParallel > 32 {
		c.Poll.MaxParallel = 32
	}
	if c.Poll.PerMinerTimeoutS < 3 {
		c.Poll.PerMinerTimeoutS = 8
	}
	return c
}

// ─── Discovered ────────────────────────────────────────────────────────────

// DiscoveredMiner is a network find (not yet managed).
type DiscoveredMiner struct {
	IP        string `json:"ip"`
	Vendor    string `json:"vendor"` // whatsminer | antminer
	Port      int    `json:"port"`
	MinerType string `json:"miner_type,omitempty"`
	MAC       string `json:"mac,omitempty"`
	FW        string `json:"fw_ver,omitempty"`
	Platform  string `json:"platform,omitempty"`
	LastSeen  string `json:"last_seen"`
	FirstSeen string `json:"first_seen,omitempty"`
	RTTMs     int    `json:"rtt_ms,omitempty"`
	Status    string `json:"status"` // online | auth_failed
	Ignored   bool   `json:"ignored,omitempty"`
}

type DiscoveredFile struct {
	Version   int              `json:"version"`
	UpdatedTS string           `json:"updated_ts"`
	ScanMS    int              `json:"scan_ms,omitempty"`
	Probed    int              `json:"probed,omitempty"`
	Found     int              `json:"found,omitempty"`
	Miners    []DiscoveredMiner `json:"miners"`
}

func LoadDiscovered(dataDir string) DiscoveredFile {
	var f DiscoveredFile
	_ = jsonutil.LoadJSON(filepath.Join(dataDir, discoveredFile), &f)
	if f.Miners == nil {
		f.Miners = []DiscoveredMiner{}
	}
	f.Version = 1
	return f
}

func SaveDiscovered(dataDir string, f DiscoveredFile) error {
	f.Version = 1
	if f.UpdatedTS == "" {
		f.UpdatedTS = time.Now().Format("2006-01-02T15:04:05")
	}
	if f.Miners == nil {
		f.Miners = []DiscoveredMiner{}
	}
	return jsonutil.SaveAtomic(filepath.Join(dataDir, discoveredFile), f)
}

// ─── Managed inventory ─────────────────────────────────────────────────────

// flexString accepts JSON string or number (serve/miners.db mirrors id as int).
type flexString string

func (f *flexString) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if len(b) == 0 || string(b) == "null" {
		*f = ""
		return nil
	}
	if b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		*f = flexString(s)
		return nil
	}
	// number / bool → string form
	*f = flexString(strings.Trim(string(b), `"`))
	return nil
}

func (f flexString) String() string { return string(f) }

// ManagedMiner is imported into the system (polling candidate).
type ManagedMiner struct {
	ID         flexString `json:"id"`
	Vendor     string     `json:"vendor"`
	Host       string     `json:"host"`
	Port       int        `json:"port"`
	Password   string     `json:"password,omitempty"`
	Enabled    bool       `json:"enabled"`
	Role       string     `json:"role"` // active | standby
	Alias      string     `json:"alias,omitempty"`
	Name       string     `json:"name,omitempty"`       // display name
	Cell       string     `json:"cell,omitempty"`       // location / rack cell
	Model      string     `json:"model,omitempty"`      // human model label
	ModelCode  string     `json:"model_code,omitempty"` // e.g. M63_VK28
	Serial     string     `json:"serial,omitempty"`     // factory / customer SN
	Inventory  string     `json:"inventory,omitempty"`  // inventory / asset number
	Cooling    string     `json:"cooling,omitempty"`    // air | hydro | immersion
	Algo       string     `json:"algo,omitempty"`       // sha256 | scrypt | eaglesong | …
	PoolID     string     `json:"pool_id,omitempty"`    // heat pool this miner serves
	MinerType  string     `json:"miner_type,omitempty"` // raw type from API
	MAC        string     `json:"mac,omitempty"`
	FW         string     `json:"fw_ver,omitempty"`
	ImportedAt string     `json:"imported_at,omitempty"`
	Source     string     `json:"source,omitempty"` // discovery | manual
	LastOKTS   string     `json:"last_ok_ts,omitempty"`
	LastError  string     `json:"last_error,omitempty"`
}

type ManagedFile struct {
	Version int           `json:"version"`
	Miners  []ManagedMiner `json:"miners"`
}

func LoadManaged(dataDir string) ManagedFile {
	var f ManagedFile
	path := filepath.Join(dataDir, managedFile)
	if err := jsonutil.LoadJSON(path, &f); err != nil {
		// Fallback: map decode so a single bad field cannot wipe inventory
		var raw map[string]any
		if err2 := jsonutil.LoadJSON(path, &raw); err2 == nil {
			f = managedFromMap(raw)
		}
	}
	if f.Miners == nil {
		f.Miners = []ManagedMiner{}
	}
	f.Version = 1
	return f
}

func managedFromMap(raw map[string]any) ManagedFile {
	out := ManagedFile{Version: 1, Miners: nil}
	arr, _ := raw["miners"].([]any)
	for _, x := range arr {
		m, ok := x.(map[string]any)
		if !ok {
			continue
		}
		mm := ManagedMiner{
			Vendor:   str(m["vendor"]),
			Host:     str(m["host"]),
			Password: str(m["password"]),
			Role:     str(m["role"]),
			Alias:    str(m["alias"]),
			Name:     str(m["name"]),
			Model:    str(m["model"]),
		}
		mm.ID = flexString(str(m["id"]))
		if v, ok := asInt(m["port"]); ok {
			mm.Port = v
		}
		if v, ok := m["enabled"].(bool); ok {
			mm.Enabled = v
		} else {
			mm.Enabled = true
		}
		if mm.Host != "" {
			out.Miners = append(out.Miners, mm)
		}
	}
	return out
}

func SaveManaged(dataDir string, f ManagedFile) error {
	f.Version = 1
	if f.Miners == nil {
		f.Miners = []ManagedMiner{}
	}
	// ensure exactly one active if any enabled
	active := 0
	for _, m := range f.Miners {
		if m.Role == "active" {
			active++
		}
	}
	if active == 0 && len(f.Miners) > 0 {
		f.Miners[0].Role = "active"
	}
	if active > 1 {
		seen := false
		for i := range f.Miners {
			if f.Miners[i].Role == "active" {
				if !seen {
					seen = true
				} else {
					f.Miners[i].Role = "standby"
				}
			}
		}
	}
	return jsonutil.SaveAtomic(filepath.Join(dataDir, managedFile), f)
}

// ActiveManaged returns the active managed miner, or nil.
// Only returns a miner with role=="active" (enabled). Does NOT fall back to
// "first enabled" — that silently pointed the live poller at a standby ASIC
// (e.g. iPollo 172.16.100.195) while inventory showed Whatsminer 192.168.1.10
// as active after miners.db migration left miners_managed.json stale.
func ActiveManaged(dataDir string) *ManagedMiner {
	f := LoadManaged(dataDir)
	for i := range f.Miners {
		if strings.EqualFold(strings.TrimSpace(f.Miners[i].Role), "active") && f.Miners[i].Enabled {
			m := f.Miners[i]
			return &m
		}
	}
	return nil
}

// ActiveID returns the managed id string of the active miner.
func (m *ManagedMiner) ActiveID() string {
	if m == nil {
		return ""
	}
	return strings.TrimSpace(m.ID.String())
}

// EnsureManagedFromSettings seeds inventory from single-host config if empty.
func EnsureManagedFromSettings(dataDir string, s Settings) {
	f := LoadManaged(dataDir)
	if len(f.Miners) > 0 {
		return
	}
	host := strings.TrimSpace(s.Host)
	if host == "" {
		return
	}
	port := s.Port
	if port <= 0 {
		port = 4028
	}
	m := ManagedMiner{
		ID:         "m_default",
		Vendor:     "whatsminer",
		Host:       host,
		Port:       port,
		Password:   s.Password,
		Enabled:    true,
		Role:       "active",
		Alias:      "primary",
		ImportedAt: time.Now().Format("2006-01-02T15:04:05"),
		Source:     "manual",
	}
	f.Miners = []ManagedMiner{m}
	_ = SaveManaged(dataDir, f)
}

// ApplyActiveToSettings copies active managed host into Settings for live loop.
func ApplyActiveToSettings(dataDir string, s *Settings) {
	m := ActiveManaged(dataDir)
	if m == nil {
		return
	}
	if m.Host != "" {
		s.Host = m.Host
	}
	if m.Port > 0 {
		s.Port = m.Port
	}
	if strings.TrimSpace(m.Password) != "" {
		s.Password = m.Password
	}
}

func newManagedID() string {
	return fmt.Sprintf("m_%d", time.Now().UnixNano()%1e12)
}

// path helpers for serve
func ManagedPath(dataDir string) string    { return filepath.Join(dataDir, managedFile) }
func DiscoveredPath(dataDir string) string { return filepath.Join(dataDir, discoveredFile) }
func PollerCfgPath(dataDir string) string  { return filepath.Join(dataDir, pollerCfgFile) }

// Exists is a tiny helper for tests/serve.
func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// Marshal helpers used by scan result
func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}
