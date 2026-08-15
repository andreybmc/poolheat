package tuyalan

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"fmt"
)

func padPKCS7(b []byte, block int) []byte {
	n := block - len(b)%block
	out := make([]byte, len(b)+n)
	copy(out, b)
	for i := len(b); i < len(out); i++ {
		out[i] = byte(n)
	}
	return out
}

func unpadPKCS7(b []byte) ([]byte, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("empty")
	}
	n := int(b[len(b)-1])
	if n < 1 || n > 16 || n > len(b) {
		return nil, fmt.Errorf("bad padding")
	}
	return b[:len(b)-n], nil
}

func encryptECB(key, plain []byte, pad bool) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	if pad {
		plain = padPKCS7(plain, aes.BlockSize)
	}
	if len(plain)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("ecb length %d", len(plain))
	}
	out := make([]byte, len(plain))
	for i := 0; i < len(plain); i += aes.BlockSize {
		block.Encrypt(out[i:i+aes.BlockSize], plain[i:i+aes.BlockSize])
	}
	return out, nil
}

func decryptECBRaw(key, enc []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	if len(enc) == 0 || len(enc)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("ecb cipher length %d", len(enc))
	}
	out := make([]byte, len(enc))
	for i := 0; i < len(enc); i += aes.BlockSize {
		block.Decrypt(out[i:i+aes.BlockSize], enc[i:i+aes.BlockSize])
	}
	return out, nil
}

func decryptECB(key, enc []byte) ([]byte, error) {
	out, err := decryptECBRaw(key, enc)
	if err != nil {
		return nil, err
	}
	return unpadPKCS7(out)
}

func encryptGCM(key, iv, aad, plain []byte) (ct, tag []byte, err error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, nil, err
	}
	if len(iv) != gcm.NonceSize() {
		return nil, nil, fmt.Errorf("gcm iv %d", len(iv))
	}
	sealed := gcm.Seal(nil, iv, plain, aad)
	if len(sealed) < gcm.Overhead() {
		return nil, nil, fmt.Errorf("gcm seal short")
	}
	ct = sealed[:len(sealed)-gcm.Overhead()]
	tag = sealed[len(sealed)-gcm.Overhead():]
	return ct, tag, nil
}

func decryptGCM(key, iv, aad, ct, tag []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return gcm.Open(nil, iv, append(append([]byte{}, ct...), tag...), aad)
}

func hmacSHA256(key, msg []byte) []byte {
	h := hmac.New(sha256.New, key)
	h.Write(msg)
	return h.Sum(nil)
}

func xor16(a, b []byte) []byte {
	out := make([]byte, 16)
	for i := 0; i < 16; i++ {
		out[i] = a[i] ^ b[i]
	}
	return out
}

func randNonce16() []byte {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return b
}

func randIV12() []byte {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return b
}

// sessionKey34 = AES-ECB(localKey, clientNonce XOR deviceNonce)  (no padding)
func sessionKey34(localKey, clientNonce, deviceNonce []byte) ([]byte, error) {
	return encryptECB(localKey, xor16(clientNonce, deviceNonce), false)
}

// sessionKey35 = GCM(localKey, iv=clientNonce[:12], xor).ciphertext (16 bytes)
func sessionKey35(localKey, clientNonce, deviceNonce []byte) ([]byte, error) {
	ct, _, err := encryptGCM(localKey, clientNonce[:12], nil, xor16(clientNonce, deviceNonce))
	if err != nil {
		return nil, err
	}
	if len(ct) != 16 {
		return nil, fmt.Errorf("session key35 len %d", len(ct))
	}
	return ct, nil
}
