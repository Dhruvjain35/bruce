"""Encryption for stored provider credentials (OAuth refresh tokens).

WHY THIS EXISTS: a Google refresh token is a long-lived bearer credential for a student's real
calendar. Postgres-at-rest encryption protects against a stolen disk; it does nothing against a
database dump, a backup copied somewhere careless, an over-broad SELECT, or a log line. So refresh
tokens are encrypted by the application before they ever reach a column, with a key that lives
outside the database entirely (BRUCE_ENCRYPTION_KEY). Compromising the DB alone is then not enough
to act as the student.

Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`, already a dependency via pyjwt[crypto]).
Authenticated: tampered ciphertext raises rather than decrypting to garbage.

RULES:
  * Never log, print, return, or prompt with plaintext OR ciphertext.
  * Never derive the key from something guessable. It is a real random key, injected as config.
  * Fail loudly when the key is missing or malformed — a service that silently stores plaintext
    because a key was absent is worse than one that refuses to start.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "BRUCE_ENCRYPTION_KEY"


class EncryptionUnavailable(RuntimeError):
    """No usable encryption key. Callers MUST NOT fall back to storing plaintext."""


class DecryptionFailed(RuntimeError):
    """Ciphertext was tampered with, or the key changed/rotated. Never returns partial plaintext."""


def generate_key() -> str:
    """A fresh urlsafe base64 key. For operators: `python -c "from bruce_engine.crypto import
    generate_key; print(generate_key())"` — then set BRUCE_ENCRYPTION_KEY and never commit it."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    raw = os.environ.get(ENV_KEY)
    if not raw:
        raise EncryptionUnavailable(
            f"{ENV_KEY} is not set — refusing to handle provider credentials. Generate one with "
            "bruce_engine.crypto.generate_key() and inject it as configuration."
        )
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception as exc:
        raise EncryptionUnavailable(f"{ENV_KEY} is not a valid Fernet key ({type(exc).__name__})") from exc


def is_configured() -> bool:
    """True iff a usable key is present. For readiness reporting — never for deciding whether to
    silently store plaintext."""
    try:
        _fernet()
        return True
    except EncryptionUnavailable:
        return False


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Raises rather than ever returning the plaintext."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a stored secret. Raises DecryptionFailed on tamper/wrong key — never guesses."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionFailed(
            "stored credential could not be decrypted — it was tampered with, or "
            f"{ENV_KEY} has changed since it was written"
        ) from exc
