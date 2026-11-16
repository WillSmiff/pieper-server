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

SESSION_MAX_AGE = int(os.environ.get("PAGER_SESSION_MAX_AGE", str(7 * 24 * 3600)))

# Login throttling: max failed attempts per client IP within the window.
LOGIN_MAX_FAILURES = int(os.environ.get("PAGER_LOGIN_MAX_FAILURES", "10"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("PAGER_LOGIN_WINDOW_SECONDS", "900"))

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
    path.write_text(key)
    return key


SECRET_KEY = _load_secret_key()
