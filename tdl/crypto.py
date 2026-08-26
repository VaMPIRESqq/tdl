"""TIDAL encrypted-stream decryption helpers."""

from __future__ import annotations

import base64

from Crypto.Cipher import AES

MASTER_KEY = base64.b64decode("UIlTTEMmmLfGowo/UC60x2H45W6MdGgTRfo/umg4754=")


def decrypt_security_token(security_token: str) -> tuple[bytes, bytes]:
    token = base64.b64decode(security_token)
    if len(token) < 32:
        raise ValueError("Security token too short")
    iv, ciphertext = token[:16], token[16:]
    ciphertext += b"\0" * ((16 - len(ciphertext) % 16) % 16)
    decrypted = AES.new(MASTER_KEY, AES.MODE_CBC, iv).decrypt(ciphertext)
    if len(decrypted) < 24:
        raise ValueError("Decrypted token too short")
    return decrypted[:16], decrypted[16:24]


def decrypt_file(data: bytes, key: bytes, nonce: bytes) -> bytes:
    # Rust's Ctr128BE uses the nonce as the first 8 bytes of a 16-byte counter.
    initial_counter = int.from_bytes(nonce + b"\0" * 8, "big")
    cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=initial_counter)
    return cipher.decrypt(data)
