# poolheat edge binaries for Keenetic Entware
#   aarch64  → Peak / Hero / Titan  (pkg arch aarch64-3.10)
#   mipsle   → Giant / Giga / Ultra (pkg arch mipselsf-3.4, softfloat)
MODULE := github.com/andreybmc/poolheat/edge
VERSION ?= $(shell cat ../VERSION 2>/dev/null || echo dev)
OUT_DIR := ../packaging/entware/opt/bin
DIST_DIR := ../dist
LDFLAGS := -s -w -X main.version=$(VERSION)

.PHONY: all build build-arm64 build-mipsel build-all-arch stage-arm64 stage-mipsel \
	build-local tidy test clean devices miner

all: build-arm64

tidy:
	go mod tidy

build-local: tidy devices-local miner-local

devices-local:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/poolheat-devices-poller ./cmd/devices-poller

miner-local:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/poolheat-miner-poller ./cmd/miner-poller

# ── aarch64 (Peak) ──────────────────────────────────────────────────────────
build-arm64: tidy devices-arm64 miner-arm64 stage-arm64

devices-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" \
		-o bin/poolheat-devices-poller-linux-arm64 ./cmd/devices-poller

miner-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" \
		-o bin/poolheat-miner-poller-linux-arm64 ./cmd/miner-poller

stage-arm64:
	mkdir -p $(OUT_DIR) $(DIST_DIR)/bin
	cp bin/poolheat-devices-poller-linux-arm64 $(OUT_DIR)/poolheat-devices-poller
	cp bin/poolheat-miner-poller-linux-arm64 $(OUT_DIR)/poolheat-miner-poller
	cp bin/poolheat-devices-poller-linux-arm64 $(DIST_DIR)/bin/
	cp bin/poolheat-miner-poller-linux-arm64 $(DIST_DIR)/bin/
	chmod +x $(OUT_DIR)/poolheat-devices-poller $(OUT_DIR)/poolheat-miner-poller
	@ls -la $(OUT_DIR)/poolheat-*-poller

# ── mipsel softfloat (Giant) ────────────────────────────────────────────────
build-mipsel: tidy devices-mipsel miner-mipsel stage-mipsel

devices-mipsel:
	CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=softfloat go build -ldflags "$(LDFLAGS)" \
		-o bin/poolheat-devices-poller-linux-mipsle ./cmd/devices-poller

miner-mipsel:
	CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=softfloat go build -ldflags "$(LDFLAGS)" \
		-o bin/poolheat-miner-poller-linux-mipsle ./cmd/miner-poller

stage-mipsel:
	mkdir -p $(OUT_DIR) $(DIST_DIR)/bin
	cp bin/poolheat-devices-poller-linux-mipsle $(OUT_DIR)/poolheat-devices-poller
	cp bin/poolheat-miner-poller-linux-mipsle $(OUT_DIR)/poolheat-miner-poller
	cp bin/poolheat-devices-poller-linux-mipsle $(DIST_DIR)/bin/
	cp bin/poolheat-miner-poller-linux-mipsle $(DIST_DIR)/bin/
	chmod +x $(OUT_DIR)/poolheat-devices-poller $(OUT_DIR)/poolheat-miner-poller
	@ls -la $(OUT_DIR)/poolheat-*-poller

# Build both arches into dist/bin/ (does not leave a single arch in OUT_DIR)
build-all-arch: tidy devices-arm64 miner-arm64 devices-mipsel miner-mipsel
	mkdir -p $(DIST_DIR)/bin
	cp bin/poolheat-devices-poller-linux-arm64 $(DIST_DIR)/bin/
	cp bin/poolheat-miner-poller-linux-arm64 $(DIST_DIR)/bin/
	cp bin/poolheat-devices-poller-linux-mipsle $(DIST_DIR)/bin/
	cp bin/poolheat-miner-poller-linux-mipsle $(DIST_DIR)/bin/
	chmod +x $(DIST_DIR)/bin/poolheat-*-poller-linux-*
	@ls -la $(DIST_DIR)/bin/

test:
	go test ./...

clean:
	rm -rf bin
