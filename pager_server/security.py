"""Password and device-token hashing (stdlib only)."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from urllib.parse import quote

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(digest_hex) // 2,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# Used to keep login timing similar whether or not the username exists.
DUMMY_HASH = hash_password(secrets.token_hex(8))


def new_device_token() -> str:
    return secrets.token_urlsafe(24)


def new_temp_password() -> str:
    """One-time password handed out by an admin; the user must change it at first login."""
    return secrets.token_urlsafe(12)


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def valid_name(name: str) -> bool:
    """Usernames and device names: 1-32 chars of letters, digits, dot, dash, underscore."""
    return bool(_NAME_RE.fullmatch(name))


# ------------------------------------------------------------------ TOTP (RFC 6238)

TOTP_PERIOD = 30


def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def totp_match(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """Return the matching time step (allowing ±1 step of clock drift), or None.

    Steps at or before `last_step` are rejected so a code can't be used twice.
    """
    code = code.replace(" ", "").strip()
    if len(code) != 6 or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // TOTP_PERIOD)
    for step in (current - 1, current, current + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def totp_uri(secret: str, username: str, issuer: str) -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period={TOTP_PERIOD}"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Device tokens are long random strings, so a plain SHA-256 is sufficient.
    return hashlib.sha256(token.encode()).hexdigest()
