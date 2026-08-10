package miner

// History is intentionally not written from the Go poller.
// serve.py collector_loop hydrates samples from live_cache.json (no :4028)
// when the Go miner-poller is active — keeps the binary small and CGO-free.
