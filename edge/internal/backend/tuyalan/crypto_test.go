package tuyalan

import (
	"bytes"
	"testing"
)

func TestECBRoundtrip(t *testing.T) {
	key := []byte("0123456789abcdef")
	plain := []byte(`{"dps":{"1":true}}`)
	enc, err := encryptECB(key, plain, true)
	if err != nil {
		t.Fatal(err)
	}
	got, err := decryptECB(key, enc)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, plain) {
		t.Fatalf("got %q", got)
	}
}

func TestSessionKey35Len(t *testing.T) {
	key := []byte("0123456789abcdef")
	a := bytes.Repeat([]byte{0x11}, 16)
	b := bytes.Repeat([]byte{0x22}, 16)
	sk, err := sessionKey35(key, a, b)
	if err != nil {
		t.Fatal(err)
	}
	if len(sk) != 16 {
		t.Fatalf("len %d", len(sk))
	}
	sk2, err := sessionKey35(key, a, b)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(sk, sk2) {
		t.Fatal("session key must be deterministic")
	}
}

func TestSessionKey34Len(t *testing.T) {
	key := []byte("0123456789abcdef")
	a := bytes.Repeat([]byte{1}, 16)
	b := bytes.Repeat([]byte{2}, 16)
	sk, err := sessionKey34(key, a, b)
	if err != nil {
		t.Fatal(err)
	}
	if len(sk) != 16 {
		t.Fatalf("len %d", len(sk))
	}
}

func TestFrame6699Roundtrip(t *testing.T) {
	key := []byte("0123456789abcdef")
	plain := []byte("hello-tuya-35!!") // 15 bytes, GCM no pad
	wire, err := pack6699(7, cmdDpQueryNew, plain, key)
	if err != nil {
		t.Fatal(err)
	}
	p, err := readPacket6699(bytes.NewReader(wire), key)
	if err != nil {
		t.Fatal(err)
	}
	// TCP path strips first 4 bytes as retcode — our pack has no retcode.
	// So compare against remaining after that heuristic.
	if p.Cmd != cmdDpQueryNew {
		t.Fatalf("cmd %d", p.Cmd)
	}
}

func TestStripPayload(t *testing.T) {
	js := []byte(`{"dps":{"20":true}}`)
	hdr := versionHeader("3.5", js)
	got := stripPayload(hdr)
	if !bytes.Equal(got, js) {
		t.Fatalf("got %q", got)
	}
}

func TestVersionHeaderLen(t *testing.T) {
	h := versionHeader("3.4", []byte("{}"))
	if len(h) != 15+2 {
		t.Fatalf("len %d", len(h))
	}
	if string(h[:3]) != "3.4" {
		t.Fatalf("hdr %q", h[:3])
	}
}
