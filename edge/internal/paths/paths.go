package paths

import (
	"os"
	"path/filepath"
)

// DataDir resolves POOLHEAT_DATA (default /opt/var/poolheat).
func DataDir() string {
	if v := os.Getenv("POOLHEAT_DATA"); v != "" {
		return v
	}
	return "/opt/var/poolheat"
}

func DevicesConfig(data string) string  { return filepath.Join(data, "devices_config.json") }
func DevicesState(data string) string   { return filepath.Join(data, "devices_state.json") }
func MiningWork(data string) string     { return filepath.Join(data, "mining_work.json") }
func Deadlines(data string) string      { return filepath.Join(data, "devices_suspend_deadlines.json") }
func PidFile(data string) string        { return filepath.Join(data, "devices_poller.pid") }
func PolicyEvents(data string) string   { return filepath.Join(data, "policy_events.json") }
