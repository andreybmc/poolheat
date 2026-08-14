package miner

import (
	"testing"
)

func TestExpandLastOctetRange(t *testing.T) {
	ips, err := expandOneRange("192.168.1.10-13")
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(ips) != 4 {
		t.Fatalf("want 4 got %d %v", len(ips), ips)
	}
	if ips[0] != "192.168.1.10" || ips[3] != "192.168.1.13" {
		t.Fatalf("unexpected %v", ips)
	}
}

func TestExpandMultiOctetRange(t *testing.T) {
	// 192.168.1-2.1-3 → 2*3 = 6 hosts
	ips, err := expandOneRange("192.168.1-2.1-3")
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(ips) != 6 {
		t.Fatalf("want 6 got %d %v", len(ips), ips)
	}
	want := map[string]bool{
		"192.168.1.1": true, "192.168.1.2": true, "192.168.1.3": true,
		"192.168.2.1": true, "192.168.2.2": true, "192.168.2.3": true,
	}
	for _, ip := range ips {
		if !want[ip] {
			t.Fatalf("unexpected %s in %v", ip, ips)
		}
	}
}

func TestExpandFullIPRange(t *testing.T) {
	ips, err := expandOneRange("192.168.1.250-192.168.2.2")
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	// 250,251,252,253,254,255, 2.0, 2.1, 2.2 = 9
	if len(ips) != 9 {
		t.Fatalf("want 9 got %d %v", len(ips), ips)
	}
	if ips[0] != "192.168.1.250" || ips[len(ips)-1] != "192.168.2.2" {
		t.Fatalf("bounds %v", ips)
	}
}

func TestExpandRangesSkipsBad(t *testing.T) {
	ips, err := expandRanges([]string{
		"172.16.100.0/30",
		"192.168.1.10-13",
		"not-an-ip",
	})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	// /30 hosts = 2 usable typically after strip; + 4 from short range
	if len(ips) < 4 {
		t.Fatalf("too few hosts: %v", ips)
	}
	// short range present
	found := false
	for _, ip := range ips {
		if ip == "192.168.1.12" {
			found = true
		}
	}
	if !found {
		t.Fatalf("missing short-range host in %v", ips)
	}
}
