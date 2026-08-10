package backend

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/config"
)

func controlTapo(ctx context.Context, on *bool, cfg config.DeviceCfg) (Result, error) {
	ip := strings.TrimSpace(cfg.IP)
	if ip == "" {
		return Result{}, fmt.Errorf("Tapo IP не настроен")
	}
	email := strings.TrimSpace(cfg.Email)
	password := cfg.Password
	c, err := tapoConnect(ctx, ip, email, password)
	if err != nil {
		return Result{}, err
	}
	defer c.close()

	if on == nil {
		got, err := c.getOn(ctx)
		if err != nil {
			return Result{}, err
		}
		out := Result{On: &got, Backend: "tapo"}
		if pm := c.getPower(ctx); pm != nil {
			out.Power = pm
		}
		return out, nil
	}
	want := *on
	// read current
	cur, err := c.getOn(ctx)
	if err == nil && cur == want {
		out := Result{On: &cur, Backend: "tapo", Skipped: true, Reason: "already_in_state"}
		if pm := c.getPower(ctx); pm != nil {
			out.Power = pm
		}
		return out, nil
	}
	if err := c.setOn(ctx, want); err != nil {
		return Result{}, err
	}
	got := want
	if g2, err2 := c.getOn(ctx); err2 == nil {
		got = g2
	}
	out := Result{On: &got, Backend: "tapo"}
	if pm := c.getPower(ctx); pm != nil {
		out.Power = pm
	}
	return out, nil
}

type tapoClient interface {
	getOn(ctx context.Context) (bool, error)
	setOn(ctx context.Context, on bool) error
	getPower(ctx context.Context) map[string]any
	close()
}

func tapoConnect(ctx context.Context, ip, email, password string) (tapoClient, error) {
	klap := &tapoKlap{ip: ip, email: email, password: password, termUUID: newUUID()}
	if err := klap.connect(ctx); err != nil {
		low := strings.ToLower(err.Error())
		if strings.Contains(low, "auth mismatch") || strings.Contains(low, "bad email/password") {
			return nil, err
		}
		// KLAP failed — no full legacy RSA in Go for now; surface KLAP error
		return nil, fmt.Errorf("Tapo KLAP: %w", err)
	}
	return klap, nil
}

// ── KLAP ────────────────────────────────────────────────────────────────────

type klapSession struct {
	key []byte
	iv  []byte // 12 bytes
	seq int32
	sig []byte // 28 bytes
}

func newKlapSession(localSeed, remoteSeed, userHash []byte) *klapSession {
	key := sha256.Sum256(append(append(append([]byte("lsk"), localSeed...), remoteSeed...), userHash...))
	fullIV := sha256.Sum256(append(append(append([]byte("iv"), localSeed...), remoteSeed...), userHash...))
	sig := sha256.Sum256(append(append(append([]byte("ldk"), localSeed...), remoteSeed...), userHash...))
	seq := int32(binary.BigEndian.Uint32(fullIV[len(fullIV)-4:]))
	return &klapSession{
		key: key[:16],
		iv:  fullIV[:12],
		seq: seq,
		sig: sig[:28],
	}
}

func (s *klapSession) encrypt(msg []byte) (ct []byte, seq int32, err error) {
	s.seq++
	seq = s.seq
	ivSeq := append(append([]byte{}, s.iv...), i32be(seq)...)
	block, err := aes.NewCipher(s.key)
	if err != nil {
		return nil, 0, err
	}
	padded := pkcs7Pad(msg, aes.BlockSize)
	enc := make([]byte, len(padded))
	cipher.NewCBCEncrypter(block, ivSeq).CryptBlocks(enc, padded)
	h := sha256.Sum256(append(append(append([]byte{}, s.sig...), i32be(seq)...), enc...))
	return append(h[:], enc...), seq, nil
}

func (s *klapSession) decrypt(msg []byte) ([]byte, error) {
	if len(msg) < 32+aes.BlockSize {
		return nil, fmt.Errorf("KLAP ciphertext too short")
	}
	ivSeq := append(append([]byte{}, s.iv...), i32be(s.seq)...)
	block, err := aes.NewCipher(s.key)
	if err != nil {
		return nil, err
	}
	ct := msg[32:]
	if len(ct)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("KLAP bad block size")
	}
	plain := make([]byte, len(ct))
	cipher.NewCBCDecrypter(block, ivSeq).CryptBlocks(plain, ct)
	return pkcs7Unpad(plain)
}

type tapoKlap struct {
	ip, email, password string
	termUUID            string
	cookie              string
	session             *klapSession
	proto               string
	http                *http.Client
}

func (c *tapoKlap) base() string { return "http://" + c.ip + "/app" }

func (c *tapoKlap) client() *http.Client {
	if c.http == nil {
		c.http = &http.Client{Timeout: 8 * time.Second}
	}
	return c.http
}

func (c *tapoKlap) close() {}

func (c *tapoKlap) connect(ctx context.Context) error {
	localSeed := make([]byte, 16)
	if _, err := rand.Read(localSeed); err != nil {
		return err
	}
	status, body, cookie, err := c.httpBytes(ctx, c.base()+"/handshake1", localSeed, "")
	if err != nil {
		return err
	}
	if status != 200 {
		return fmt.Errorf("KLAP handshake1 HTTP %d", status)
	}
	if len(body) < 48 {
		return fmt.Errorf("KLAP handshake1 bad body len=%d", len(body))
	}
	remoteSeed := body[:16]
	serverHash := body[16:48]
	if cookie != "" {
		c.cookie = cookie
	}

	var matchedAuth []byte
	var matchedProto string
	for _, cand := range c.candidateAuths() {
		proto := "v2"
		if strings.HasPrefix(cand.label, "v1") {
			proto = "v1"
		}
		if bytes.Equal(h1Hash(localSeed, remoteSeed, cand.hash, proto), serverHash) {
			matchedAuth = cand.hash
			matchedProto = proto
			break
		}
	}
	if matchedAuth == nil {
		return fmt.Errorf("KLAP auth mismatch (bad email/password? case-sensitive)")
	}
	c.proto = matchedProto

	h2 := h2Hash(localSeed, remoteSeed, matchedAuth, c.proto)
	status2, _, cookie2, err := c.httpBytes(ctx, c.base()+"/handshake2", h2, c.cookie)
	if err != nil {
		return err
	}
	if cookie2 != "" {
		c.cookie = cookie2
	}
	if status2 != 200 {
		return fmt.Errorf("KLAP handshake2 HTTP %d (bad email/password?)", status2)
	}
	c.session = newKlapSession(localSeed, remoteSeed, matchedAuth)
	return nil
}

type authCand struct {
	label string
	hash  []byte
}

func (c *tapoKlap) candidateAuths() []authCand {
	email := c.email
	pw := c.password
	emailSHA1 := sha1Hex(email)
	var out []authCand
	seen := map[string]bool{}
	add := func(label string, h []byte) {
		k := hex.EncodeToString(h)
		if seen[k] {
			return
		}
		seen[k] = true
		out = append(out, authCand{label, h})
	}
	for _, proto := range []string{"v2", "v1"} {
		gen := authHashV2
		if proto == "v1" {
			gen = authHashV1
		}
		for _, pair := range []struct{ lab, user string }{
			{proto + ":email", email},
			{proto + ":email_sha1hex", emailSHA1},
			{proto + ":blank", ""},
		} {
			if pair.user == "" && pw != "" {
				add(proto+":blank_creds", gen("", ""))
				continue
			}
			u := pair.user
			p := pw
			if u == "" {
				p = ""
			}
			add(pair.lab, gen(u, p))
		}
		add(proto+":email_empty_pw", gen(email, ""))
	}
	return out
}

func authHashV1(user, pass string) []byte {
	u := md5.Sum([]byte(user))
	p := md5.Sum([]byte(pass))
	h := md5.Sum(append(u[:], p[:]...))
	return h[:]
}

func authHashV2(user, pass string) []byte {
	u := sha1.Sum([]byte(user))
	p := sha1.Sum([]byte(pass))
	h := sha256.Sum256(append(u[:], p[:]...))
	return h[:]
}

func h1Hash(local, remote, auth []byte, proto string) []byte {
	if proto == "v1" {
		h := sha256.Sum256(append(local, auth...))
		return h[:]
	}
	h := sha256.Sum256(append(append(local, remote...), auth...))
	return h[:]
}

func h2Hash(local, remote, auth []byte, proto string) []byte {
	if proto == "v1" {
		h := sha256.Sum256(append(remote, auth...))
		return h[:]
	}
	h := sha256.Sum256(append(append(remote, local...), auth...))
	return h[:]
}

func (c *tapoKlap) request(ctx context.Context, method string, params map[string]any) (map[string]any, error) {
	if c.session == nil {
		return nil, fmt.Errorf("KLAP not connected")
	}
	if params == nil {
		params = map[string]any{}
	}
	payload := map[string]any{
		"method":          method,
		"params":          params,
		"requestTimeMils": time.Now().UnixMilli(),
		"terminalUUID":    c.termUUID,
	}
	raw, _ := json.Marshal(payload)
	enc, seq, err := c.session.encrypt(raw)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/request?seq=%d", c.base(), seq)
	status, body, _, err := c.httpBytes(ctx, url, enc, c.cookie)
	if err != nil {
		return nil, err
	}
	if status == 403 {
		c.session = nil
		return nil, fmt.Errorf("KLAP session expired (403) — retry")
	}
	if status != 200 {
		return nil, fmt.Errorf("KLAP request HTTP %d", status)
	}
	plain, err := c.session.decrypt(body)
	if err != nil {
		return nil, fmt.Errorf("KLAP decrypt: %w", err)
	}
	var j map[string]any
	if err := json.Unmarshal(plain, &j); err != nil {
		return nil, fmt.Errorf("KLAP parse: %w", err)
	}
	return j, nil
}

func (c *tapoKlap) getOn(ctx context.Context) (bool, error) {
	inner, err := c.request(ctx, "get_device_info", nil)
	if err != nil {
		return false, err
	}
	if code, _ := asIntAny(inner["error_code"]); code != 0 {
		return false, fmt.Errorf("get_device_info error_code=%v", inner["error_code"])
	}
	res, _ := inner["result"].(map[string]any)
	return asBoolAny(res["device_on"]), nil
}

func (c *tapoKlap) setOn(ctx context.Context, on bool) error {
	inner, err := c.request(ctx, "set_device_info", map[string]any{"device_on": on})
	if err != nil {
		return err
	}
	if code, _ := asIntAny(inner["error_code"]); code != 0 {
		return fmt.Errorf("set_device_info error_code=%v", inner["error_code"])
	}
	return nil
}

func (c *tapoKlap) getPower(ctx context.Context) map[string]any {
	try := func(method string) map[string]any {
		inner, err := c.request(ctx, method, nil)
		if err != nil {
			return nil
		}
		if code, _ := asIntAny(inner["error_code"]); code != 0 {
			return nil
		}
		res, _ := inner["result"].(map[string]any)
		return tapoResultToPower(res)
	}
	if m := try("get_energy_usage"); m != nil {
		return m
	}
	return try("get_device_info")
}

func (c *tapoKlap) httpBytes(ctx context.Context, url string, data []byte, cookie string) (int, []byte, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return 0, nil, "", err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("User-Agent", "poolheat-tapo/1.0")
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	resp, err := c.client().Do(req)
	if err != nil {
		return 0, nil, "", err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp.StatusCode, nil, "", err
	}
	sc := parseSessionCookie(resp.Header.Get("Set-Cookie"))
	return resp.StatusCode, body, sc, nil
}

func parseSessionCookie(setCookie string) string {
	if setCookie == "" {
		return ""
	}
	// Prefer TP_SESSIONID=
	parts := strings.Split(setCookie, ",")
	for _, part := range parts {
		part = strings.TrimSpace(part)
		pair := strings.Split(part, ";")[0]
		if strings.HasPrefix(strings.ToUpper(pair), "TP_SESSIONID=") {
			return strings.TrimSpace(pair)
		}
	}
	return strings.TrimSpace(strings.Split(setCookie, ";")[0])
}

func tapoResultToPower(res map[string]any) map[string]any {
	if res == nil {
		return nil
	}
	out := map[string]any{}
	if cp, ok := asFloatAny(res["current_power"]); ok {
		if cp > 2000 {
			out["power_w"] = round2(cp / 1000)
		} else {
			out["power_w"] = round2(cp)
		}
	} else if cp, ok := asFloatAny(res["power_mw"]); ok {
		out["power_w"] = round2(cp / 1000)
	}
	if v, ok := asFloatAny(res["voltage_mv"]); ok {
		out["voltage_v"] = round3(v / 1000)
	} else if v, ok := asFloatAny(res["voltage"]); ok {
		out["voltage_v"] = round3(v)
	}
	if v, ok := asFloatAny(res["current_ma"]); ok {
		out["current_a"] = round3(v / 1000)
	} else if v, ok := asFloatAny(res["current"]); ok {
		out["current_a"] = round3(v)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func pkcs7Pad(b []byte, block int) []byte {
	n := block - (len(b) % block)
	if n == 0 {
		n = block
	}
	pad := bytes.Repeat([]byte{byte(n)}, n)
	return append(b, pad...)
}

func pkcs7Unpad(b []byte) ([]byte, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("empty")
	}
	n := int(b[len(b)-1])
	if n == 0 || n > len(b) || n > aes.BlockSize {
		return nil, fmt.Errorf("bad pad")
	}
	for i := 0; i < n; i++ {
		if b[len(b)-1-i] != byte(n) {
			return nil, fmt.Errorf("bad pad bytes")
		}
	}
	return b[:len(b)-n], nil
}

func i32be(v int32) []byte {
	b := make([]byte, 4)
	binary.BigEndian.PutUint32(b, uint32(v))
	return b
}

func sha1Hex(s string) string {
	h := sha1.Sum([]byte(s))
	return hex.EncodeToString(h[:])
}

func newUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func asIntAny(v any) (int, bool) {
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case json.Number:
		i, err := t.Int64()
		return int(i), err == nil
	}
	return 0, false
}

func asFloatAny(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case int:
		return float64(t), true
	}
	return 0, false
}

func asBoolAny(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case float64:
		return t != 0
	}
	return false
}

func round2(v float64) float64 { return float64(int(v*100+0.5)) / 100 }
func round3(v float64) float64 { return float64(int(v*1000+0.5)) / 1000 }
