"""Runtime configuration, read from environment variables."""

import os
import secrets
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("PAGER_DATA_DIR", Path(__file__).resolve().parent.parent / "data")
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "pager.db"

# Max characters per page. The T-Display S3 screen is 320x170, so keep it short.
MAX_MESSAGE_LENGTH = int(os.environ.get("PAGER_MAX_MESSAGE_LENGTH", "200"))

# Set to 1 when the site is served over HTTPS (recommended), so the session
# cookie is only ever sent over TLS.
COOKIE_SECURE = os.environ.get("PAGER_COOKIE_SECURE", "0") == "1"

# Absolute session lifetime: a login is valid for this long, regardless of activity.
SESSION_MAX_AGE = int(os.environ.get("PAGER_SESSION_MAX_AGE", str(7 * 24 * 3600)))

# Admin sessions are shorter, and sensitive admin actions need a password/2FA
# check within the last REAUTH_SECONDS.
ADMIN_SESSION_MAX_AGE = int(os.environ.get("PAGER_ADMIN_SESSION_MAX_AGE", str(12 * 3600)))
REAUTH_SECONDS = int(os.environ.get("PAGER_REAUTH_SECONDS", "600"))

MIN_PASSWORD_LENGTH = int(os.environ.get("PAGER_MIN_PASSWORD_LENGTH", "10"))
MAX_PASSWORD_LENGTH = 256

# Name shown in authenticator apps.
TOTP_ISSUER = os.environ.get("PAGER_TOTP_ISSUER", "Pager")

# Login throttling within a sliding window: max failed attempts per client IP,
# and per username (protects against attacks spread over many IPs).
LOGIN_MAX_FAILURES = int(os.environ.get("PAGER_LOGIN_MAX_FAILURES", "10"))
LOGIN_MAX_FAILURES_PER_USER = int(os.environ.get("PAGER_LOGIN_MAX_FAILURES_PER_USER", "20"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("PAGER_LOGIN_WINDOW_SECONDS", "900"))

# Pages older than this are deleted (0 = keep forever).
RETENTION_DAYS = int(os.environ.get("PAGER_RETENTION_DAYS", "90"))

# How many missed messages a device can fetch with a "sync" request.
SYNC_LIMIT = int(os.environ.get("PAGER_SYNC_LIMIT", "20"))


def _load_secret_key() -> str:
    env = os.environ.get("PAGER_SECRET_KEY")
    if env:
        return env
    path = DATA_DIR / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


SECRET_KEY = _load_secret_key()
