"""Crypto helpers shared by API V2 and V3."""

from __future__ import annotations

import base64
import binascii
import hashlib

try:
    from Crypto.Cipher import AES  # type: ignore
except ImportError:  # Entware often ships pycryptodomex as Cryptodome
    from Cryptodome.Cipher import AES  # type: ignore

try:
    from passlib.hash import md5_crypt
except ImportError:  # pragma: no cover
    md5_crypt = None  # type: ignore


def _md5_crypt_hash_fallback(word: str, salt: str) -> str:
    """Minimal $1$ MD5-crypt when passlib is unavailable (rare on Entware)."""
    import struct

    bare = salt
    if salt.startswith("$1$"):
        parts = salt.split("$")
        bare = parts[2] if len(parts) > 2 else salt
    # OpenSSL passwd -1 compatible via hashlib only is non-trivial;
    # require passlib for writes that need get_token.
    raise ImportError(
        "passlib is required for Whatsminer API auth (opkg/pip install passlib)"
    )


def md5_crypt_hash(word: str, salt: str) -> str:
    """OpenSSL ``passwd -1 -salt <salt>`` style; return only the hash body."""
    bare = salt
    if salt.startswith("$1$"):
        parts = salt.split("$")
        bare = parts[2]
    if md5_crypt is None:
        return _md5_crypt_hash_fallback(word, bare)
    full = md5_crypt.using(salt=bare).hash(word)
    # format: $1$salt$hash
    return full.split("$")[3]


def pad16_null(data: str | bytes) -> bytes:
    """Pad with NULs until length is a multiple of 16 (Whatsminer V2)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    while len(data) % 16 != 0:
        data += b"\0"
    return data


def pad_pkcs7(data: str | bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    pad = 16 - (len(data) % 16)
    return data + bytes([pad] * pad)


def unpad_null(data: bytes) -> bytes:
    return data.rstrip(b"\0")


def unpad_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    return data.rstrip(b"\0")


def aes256_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).encrypt(plaintext)


def aes256_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).decrypt(ciphertext)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").replace("\n", "")


def b64d(data: str) -> bytes:
    return base64.b64decode(data)


def sha256_digest(data: str | bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).digest()


def sha256_hex_key(text: str) -> bytes:
    """V2 aeskey: unhexlify(sha256(key_body).hexdigest())."""
    return binascii.unhexlify(hashlib.sha256(text.encode("utf-8")).hexdigest())
