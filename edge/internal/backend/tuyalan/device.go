package tuyalan

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// Device is a local Tuya LAN client (protocol 3.3 / 3.4 / 3.5).
// Replaces the Python tinytuya helper for status + set.
type Device struct {
	IP      string
	ID      string
	Key     string
	Version float64
	Timeout time.Duration

	conn   net.Conn
	seq    atomic.Uint32
	sess   []byte
	local  []byte
	verStr string
	is35   bool
	is34   bool
}

func (d *Device) Close() {
	if d.conn != nil {
		_ = d.conn.Close()
		d.conn = nil
	}
	d.sess = nil
}

func (d *Device) prepare() error {
	key := []byte(strings.TrimSpace(d.Key))
	if len(key) != 16 {
		return fmt.Errorf("local_key must be 16 bytes, got %d", len(key))
	}
	d.local = key
	v := d.Version
	if v <= 0 {
		v = 3.4
	}
	d.is35 = v >= 3.45
	d.is34 = v >= 3.35 && !d.is35
	if d.is35 {
		d.verStr = "3.5"
	} else if d.is34 {
		d.verStr = "3.4"
	} else if v >= 3.25 {
		d.verStr = "3.3"
	} else {
		d.verStr = "3.1"
	}
	if d.Timeout <= 0 {
		d.Timeout = 6 * time.Second
	}
	return nil
}

func (d *Device) Connect(ctx context.Context) error {
	if err := d.prepare(); err != nil {
		return err
	}
	d.Close()
	dialer := net.Dialer{Timeout: d.Timeout}
	addr := net.JoinHostPort(strings.TrimSpace(d.IP), "6668")
	c, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("tuya dial %s: %w", addr, err)
	}
	d.conn = c
	_ = c.SetDeadline(deadline(ctx, d.Timeout))
	if d.is34 || d.is35 {
		if err := d.handshake(ctx); err != nil {
			d.Close()
			return err
		}
	} else {
		d.sess = d.local
	}
	return nil
}

func deadline(ctx context.Context, fallback time.Duration) time.Time {
	if dl, ok := ctx.Deadline(); ok {
		return dl
	}
	return time.Now().Add(fallback)
}

func (d *Device) nextSeq() uint32 {
	return d.seq.Add(1)
}

func (d *Device) handshake(ctx context.Context) error {
	_ = d.conn.SetDeadline(deadline(ctx, d.Timeout))
	clientNonce := randNonce16()

	var startPayload []byte
	var err error
	if d.is35 {
		startPayload = clientNonce
	} else {
		// nonce is already 16 bytes — no PKCS#7 (matches tinytuya pad=False for 3.4 START)
		startPayload, err = encryptECB(d.local, clientNonce, false)
		if err != nil {
			return err
		}
	}
	if err := d.sendRaw(cmdSessStart, startPayload, d.local); err != nil {
		return fmt.Errorf("tuya sess start: %w", err)
	}
	resp, err := d.readRaw(d.local)
	if err != nil {
		return fmt.Errorf("tuya sess resp: %w", err)
	}
	body := resp.Payload
	if d.is34 {
		// device→client 55AA often prefixes a 4-byte retcode before ciphertext
		enc := body
		if len(enc) >= 20 && enc[0] == 0 && enc[1] == 0 && enc[2] == 0 && (len(enc)-4)%16 == 0 {
			enc = enc[4:]
		}
		plain, err := decryptECB(d.local, enc)
		if err != nil || len(plain) < 16 {
			if raw, err2 := decryptECBRaw(d.local, enc); err2 == nil && len(raw) >= 16 {
				plain = raw
				err = nil
			}
		}
		if err != nil {
			return fmt.Errorf("tuya sess resp decrypt: %w", err)
		}
		body = plain
	}
	// 3.5 sometimes prefixes a 4-byte retcode inside GCM plaintext.
	if len(body) >= 52 && body[0] == 0 && body[1] == 0 && body[2] == 0 {
		body = body[4:]
	}
	if len(body) < 16 {
		return fmt.Errorf("tuya sess resp short (%d)", len(body))
	}
	deviceNonce := body[:16]
	if len(body) >= 48 {
		gotHMAC := body[16:48]
		want := hmacSHA256(d.local, clientNonce)
		if !hmacEqual(want, gotHMAC) {
			return fmt.Errorf("tuya sess hmac (wrong key or version)")
		}
	}
	fin := hmacSHA256(d.local, deviceNonce)
	var finPayload []byte
	if d.is35 {
		finPayload = fin
	} else {
		finPayload, err = encryptECB(d.local, fin, true)
		if err != nil {
			return err
		}
	}
	if err := d.sendRaw(cmdSessFinish, finPayload, d.local); err != nil {
		return fmt.Errorf("tuya sess finish: %w", err)
	}
	if d.is35 {
		d.sess, err = sessionKey35(d.local, clientNonce, deviceNonce)
	} else {
		d.sess, err = sessionKey34(d.local, clientNonce, deviceNonce)
	}
	if err != nil {
		return fmt.Errorf("tuya session key: %w", err)
	}
	return nil
}

func (d *Device) sendRaw(cmd uint32, payload, key []byte) error {
	seq := d.nextSeq()
	var wire []byte
	var err error
	if d.is35 {
		wire, err = pack6699(seq, cmd, payload, key)
	} else {
		wire = pack55(seq, cmd, payload, key, d.is34)
	}
	if err != nil {
		return err
	}
	_, err = d.conn.Write(wire)
	return err
}

func (d *Device) send(cmd uint32, payload []byte) error {
	key := d.sess
	if key == nil {
		key = d.local
	}
	return d.sendRaw(cmd, payload, key)
}

func (d *Device) readRaw(key []byte) (*packet, error) {
	if d.is35 {
		return readPacket6699(d.conn, key)
	}
	useHMAC := d.is34
	return readPacket55(d.conn, key, useHMAC)
}

func (d *Device) read() (*packet, error) {
	key := d.sess
	if key == nil {
		key = d.local
	}
	return d.readRaw(key)
}

func (d *Device) encryptPayload(cmd uint32, jsonObj any) ([]byte, error) {
	raw, err := json.Marshal(jsonObj)
	if err != nil {
		return nil, err
	}
	plain := raw
	if !skipHeaderCmds(cmd) && (d.is34 || d.is35 || d.verStr == "3.3") {
		plain = versionHeader(d.verStr, raw)
	}
	if d.is35 {
		return plain, nil // GCM at frame layer
	}
	if d.is34 || d.verStr == "3.3" {
		return encryptECB(d.sess, plain, true)
	}
	return plain, nil
}

func (d *Device) decryptInner(p *packet) []byte {
	b := p.Payload
	if d.is34 || d.verStr == "3.3" {
		if plain, err := decryptECB(d.sess, b); err == nil {
			b = plain
		}
	}
	return stripPayload(b)
}

// Status queries DPS. Connects if needed.
func (d *Device) Status(ctx context.Context) (map[string]any, error) {
	if err := d.Connect(ctx); err != nil {
		return nil, err
	}
	defer d.Close()
	_ = d.conn.SetDeadline(deadline(ctx, d.Timeout))
	cmd := uint32(cmdDpQuery)
	payloadObj := any(map[string]any{})
	if d.is34 || d.is35 {
		cmd = cmdDpQueryNew
	} else {
		payloadObj = map[string]any{"gwId": d.ID, "devId": d.ID}
	}
	wire, err := d.encryptPayload(cmd, payloadObj)
	if err != nil {
		return nil, err
	}
	tryStatus := []uint32{cmd}
	if d.is34 && cmd == cmdDpQueryNew {
		tryStatus = append(tryStatus, cmdDpQuery)
	}
	var lastErr error
	for si, sc := range tryStatus {
		if si > 0 {
			d.Close()
			if err := d.Connect(ctx); err != nil {
				lastErr = err
				continue
			}
			_ = d.conn.SetDeadline(deadline(ctx, d.Timeout))
		}
		w := wire
		if sc != cmd {
			obj2 := map[string]any{"gwId": d.ID, "devId": d.ID, "uid": d.ID, "t": strconv.FormatInt(time.Now().Unix(), 10)}
			var err error
			w, err = d.encryptPayload(sc, obj2)
			if err != nil {
				continue
			}
		}
		if err := d.send(sc, w); err != nil {
			lastErr = fmt.Errorf("tuya status send: %w", err)
			continue
		}
		for i := 0; i < 3; i++ {
			p, err := d.read()
			if err != nil {
				lastErr = fmt.Errorf("tuya status: %w", err)
				break
			}
			inner := d.decryptInner(p)
			if m := decodeJSON(inner); m != nil {
				if got := extractDPS(m); got != nil {
					return got, nil
				}
				if len(m) > 0 {
					return m, nil
				}
			}
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, fmt.Errorf("tuya status: empty")
}

// Set writes one DPS (bool/int/string) and re-reads.
func (d *Device) Set(ctx context.Context, dpsKey string, val any) (map[string]any, error) {
	return d.SetDPS(ctx, map[string]any{dpsKey: val})
}

// SetDPS writes several DPS in one CONTROL / CONTROL_NEW (switch + brightness + mode).
func (d *Device) SetDPS(ctx context.Context, dps map[string]any) (map[string]any, error) {
	if len(dps) == 0 {
		return nil, fmt.Errorf("tuya set: empty dps")
	}
	if err := d.Connect(ctx); err != nil {
		return nil, err
	}
	defer d.Close()
	_ = d.conn.SetDeadline(deadline(ctx, d.Timeout))
	cmd := uint32(cmdControl)
	var obj any
	if d.is34 || d.is35 {
		cmd = cmdControlNew
		obj = map[string]any{
			"protocol": 5,
			"t":        time.Now().Unix(),
			"data":     map[string]any{"dps": dps},
		}
	} else {
		obj = map[string]any{
			"devId": d.ID,
			"uid":   d.ID,
			"t":     strconv.FormatInt(time.Now().Unix(), 10),
			"dps":   dps,
		}
	}
	wire, err := d.encryptPayload(cmd, obj)
	if err != nil {
		return nil, err
	}
	tryCmds := []uint32{cmd}
	if d.is34 && cmd == cmdControlNew {
		tryCmds = append(tryCmds, cmdControl) // classic 0x07 if protocol-5 is ignored
	}
	var last map[string]any
	var sent bool
	for _, c := range tryCmds {
		w := wire
		if c != cmd {
			obj2 := map[string]any{
				"devId": d.ID,
				"uid":   d.ID,
				"t":     strconv.FormatInt(time.Now().Unix(), 10),
				"dps":   dps,
			}
			var err error
			w, err = d.encryptPayload(c, obj2)
			if err != nil {
				continue
			}
		}
		if err := d.send(c, w); err != nil {
			return nil, fmt.Errorf("tuya set send: %w", err)
		}
		sent = true
		for i := 0; i < 2; i++ {
			p, err := d.read()
			if err != nil {
				break
			}
			if m := decodeJSON(d.decryptInner(p)); m != nil {
				if got := extractDPS(m); got != nil {
					last = got
				}
			}
		}
		if last != nil {
			break
		}
	}
	if !sent {
		return nil, fmt.Errorf("tuya set send: no command")
	}
	// confirm with DP query on the same socket (session still valid)
	qcmd := uint32(cmdDpQuery)
	qobj := any(map[string]any{})
	if d.is34 || d.is35 {
		qcmd = cmdDpQueryNew
	} else {
		qobj = map[string]any{"gwId": d.ID, "devId": d.ID}
	}
	qwire, err := d.encryptPayload(qcmd, qobj)
	if err == nil {
		if err := d.send(qcmd, qwire); err == nil {
			if p, err := d.read(); err == nil {
				if m := decodeJSON(d.decryptInner(p)); m != nil {
					if got := extractDPS(m); got != nil {
						last = got
					}
				}
			}
		}
	}
	if last == nil {
		// 3.4 plugs often ACK CONTROL and drop the socket — treat send as applied
		last = dps
	}
	return last, nil
}

func decodeJSON(b []byte) map[string]any {
	b = stripPayload(b)
	i := 0
	for i < len(b) && b[i] != '{' && b[i] != '[' {
		i++
	}
	if i >= len(b) {
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal(b[i:], &m); err != nil {
		// trim trailing junk
		if j := lastJSONEnd(b[i:]); j > 0 {
			if err2 := json.Unmarshal(b[i:i+j], &m); err2 != nil {
				return nil
			}
		} else {
			return nil
		}
	}
	return m
}

func lastJSONEnd(b []byte) int {
	if len(b) == 0 || b[0] != '{' {
		return 0
	}
	depth := 0
	inStr := false
	esc := false
	for i, c := range b {
		if inStr {
			if esc {
				esc = false
				continue
			}
			if c == '\\' {
				esc = true
				continue
			}
			if c == '"' {
				inStr = false
			}
			continue
		}
		switch c {
		case '"':
			inStr = true
		case '{', '[':
			depth++
		case '}', ']':
			depth--
			if depth == 0 {
				return i + 1
			}
		}
	}
	return 0
}

func extractDPS(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	if d, ok := m["dps"].(map[string]any); ok && len(d) > 0 {
		return d
	}
	if data, ok := m["data"].(map[string]any); ok {
		if d, ok := data["dps"].(map[string]any); ok && len(d) > 0 {
			return d
		}
	}
	return nil
}

// TryConnect reports whether this version handshake works.
func TryVersion(ctx context.Context, ip, id, key string, ver float64, timeout time.Duration) error {
	d := &Device{IP: ip, ID: id, Key: key, Version: ver, Timeout: timeout}
	err := d.Connect(ctx)
	d.Close()
	return err
}
