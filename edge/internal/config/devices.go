package config

import (
	"fmt"
	"strings"

	"github.com/andreybmc/poolheat/edge/internal/jsonutil"
)

// PollerCfg matches Python DEFAULT_DEVICES_POLLER.
type PollerCfg struct {
	Enabled             bool `json:"enabled"`
	IntervalSec         int  `json:"interval_sec"`
	SetTimeoutSec       int  `json:"set_timeout_sec"`
	ErrorBackoffSec     int  `json:"error_backoff_sec"`
	MiningWorkMaxAgeSec int  `json:"mining_work_max_age_sec"`
}

func DefaultPoller() PollerCfg {
	return PollerCfg{
		Enabled:             true,
		IntervalSec:         5,
		SetTimeoutSec:       15,
		ErrorBackoffSec:     20,
		MiningWorkMaxAgeSec: 300,
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
	// JSON may omit enabled → treat missing as true via pointer? Our struct bool
	// defaults false on omit. Python defaults true. Re-read with map if needed.
	return out
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

	// ewelink
	EwelinkPort int `json:"ewelink_port"`

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

	// xiaomi
	XiaomiToken string `json:"xiaomi_token"`
	XiaomiModel string `json:"xiaomi_model"`
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
		WebhookMethod: "GET",
		ShellyGen:     "auto",
		TuyaEcosystem: "smartlife",
		TuyaCountry:   "7",
		TuyaRegion:    "eu",
		TuyaVersion:   3.4,
		TuyaSwitchDPS: 1,
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
