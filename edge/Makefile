# poolheat edge binaries (Keenetic Entware aarch64)
MODULE := github.com/andreybmc/poolheat/edge
BIN_NAME := poolheat-devices-poller
VERSION ?= $(shell cat ../VERSION 2>/dev/null || echo dev)
OUT_DIR := ../packaging/entware/opt/bin
LDFLAGS := -s -w -X main.version=$(VERSION)

.PHONY: all build build-arm64 build-local tidy test clean

all: build-arm64

tidy:
	go mod tidy

build-local: tidy
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/$(BIN_NAME) ./cmd/devices-poller

build-arm64: tidy
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o bin/$(BIN_NAME)-linux-arm64 ./cmd/devices-poller
	mkdir -p $(OUT_DIR)
	cp bin/$(BIN_NAME)-linux-arm64 $(OUT_DIR)/$(BIN_NAME)
	chmod +x $(OUT_DIR)/$(BIN_NAME)
	@ls -la $(OUT_DIR)/$(BIN_NAME) bin/$(BIN_NAME)-linux-arm64

test:
	go test ./...

clean:
	rm -rf bin
