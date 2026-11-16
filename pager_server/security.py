"""Password and device-token hashing (stdlib only)."""

import hashlib
import hmac
import os
import secrets

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


def hash_token(token: str) -> str:
    # Device tokens are long random strings, so a plain SHA-256 is sufficient.
    return hashlib.sha256(token.encode()).hexdigest()
