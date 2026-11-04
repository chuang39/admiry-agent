"""Owner-side tokens at rest (spec 2026-09-29-admiry-channels-design §8: `channel_account.secret_enc`). A token is
sealed with Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`) under a key derived from the box's
AGENT_KEY_ENCRYPTION_KEY, a hex string of at least 32 bytes (`openssl rand -hex 32`): the Fernet key is the
base64 of the SHA-256 of those bytes. The sealed bytes go in the database, the key stays in the box's .env, and
neither the token nor the key is ever logged or put in an error. Rotating the key strands what the old one sealed
(`Undecryptable`); the owner reconnects."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

MIN_KEY_BYTES = 32
BAD_KEY = "the encryption key must be a hex string of at least 32 bytes (openssl rand -hex 32)"


class Undecryptable(ValueError):
    """The bytes were not sealed with this key, or were damaged since."""


def _fernet(key: str) -> Fernet:
    try:
        raw = bytes.fromhex(key)
    except ValueError:
        raise ValueError(BAD_KEY) from None
    if len(raw) < MIN_KEY_BYTES:
        raise ValueError(BAD_KEY)
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))


def encrypt(plaintext: str, key: str) -> bytes:
    """`plaintext` sealed under `key`. ValueError when the key is not a hex string of at least 32 bytes."""
    return _fernet(key).encrypt(plaintext.encode())


def decrypt(blob: bytes, key: str) -> str:
    """What `encrypt` sealed. Undecryptable for a different key or damaged bytes; ValueError for a malformed key."""
    try:
        return _fernet(key).decrypt(bytes(blob)).decode()
    except InvalidToken:
        raise Undecryptable("the stored bytes cannot be opened with this key") from None
