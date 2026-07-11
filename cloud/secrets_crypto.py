#!/usr/bin/env python3
"""
secrets_crypto.py -- Transparent field-level encryption for tenant secrets.

Secrets like the SMTP password and the Entra client secret live inside the
tenant_settings JSON blob. This module encrypts designated secret fields at
rest so the database never stores them in plaintext.

Design:
- A Fernet key is derived from the SETTINGS_ENCRYPTION_KEY environment variable.
  The value can be a raw urlsafe-base64 Fernet key, or any passphrase (we derive
  a 32-byte key from it via SHA-256). This keeps setup forgiving.
- Encrypted values are stored with an "enc:v1:" sentinel prefix. That lets us
  tell ciphertext from legacy plaintext, so reads stay backward compatible and
  existing plaintext gets encrypted the next time a tenant saves settings.
- If no key is configured, encryption is a no-op (values stay plaintext, exactly
  as before). A one-time warning is logged so this is visible in dev but never
  crashes a local run. In production, set the env var.

Public API:
    encrypt_secret(plaintext)  -> str   (ciphertext with prefix, or plaintext if no key)
    decrypt_secret(value)      -> str   (plaintext; passes through unencrypted values)
    is_encrypted(value)        -> bool
    encryption_enabled()       -> bool
"""

import os
import hashlib
import base64
import logging

log = logging.getLogger(__name__)

_PREFIX = "enc:v1:"

try:
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - only if the lib is missing
    Fernet = None
    InvalidToken = Exception
    _CRYPTO_AVAILABLE = False

_fernet = None
_warned = False


def _derive_key(raw: str) -> bytes:
    """Accept a valid Fernet key as-is, else derive one from an arbitrary string.

    A Fernet key is 32 url-safe base64 bytes. If the provided value already
    decodes to exactly 32 bytes we use it directly; otherwise we hash it with
    SHA-256 (which yields 32 bytes) and base64-encode that.
    """
    raw = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw)
        if len(decoded) == 32:
            return raw.encode("utf-8")
    except Exception:
        pass
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet():
    """Return a cached Fernet instance, or None when encryption is disabled."""
    global _fernet, _warned
    if _fernet is not None:
        return _fernet
    raw = os.getenv("SETTINGS_ENCRYPTION_KEY", "").strip()
    if not raw or not _CRYPTO_AVAILABLE:
        if not _warned:
            if not _CRYPTO_AVAILABLE:
                log.warning("secrets_crypto: cryptography not installed; secrets stored in plaintext.")
            else:
                log.warning("secrets_crypto: SETTINGS_ENCRYPTION_KEY not set; tenant secrets stored "
                            "in plaintext. Set it in production to encrypt secrets at rest.")
            _warned = True
        return None
    _fernet = Fernet(_derive_key(raw))
    return _fernet


def encryption_enabled() -> bool:
    return _get_fernet() is not None


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_secret(plaintext) -> str:
    """Encrypt a secret for storage. Returns plaintext unchanged when encryption
    is disabled or the value is empty/already encrypted."""
    if not isinstance(plaintext, str) or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    f = _get_fernet()
    if f is None:
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_secret(value):
    """Decrypt a stored secret. Passes through values that are not encrypted
    (legacy plaintext). If a value is marked encrypted but cannot be decrypted
    (wrong/rotated key), returns an empty string rather than leaking ciphertext
    or crashing, and logs the failure."""
    if not is_encrypted(value):
        return value
    f = _get_fernet()
    if f is None:
        log.error("secrets_crypto: found an encrypted secret but no key is configured to decrypt it.")
        return ""
    token = value[len(_PREFIX):].encode("ascii")
    try:
        return f.decrypt(token).decode("utf-8")
    except InvalidToken:
        log.error("secrets_crypto: failed to decrypt a secret (key mismatch or corruption).")
        return ""
