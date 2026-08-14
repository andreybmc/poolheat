package backend

import (
	"context"
	"fmt"
	"strings"

	"github.com/andreybmc/poolheat/edge/internal/config"
)

// Result of a status/set call.
type Result struct {
	On       *bool          // physical on
	Backend  string
	Power    map[string]any
	Skipped  bool
	Reason   string
	Extra    map[string]any
}

// Control runs status (on==nil) or set physical state.
func Control(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	be := cfg.BackendNorm()
	switch be {
	case "tapo":
		return controlTapo(ctx, on, cfg)
	case "tuya":
		return controlTuya(ctx, on, cfg)
	case "shelly":
		return controlShelly(ctx, on, cfg)
	case "ewelink":
		return controlEwelink(ctx, on, cfg)
	case "webhook":
		return controlWebhook(ctx, on, cfg)
	case "homeassistant":
		return controlHA(ctx, on, cfg)
	case "xiaomi":
		return controlXiaomi(ctx, on, cfg)
	default:
		return Result{}, fmt.Errorf("unknown backend: %s", be)
	}
}

// Ready mirrors Python _device_ready.
func Ready(cfg config.DeviceCfg) bool {
	be := cfg.BackendNorm()
	ip := strings.TrimSpace(cfg.IP)
	switch be {
	case "tapo", "shelly":
		return ip != ""
	case "ewelink":
		if ip == "" || strings.TrimSpace(cfg.DeviceID) == "" {
			return false
		}
		mode := strings.ToLower(strings.TrimSpace(cfg.EwelinkMode))
		if mode == "lan" {
			return strings.TrimSpace(cfg.EwelinkDeviceKey) != ""
		}
		return true
	case "webhook":
		return strings.TrimSpace(cfg.WebhookOnURL) != "" || strings.TrimSpace(cfg.WebhookOffURL) != ""
	case "homeassistant":
		return strings.TrimSpace(cfg.HAURL) != "" && strings.TrimSpace(cfg.HAEntityID) != ""
	case "tuya":
		return ip != "" && strings.TrimSpace(cfg.DeviceID) != "" &&
			(strings.TrimSpace(cfg.TuyaLocalKey) != "" ||
				(strings.TrimSpace(cfg.Email) != "" && cfg.Password != ""))
	case "xiaomi":
		return ip != "" && strings.TrimSpace(cfg.XiaomiToken) != ""
	}
	return false
}

func LogicalToPhysical(logical bool, inverted bool) bool {
	if inverted {
		return !logical
	}
	return logical
}

func PhysicalToLogical(physical bool, inverted bool) bool {
	if inverted {
		return !physical
	}
	return physical
}

func DriverLabel(be string) string {
	cfg := config.DeviceCfg{Backend: be}
	switch cfg.BackendNorm() {
	case "tapo":
		return "Tapo"
	case "tuya":
		return "Smart Life"
	case "shelly":
		return "Shelly"
	case "ewelink":
		return "eWeLink"
	case "webhook":
		return "Webhook"
	case "homeassistant":
		return "Home Assistant"
	case "xiaomi":
		return "Xiaomi"
	default:
		return be
	}
}
