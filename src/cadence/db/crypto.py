"""Field-level encryption for direct identifiers.

Database-level encryption protects against a stolen disk. It does not protect
against a leaked backup, an over-broad analytics query, or a support engineer
with read access - all of which are more likely. So the handful of columns that
actually identify a person are sealed separately, with a key held outside the
database.

AES-256-GCM via `cryptography` where it is installed. Where it is not, the
fallback below is authenticated but not AES, and it refuses to pretend
otherwise: `is_production_grade` is False and the API start-up check turns that
into a refusal to boot outside development.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

try:  # pragma: no cover - depends on the environment
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAVE_AES = True
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore[assignment]
    _HAVE_AES = False


class IdentityCipher:
    def __init__(self, key_b64: str | None):
        self.enabled = bool(key_b64)
        self._key = base64.b64decode(key_b64) if key_b64 else b""
        if self.enabled and len(self._key) != 32:
            raise ValueError("CADENCE_ENCRYPTION_KEY must decode to exactly 32 bytes")

    @property
    def is_production_grade(self) -> bool:
        return self.enabled and _HAVE_AES

    def seal(self, payload: dict[str, Any]) -> bytes | None:
        if not payload:
            return None
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        if not self.enabled:
            # No key configured: store plaintext and let the start-up check
            # complain, rather than silently losing the data.
            return b"plain:" + raw
        nonce = os.urandom(12)
        if _HAVE_AES:
            return b"gcm:" + nonce + AESGCM(self._key).encrypt(nonce, raw, None)
        return b"hmac:" + nonce + _xor(self._key, nonce, raw) + _tag(self._key, nonce, raw)

    def open(self, blob: bytes | None) -> dict[str, Any]:
        if not blob:
            return {}
        if blob.startswith(b"plain:"):
            return json.loads(blob[6:])
        if blob.startswith(b"gcm:"):
            nonce, ciphertext = blob[4:16], blob[16:]
            return json.loads(AESGCM(self._key).decrypt(nonce, ciphertext, None))
        if blob.startswith(b"hmac:"):
            body = blob[5:]
            nonce, rest = body[:12], body[12:]
            ciphertext, tag = rest[:-32], rest[-32:]
            plaintext = _xor(self._key, nonce, ciphertext)
            if not hmac.compare_digest(tag, _tag(self._key, nonce, plaintext)):
                raise ValueError("identity blob failed authentication")
            return json.loads(plaintext)
        raise ValueError("unrecognised identity blob format")


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.blake2b(
            nonce + counter.to_bytes(4, "big"), key=key, digest_size=64
        ).digest()
        counter += 1
    return bytes(out[:length])


def _xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data)), strict=True))


def _tag(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    return hmac.new(key, nonce + plaintext, hashlib.sha256).digest()
