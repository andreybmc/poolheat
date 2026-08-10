// poolheat-devices-poller — Go replacement for `serve.py --devices-poller`.
//
// File IPC compatible with Python serve:
//   devices_config.json, devices_state.json, mining_work.json,
//   devices_suspend_deadlines.json, devices_poller.pid
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/andreybmc/poolheat/edge/internal/paths"
	"github.com/andreybmc/poolheat/edge/internal/poller"
)

var version = "dev"

func main() {
	showVer := flag.Bool("version", false, "print version and exit")
	data := flag.String("data", "", "POOLHEAT_DATA directory (default env or /opt/var/poolheat)")
	flag.Parse()
	if *showVer {
		fmt.Println(version)
		return
	}
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("")
	dataDir := *data
	if dataDir == "" {
		dataDir = paths.DataDir()
	}
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		log.Fatalf("[devices-poller] data dir: %v", err)
	}
	if err := poller.Run(dataDir); err != nil {
		log.Fatalf("[devices-poller] exit: %v", err)
	}
}
