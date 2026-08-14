package config

import (
	"fmt"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

// PollerCfg matches Python DEFAULT_DEVICES_POLLER.
type PollerCfg struct {
	Enabled             bool `json:"enabled"`
	IntervalSec         int  `json:"interval_sec"`
	SetTimeoutSec       int  `json:"set_timeout_sec"`
	ErrorBackoffSec     int  `json:"error_backoff_sec"`
	MiningWorkMaxAgeSec int  `json:"mining_work_max_age_sec"`
	// HoldIntervalSec: background probe period when enforce_desired devices exist.
	// Loop uses min(IntervalSec, HoldIntervalSec) so hold is not slower than status.
	HoldIntervalSec int `json:"hold_interval_sec"`
	// EnforceCooldownSec: min gap between restore attempts for the same device.
	EnforceCooldownSec int `json:"enforce_cooldown_sec"`
}

func DefaultPoller() PollerCfg {
	return PollerCfg{
		Enabled:             true,
		IntervalSec:         5,
		SetTimeoutSec:       15,
		ErrorBackoffSec:     20,
		MiningWorkMaxAgeSec: 300,
		HoldIntervalSec:     5,
		EnforceCooldownSec:  3,
	}
}

func NormalizePoller(p *PollerCfg) PollerCfg {
	out := DefaultPoller()
	if p == nil {
		return out
	}
	out.Enabled = p.Enabled
	if p.IntervalSec > 0 {
		out.IntervalSec = clamp(p.IntervalSec, 3, 120)
	}
	if p.SetTimeoutSec > 0 {
		out.SetTimeoutSec = clamp(p.SetTimeoutSec, 3, 60)
	}
	if p.ErrorBackoffSec > 0 {
		out.ErrorBackoffSec = clamp(p.ErrorBackoffSec, 5, 600)
	}
	if p.MiningWorkMaxAgeSec > 0 {
		out.MiningWorkMaxAgeSec = clamp(p.MiningWorkMaxAgeSec, 30, 1800)
	}
	if p.HoldIntervalSec > 0 {
		out.HoldIntervalSec = clamp(p.HoldIntervalSec, 3, 120)
	}
	if p.EnforceCooldownSec >= 0 {
		out.EnforceCooldownSec = clamp(p.EnforceCooldownSec, 0, 60)
	}
	return out
}

// StatusTimeout is the per-device status probe budget.
// Floor 8s: set_timeout_sec=3 (common in UI) is too short for Tuya/Tapo LAN.
func (p PollerCfg) StatusTimeout() time.Duration {
	sec := p.SetTimeoutSec
	if sec < 8 {
		sec = 8
	}
	if sec > 60 {
		sec = 60
	}
	return time.Duration(sec) * time.Second
}

// SetTimeout is the per-device set/enforce budget (needs re-read after write).
func (p PollerCfg) SetTimeout() time.Duration {
	sec := p.SetTimeoutSec
	if sec < 15 {
		sec = 15
	}
	if sec > 60 {
		sec = 60
	}
	return time.Duration(sec) * time.Second
}

// LoopInterval is how often the background poll+hold tick runs.
func (p PollerCfg) LoopInterval() int {
	iv := p.IntervalSec
	if iv < 3 {
		iv = 5
	}
	if p.HoldIntervalSec > 0 && p.HoldIntervalSec < iv {
		iv = p.HoldIntervalSec
	}
	if iv < 3 {
		iv = 3
	}
	if iv > 120 {
		iv = 120
	}
	return iv
}

// DeviceCfg is settings-only (from devices_config.json).
type DeviceCfg struct {
	ID        string `json:"id"`
	Alias     string `json:"alias"`
	Name      string `json:"name"`
	NameEN    string `json:"name_en"`
	NameRU    string `json:"name_ru"`
	Icon      string `json:"icon"`
	Enabled   *bool  `json:"enabled"`
	Backend   string `json:"backend"`
	IP        string `json:"ip"`
	Email     string `json:"email"`
	Password  string `json:"password"`
	DeviceID  string `json:"device_id"`
	Inverted  bool   `json:"inverted"`

	// policy
	AutoOnMining           bool `json:"auto_on_mining"`
	AutoOffSuspend         bool `json:"auto_off_suspend"`
	AutoOffSuspendDelaySec *int `json:"auto_off_suspend_delay_sec"`
	EnforceDesired         bool `json:"enforce_desired"`
	AllowOffWhileMining    bool `json:"allow_off_while_mining"`
	AllowOnWhileSuspend    bool `json:"allow_on_while_suspend"`

	// ewelink (LAN DIY or CoolKit AES with devicekey)
	EwelinkPort      int    `json:"ewelink_port"`
	EwelinkMode      string `json:"ewelink_mode"`      // auto | diy | lan
	EwelinkDeviceKey string `json:"ewelink_devicekey"` // cloud devicekey (LAN encrypt)
	EwelinkAPIKey    string `json:"ewelink_apikey"`    // user selfApikey
	EwelinkOutlet    int    `json:"ewelink_outlet"`

	// webhook
	WebhookOnURL   string `json:"webhook_on_url"`
	WebhookOffURL  string `json:"webhook_off_url"`
	WebhookMethod  string `json:"webhook_method"`
	WebhookBodyOn  string `json:"webhook_body_on"`
	WebhookBodyOff string `json:"webhook_body_off"`
	WebhookHeaders string `json:"webhook_headers"`

	// shelly
	ShellyChannel int    `json:"shelly_channel"`
	ShellyGen     string `json:"shelly_gen"`

	// homeassistant
	HAURL      string `json:"ha_url"`
	HAToken    string `json:"ha_token"`
	HAEntityID string `json:"ha_entity_id"`

	// tuya
	TuyaEcosystem string  `json:"tuya_ecosystem"`
	TuyaCountry   string  `json:"tuya_country"`
	TuyaRegion    string  `json:"tuya_region"`
	TuyaLocalKey  string  `json:"tuya_local_key"`
	TuyaVersion   float64 `json:"tuya_version"`
	TuyaSwitchDPS int     `json:"tuya_switch_dps"`
	// Lights / dimmers (Smart Life): switch often DPS 20, bright 22, mode 21
	TuyaBrightDPS int `json:"tuya_bright_dps"` // 0 = default 22 for light/dimmer
	TuyaModeDPS   int `json:"tuya_mode_dps"`   // 0 = default 21 for light/dimmer
	// device_kind: switch | light | dimmer (hint for UI + DPS defaults)
	DeviceKind string `json:"device_kind"`

	// One-shot set overrides (not persisted; filled from device_req)
	SetBrightness *int    `json:"-"` // 0–100 percent
	SetMode       *string `json:"-"` // white|colour|scene|music

	// xiaomi
	XiaomiToken string `json:"xiaomi_token"`
	XiaomiModel string `json:"xiaomi_model"`
}

// IsLight returns true for light/dimmer kinds (or empty kind with light DPS hints).
func (d DeviceCfg) IsLight() bool {
	k := strings.ToLower(strings.TrimSpace(d.DeviceKind))
	if k == "light" || k == "dimmer" || k == "lamp" || k == "bulb" {
		return true
	}
	// auto: switch dps 20 is almost always a light
	if d.TuyaSwitchDPS == 20 {
		return true
	}
	return false
}

// BrightDPS returns brightness DPS (default 22 for lights).
func (d DeviceCfg) BrightDPS() int {
	if d.TuyaBrightDPS > 0 {
		return d.TuyaBrightDPS
	}
	if d.IsLight() {
		return 22
	}
	return 0
}

// ModeDPS returns work-mode DPS (default 21 for lights).
func (d DeviceCfg) ModeDPS() int {
	if d.TuyaModeDPS > 0 {
		return d.TuyaModeDPS
	}
	if d.IsLight() {
		return 21
	}
	return 0
}

func (d DeviceCfg) IsEnabled() bool {
	if d.Enabled == nil {
		return true
	}
	return *d.Enabled
}

func (d DeviceCfg) BackendNorm() string {
	be := strings.ToLower(strings.TrimSpace(d.Backend))
	if be == "" {
		return "tapo"
	}
	switch be {
	case "smartlife", "smart_life":
		return "tuya"
	case "mi", "mihome", "mi_home", "miio", "mijia":
		return "xiaomi"
	case "sonoff", "sonoff_diy":
		return "ewelink"
	case "ha":
		return "homeassistant"
	}
	return be
}

func (d DeviceCfg) OffDelaySec() int {
	if d.AutoOffSuspendDelaySec == nil {
		return 60
	}
	return clamp(*d.AutoOffSuspendDelaySec, 0, 3600)
}

func (d DeviceCfg) Label() string {
	if d.Alias != "" {
		return d.Alias
	}
	if d.Name != "" {
		return d.Name
	}
	return d.ID
}

// File is the on-disk devices_config.json shape.
type File struct {
	Version int         `json:"version"`
	Poller  PollerCfg   `json:"poller"`
	Devices []DeviceCfg `json:"devices"`
}

// Load reads devices_config.json.
func Load(path string) (File, error) {
	var raw map[string]any
	if err := jsonutil.LoadJSON(path, &raw); err != nil {
		return File{}, err
	}
	// enabled default true: parse poller carefully
	out := File{Version: 1, Poller: DefaultPoller(), Devices: nil}
	if raw == nil {
		return out, nil
	}
	if p, ok := raw["poller"].(map[string]any); ok {
		out.Poller = parsePoller(p)
	}
	devs, _ := raw["devices"].([]any)
	for _, item := range devs {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		d, err := parseDevice(m)
		if err != nil || d.ID == "" {
			continue
		}
		out.Devices = append(out.Devices, d)
	}
	return out, nil
}

func parsePoller(m map[string]any) PollerCfg {
	p := DefaultPoller()
	if v, ok := m["enabled"]; ok {
		p.Enabled = asBool(v, true)
	}
	if v, ok := asInt(m["interval_sec"]); ok {
		p.IntervalSec = clamp(v, 3, 120)
	}
	if v, ok := asInt(m["set_timeout_sec"]); ok {
		p.SetTimeoutSec = clamp(v, 3, 60)
	}
	if v, ok := asInt(m["error_backoff_sec"]); ok {
		p.ErrorBackoffSec = clamp(v, 5, 600)
	}
	if v, ok := asInt(m["mining_work_max_age_sec"]); ok {
		p.MiningWorkMaxAgeSec = clamp(v, 30, 1800)
	}
	if v, ok := asInt(m["hold_interval_sec"]); ok {
		p.HoldIntervalSec = clamp(v, 3, 120)
	}
	if v, ok := asInt(m["enforce_cooldown_sec"]); ok {
		p.EnforceCooldownSec = clamp(v, 0, 60)
	}
	return p
}

func parseDevice(m map[string]any) (DeviceCfg, error) {
	d := DeviceCfg{
		ID:            str(m["id"]),
		Alias:         str(m["alias"]),
		Name:          str(m["name"]),
		NameEN:        str(m["name_en"]),
		NameRU:        str(m["name_ru"]),
		Icon:          str(m["icon"]),
		Backend:       str(m["backend"]),
		IP:            str(m["ip"]),
		Email:         str(m["email"]),
		Password:      str(m["password"]),
		DeviceID:      str(m["device_id"]),
		Inverted:      asBool(m["inverted"], false),
		AutoOnMining:  asBool(m["auto_on_mining"], false),
		AutoOffSuspend: asBool(m["auto_off_suspend"], false),
		EnforceDesired: asBool(m["enforce_desired"], false),
		AllowOffWhileMining: asBool(m["allow_off_while_mining"], false),
		AllowOnWhileSuspend: asBool(m["allow_on_while_suspend"], false),
		EwelinkPort:   8081,
		EwelinkMode:   "auto",
		WebhookMethod: "GET",
		ShellyGen:     "auto",
		TuyaEcosystem: "smartlife",
		TuyaCountry:   "7",
		TuyaRegion:    "eu",
		TuyaVersion:   3.4,
		TuyaSwitchDPS: 1,
		DeviceKind:    str(m["device_kind"]),
	}
	if v, ok := asInt(m["tuya_bright_dps"]); ok {
		d.TuyaBrightDPS = clamp(v, 0, 255)
	}
	if v, ok := asInt(m["tuya_mode_dps"]); ok {
		d.TuyaModeDPS = clamp(v, 0, 255)
	}
	if v, ok := m["enabled"]; ok {
		b := asBool(v, true)
		d.Enabled = &b
	}
	if v, ok := asInt(m["auto_off_suspend_delay_sec"]); ok {
		d.AutoOffSuspendDelaySec = &v
	}
	if v, ok := asInt(m["ewelink_port"]); ok {
		d.EwelinkPort = clamp(v, 1, 65535)
	}
	em := strings.ToLower(str(m["ewelink_mode"]))
	if em == "diy" || em == "lan" || em == "auto" {
		d.EwelinkMode = em
	}
	d.EwelinkDeviceKey = str(m["ewelink_devicekey"])
	if d.EwelinkDeviceKey == "" {
		d.EwelinkDeviceKey = str(m["devicekey"])
	}
	d.EwelinkAPIKey = str(m["ewelink_apikey"])
	if d.EwelinkAPIKey == "" {
		d.EwelinkAPIKey = str(m["apikey"])
	}
	if v, ok := asInt(m["ewelink_outlet"]); ok {
		d.EwelinkOutlet = clamp(v, 0, 7)
	}
	d.WebhookOnURL = str(m["webhook_on_url"])
	d.WebhookOffURL = str(m["webhook_off_url"])
	if s := strings.ToUpper(str(m["webhook_method"])); s == "POST" {
		d.WebhookMethod = "POST"
	}
	d.WebhookBodyOn = str(m["webhook_body_on"])
	d.WebhookBodyOff = str(m["webhook_body_off"])
	d.WebhookHeaders = str(m["webhook_headers"])
	if v, ok := asInt(m["shelly_channel"]); ok {
		d.ShellyChannel = clamp(v, 0, 3)
	}
	d.ShellyGen = str(m["shelly_gen"])
	if d.ShellyGen == "" {
		d.ShellyGen = "auto"
	}
	d.HAURL = str(m["ha_url"])
	d.HAToken = str(m["ha_token"])
	d.HAEntityID = str(m["ha_entity_id"])
	d.TuyaEcosystem = str(m["tuya_ecosystem"])
	if d.TuyaEcosystem == "" {
		d.TuyaEcosystem = "smartlife"
	}
	d.TuyaCountry = str(m["tuya_country"])
	if d.TuyaCountry == "" {
		d.TuyaCountry = "7"
	}
	d.TuyaRegion = str(m["tuya_region"])
	if d.TuyaRegion == "" {
		d.TuyaRegion = "eu"
	}
	d.TuyaLocalKey = str(m["tuya_local_key"])
	if d.TuyaLocalKey == "" {
		d.TuyaLocalKey = str(m["local_key"])
	}
	if f, ok := asFloat(m["tuya_version"]); ok {
		d.TuyaVersion = f
	}
	if v, ok := asInt(m["tuya_switch_dps"]); ok {
		d.TuyaSwitchDPS = clamp(v, 1, 255)
	}
	d.XiaomiToken = str(m["xiaomi_token"])
	if d.XiaomiToken == "" {
		d.XiaomiToken = str(m["miio_token"])
	}
	d.XiaomiModel = str(m["xiaomi_model"])
	if d.ID == "" {
		return d, fmt.Errorf("missing id")
	}
	return d, nil
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func str(v any) string {
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

func asBool(v any, def bool) bool {
	if v == nil {
		return def
	}
	switch t := v.(type) {
	case bool:
		return t
	case float64:
		return t != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(t))
		switch s {
		case "1", "true", "yes", "on", "y":
			return true
		case "0", "false", "no", "off", "n", "":
			return false
		}
	}
	return def
}

func asInt(v any) (int, bool) {
	if v == nil {
		return 0, false
	}
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case string:
		var n int
		_, err := fmt.Sscanf(strings.TrimSpace(t), "%d", &n)
		return n, err == nil
	}
	return 0, false
}

func asFloat(v any) (float64, bool) {
	if v == nil {
		return 0, false
	}
	switch t := v.(type) {
	case float64:
		return t, true
	case int:
		return float64(t), true
	case string:
		var f float64
		_, err := fmt.Sscanf(strings.TrimSpace(t), "%f", &f)
		return f, err == nil
	}
	return 0, false
}
