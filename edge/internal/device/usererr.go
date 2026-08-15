package device

import (
	"strings"
)

// UserFacingError maps technical backend errors to short UI categories.
// Raw dial/EOF/tcp noise must stay in process logs only — never in last_error.
//
// Categories (RU):
//   offline      → устройство офлайн
//   config       → ошибка конфигурации
//   unsupported  → устройство не поддерживается
func UserFacingError(err error, backend string) string {
	if err == nil {
		return "устройство офлайн"
	}
	return userFacingFromText(err.Error(), backend)
}

func userFacingFromText(raw, backend string) string {
	low := strings.ToLower(strings.TrimSpace(raw))
	if backend != "" {
		low = strings.ToLower(backend) + " " + low
	}
	if low == "" {
		return "устройство офлайн"
	}

	// Policy / mining gates — keep short if already human
	if containsAny(low, "майнинг", "while mining", "while suspend", "not allowed", "запрещ") {
		if !containsAny(low, "dial", "tcp", "eof", "route", "timeout", "connect:") {
			s := strings.TrimSpace(raw)
			if len(s) > 120 {
				s = s[:120]
			}
			return s
		}
		return "запрещено"
	}
	if containsAny(low, "preempted by ui", "busy") {
		return "занято, повторите"
	}
	if containsAny(low, "disabled", "отключ") {
		return "устройство отключено"
	}

	// Unsupported
	if containsAny(low,
		"not supported", "unsupported", "not implemented", "не поддержив",
		"unknown backend", "unknown device", "no backend",
	) {
		return "устройство не поддерживается"
	}

	// Offline / connectivity first (before bare "empty" config matches)
	if containsAny(low,
		"offline", "офлайн", "не в сети", "недоступ", "unreachable",
		"no route", "network is unreachable", "connection refused",
		"connection reset", "reset by peer", "broken pipe",
		"i/o timeout", "timeout", "timed out", "deadline",
		"eof", "empty reply", "empty response", "status: empty", "status empty", "closed",
		"dial ", "dial tcp", "connect:", "not responding", "не отвечает",
		"name or service not known", "nodename nor servname",
		"failed to resolve", "no such host", "host is down", "network down",
		"devices-poller", "signal: killed", "status timeout",
		"set failed", "status failed", "no switch state",
		"tinytuya", "tuya dial", "tuya status",
		"404", "502", "503", "504",
		"tcp", "udp", "socket",
	) {
		return "устройство офлайн"
	}

	// Configuration (missing/invalid keys — not connectivity)
	if containsAny(low,
		"not configured", "не настроен", "invalid", "не действитель",
		"auth mismatch", "unauthorized", "login error", "login_device",
		"bad email", "bad password", "local_key", "localkey",
		"devicekey empty", "device_id empty", "deviceid empty",
		"token empty", "token invalid", "wrong key", "decrypt", "klap",
		"handshake", "error_code=-1501", "1003", "missing", "required",
		"credentials", "email/password", "ip empty", "host empty", "config", "конфиг",
		"key empty", "password empty", "email empty",
	) {
		return "ошибка конфигурации"
	}

	// Default: never dump technical strings into UI
	return "устройство офлайн"
}

func containsAny(s string, parts ...string) bool {
	for _, p := range parts {
		if p != "" && strings.Contains(s, p) {
			return true
		}
	}
	return false
}
