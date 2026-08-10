// poolheat-miner-poller — Go replacement for `serve.py --miner-poller`.
//
// Polls Whatsminer (via wm-lib public API), writes:
//   live_cache.json · mining_work.json · chipmap_cache.json
//   (+ history when enabled)
//
// serve.py must not talk to the ASIC — only read these JSON files / enqueue writes.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/andreybmc/poolheat/edge/internal/miner"
)

var version = "dev"

func main() {
	showVer := flag.Bool("version", false, "print version")
	data := flag.String("data", "", "POOLHEAT_DATA")
	host := flag.String("host", "", "miner host override")
	flag.Parse()
	if *showVer {
		fmt.Println(version)
		return
	}
	log.SetFlags(log.LstdFlags)
	if *data != "" {
		_ = os.Setenv("POOLHEAT_DATA", *data)
	}
	if *host != "" {
		_ = os.Setenv("POOLHEAT_MINER_HOST", *host)
	}
	s := miner.LoadSettings()
	if err := os.MkdirAll(s.DataDir, 0o755); err != nil {
		log.Fatalf("[miner-poller] data dir: %v", err)
	}
	if err := miner.Run(s); err != nil {
		log.Fatalf("[miner-poller] exit: %v", err)
	}
}
