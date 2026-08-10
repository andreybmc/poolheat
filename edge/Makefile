# poolheat edge binaries (Keenetic Entware aarch64)
MODULE := github.com/andreybmc/poolheat/edge
VERSION ?= $(shell cat ../VERSION 2>/dev/null || echo dev)
OUT_DIR := ../packaging/entware/opt/bin
LDFLAGS := -s -w -X main.version=$(VERSION)

.PHONY: all build build-arm64 build-local tidy test clean devices miner

all: build-arm64

tidy:
	go mod tidy

build-local: tidy devices-local miner-local

devices-local:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/poolheat-devices-poller ./cmd/devices-poller

miner-local:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/poolheat-miner-poller ./cmd/miner-poller

build-arm64: tidy devices-arm64 miner-arm64

devices-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o bin/poolheat-devices-poller-linux-arm64 ./cmd/devices-poller
	mkdir -p $(OUT_DIR)
	cp bin/poolheat-devices-poller-linux-arm64 $(OUT_DIR)/poolheat-devices-poller
	chmod +x $(OUT_DIR)/poolheat-devices-poller

miner-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o bin/poolheat-miner-poller-linux-arm64 ./cmd/miner-poller
	mkdir -p $(OUT_DIR)
	cp bin/poolheat-miner-poller-linux-arm64 $(OUT_DIR)/poolheat-miner-poller
	chmod +x $(OUT_DIR)/poolheat-miner-poller
	@ls -la $(OUT_DIR)/poolheat-*-poller

test:
	go test ./...

clean:
	rm -rf bin
