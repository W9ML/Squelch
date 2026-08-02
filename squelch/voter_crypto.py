"""AES-128-CTR decryption for encrypted voter SSE streams.

Some voter dashboards encrypt each SSE `voter` event so the raw API
returns ciphertext rather than client/RSSI data. Per that protocol each
event's `data:` line is:

    <base64 iv>.<base64 ciphertext>

decrypted with AES-128-CTR using a shared 16-byte key and that message's
16 random iv bytes as the initial counter block (the whole 16-byte block
is the big-endian counter, NIST SP 800-38A). Every message carries its
own iv and is independently decryptable — no cross-message state.

`error` events on such streams are sent as plain (unencrypted) JSON.

Squelch runs server-side, so it holds the raw key directly (config
[voter] key) — there's no need for the browser-side XOR-pad obfuscation
the dashboard uses to avoid shipping the key in its JS bundle.
"""

from __future__ import annotations

import base64

try:
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False


def load_key(hexkey: str) -> bytes:
    """Parse a 32-hex-character key into 16 bytes."""
    key = bytes.fromhex(hexkey.strip())
    if len(key) != 16:
        raise ValueError("voter key must be 16 bytes (32 hex characters)")
    return key


def decrypt_message(data_field: str, key: bytes) -> str:
    """Decrypt one `<b64 iv>.<b64 ciphertext>` payload to a JSON string."""
    if not AVAILABLE:
        raise RuntimeError("the 'cryptography' package is required to "
                           "decrypt the voter stream")
    iv_b64, sep, ct_b64 = data_field.partition(".")
    if not sep or not ct_b64:
        raise ValueError("not an encrypted voter payload")
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    if len(iv) != 16:
        raise ValueError(f"bad iv length {len(iv)} (expected 16)")
    dec = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return (dec.update(ct) + dec.finalize()).decode("utf-8")


def encrypt_message(plaintext: str, key: bytes, iv: bytes | None = None) -> str:
    """Inverse of decrypt_message — used by the tests and the reference
    client in tools/."""
    if not AVAILABLE:
        raise RuntimeError("the 'cryptography' package is required")
    import os
    iv = iv or os.urandom(16)
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ct = enc.update(plaintext.encode("utf-8")) + enc.finalize()
    return (base64.b64encode(iv).decode() + "."
            + base64.b64encode(ct).decode())
