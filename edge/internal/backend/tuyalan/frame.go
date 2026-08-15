package tuyalan

import (
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"
)

const (
	prefix55aa uint32 = 0x000055aa
	footer55aa uint32 = 0x0000aa55
	prefix6699 uint32 = 0x00006699
	footer6699 uint32 = 0x00009966

	cmdSessStart  uint32 = 0x03
	cmdSessResp   uint32 = 0x04
	cmdSessFinish uint32 = 0x05
	cmdControl    uint32 = 0x07
	cmdControlNew uint32 = 0x0d
	cmdDpQuery    uint32 = 0x0a
	cmdDpQueryNew uint32 = 0x10
)

type packet struct {
	Seq     uint32
	Cmd     uint32
	Payload []byte
	Ret     uint32
	HasRet  bool
}

func skipHeaderCmds(cmd uint32) bool {
	switch cmd {
	case cmdSessStart, cmdSessResp, cmdSessFinish, cmdDpQuery, cmdDpQueryNew, 0x09, 0x12, 0x40:
		return true
	}
	return false
}

func pack55(seq, cmd uint32, payload, hmacKey []byte, useHMAC bool) []byte {
	// client→device: no retcode
	body := payload
	// length = payload + integrity + footer
	intLen := 4
	if useHMAC {
		intLen = 32
	}
	length := uint32(len(body) + intLen + 4)
	buf := make([]byte, 0, 16+len(body)+intLen+4)
	buf = binary.BigEndian.AppendUint32(buf, prefix55aa)
	buf = binary.BigEndian.AppendUint32(buf, seq)
	buf = binary.BigEndian.AppendUint32(buf, cmd)
	buf = binary.BigEndian.AppendUint32(buf, length)
	buf = append(buf, body...)
	macOver := buf // prefix..payload
	if useHMAC {
		buf = append(buf, hmacSHA256(hmacKey, macOver)...)
	} else {
		buf = binary.BigEndian.AppendUint32(buf, crc32.ChecksumIEEE(macOver))
	}
	buf = binary.BigEndian.AppendUint32(buf, footer55aa)
	return buf
}

func pack6699(seq, cmd uint32, plain, gcmKey []byte) ([]byte, error) {
	iv := randIV12()
	// length = IV + ciphertext + tag (no footer)
	length := uint32(12 + len(plain) + 16)
	headerAfterPrefix := make([]byte, 0, 14)
	headerAfterPrefix = append(headerAfterPrefix, 0, 0) // reserved
	headerAfterPrefix = binary.BigEndian.AppendUint32(headerAfterPrefix, seq)
	headerAfterPrefix = binary.BigEndian.AppendUint32(headerAfterPrefix, cmd)
	headerAfterPrefix = binary.BigEndian.AppendUint32(headerAfterPrefix, length)
	ct, tag, err := encryptGCM(gcmKey, iv, headerAfterPrefix, plain)
	if err != nil {
		return nil, err
	}
	buf := make([]byte, 0, 4+14+12+len(ct)+16+4)
	buf = binary.BigEndian.AppendUint32(buf, prefix6699)
	buf = append(buf, headerAfterPrefix...)
	buf = append(buf, iv...)
	buf = append(buf, ct...)
	buf = append(buf, tag...)
	buf = binary.BigEndian.AppendUint32(buf, footer6699)
	return buf, nil
}

func readFull(r io.Reader, n int) ([]byte, error) {
	buf := make([]byte, n)
	_, err := io.ReadFull(r, buf)
	return buf, err
}

func readPacket55(r io.Reader, hmacKey []byte, useHMAC bool) (*packet, error) {
	hdr, err := readFull(r, 16)
	if err != nil {
		return nil, err
	}
	if binary.BigEndian.Uint32(hdr[0:4]) != prefix55aa {
		return nil, fmt.Errorf("55aa header 0x%x", binary.BigEndian.Uint32(hdr[0:4]))
	}
	seq := binary.BigEndian.Uint32(hdr[4:8])
	cmd := binary.BigEndian.Uint32(hdr[8:12])
	length := binary.BigEndian.Uint32(hdr[12:16])
	if length < 8 || length > 1<<20 {
		return nil, fmt.Errorf("55aa length %d", length)
	}
	rest, err := readFull(r, int(length))
	if err != nil {
		return nil, err
	}
	intLen := 4
	if useHMAC {
		intLen = 32
	}
	if len(rest) < intLen+4 {
		return nil, fmt.Errorf("55aa short body")
	}
	if binary.BigEndian.Uint32(rest[len(rest)-4:]) != footer55aa {
		return nil, fmt.Errorf("55aa footer")
	}
	payloadAndMac := rest[:len(rest)-4]
	mac := payloadAndMac[len(payloadAndMac)-intLen:]
	enc := payloadAndMac[:len(payloadAndMac)-intLen]
	macOver := append(append([]byte{}, hdr...), enc...)
	if useHMAC {
		want := hmacSHA256(hmacKey, macOver)
		if !hmacEqual(want, mac) {
			return nil, fmt.Errorf("55aa hmac mismatch")
		}
	}
	p := &packet{Seq: seq, Cmd: cmd, Payload: enc}
	// tinytuya unpack always takes a 4-byte retcode on device→client 55AA
	if len(enc) >= 4 && enc[0] == 0 && enc[1] == 0 && enc[2] == 0 {
		p.Ret = binary.BigEndian.Uint32(enc[:4])
		p.HasRet = true
		p.Payload = enc[4:]
	}
	return p, nil
}

func readPacket6699(r io.Reader, gcmKey []byte) (*packet, error) {
	hdr, err := readFull(r, 18) // prefix + reserved + seq + cmd + len
	if err != nil {
		return nil, err
	}
	if binary.BigEndian.Uint32(hdr[0:4]) != prefix6699 {
		return nil, fmt.Errorf("6699 header 0x%x", binary.BigEndian.Uint32(hdr[0:4]))
	}
	aad := hdr[4:18]
	seq := binary.BigEndian.Uint32(hdr[6:10])
	cmd := binary.BigEndian.Uint32(hdr[10:14])
	length := binary.BigEndian.Uint32(hdr[14:18])
	if length < 28 || length > 1<<20 {
		return nil, fmt.Errorf("6699 length %d", length)
	}
	body, err := readFull(r, int(length)+4)
	if err != nil {
		return nil, err
	}
	if binary.BigEndian.Uint32(body[len(body)-4:]) != footer6699 {
		return nil, fmt.Errorf("6699 footer")
	}
	body = body[:len(body)-4]
	iv := body[:12]
	tag := body[len(body)-16:]
	ct := body[12 : len(body)-16]
	plain, err := decryptGCM(gcmKey, iv, aad, ct, tag)
	if err != nil {
		return nil, fmt.Errorf("6699 gcm: %w", err)
	}
	p := &packet{Seq: seq, Cmd: cmd, Payload: plain}
	// tinytuya: no_retcode=False — first 4 plaintext bytes are retcode (incl. handshake)
	if len(plain) >= 4 {
		p.Ret = binary.BigEndian.Uint32(plain[:4])
		p.HasRet = true
		p.Payload = plain[4:]
	}
	return p, nil
}

func hmacEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	var v byte
	for i := range a {
		v |= a[i] ^ b[i]
	}
	return v == 0
}

func versionHeader(ver string, json []byte) []byte {
	h := make([]byte, 0, 15+len(json))
	h = append(h, ver...)
	if pad := 15 - len(ver); pad > 0 {
		h = append(h, make([]byte, pad)...)
	}
	return append(h, json...)
}

func stripPayload(b []byte) []byte {
	if len(b) >= 15 && b[0] == '3' && b[1] == '.' {
		return b[15:]
	}
	// leftover retcode
	if len(b) >= 4 && b[0] == 0 && b[1] == 0 && b[2] == 0 && b[3] == 0 {
		if len(b) >= 19 && b[4] == '3' && b[5] == '.' {
			return b[19:]
		}
		rest := b[4:]
		if len(rest) > 0 && rest[0] == '{' {
			return rest
		}
	}
	return b
}
