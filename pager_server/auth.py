"""Authentication and authorization helpers shared by the web app and the admin backoffice.

Access levels, checked server-side on every request:
  - get_ctx:             who is logged in (or None)
  - require_user:        logged in and account setup complete (password changed, 2FA if admin)
  - require_admin:       require_user + admin role
  - require_recent_auth: require_admin + password/2FA confirmed within REAUTH_SECONDS
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import config, db
from .security import totp_match, verify_password

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


@dataclass
class Ctx:
    token: str
    user_id: int
    username: str
    is_admin: bool
    has_2fa: bool
    must_change_password: bool
    auth_at: str | None
    totp_pending: str | None

    @property
    def setup_required(self) -> bool:
        """True until the user has replaced a temporary password and (admins) enabled 2FA."""
        return self.must_change_password or (self.is_admin and not self.has_2fa)

    @property
    def recently_authenticated(self) -> bool:
        if not self.auth_at:
            return False
        age = time.time() - datetime.fromisoformat(self.auth_at).timestamp()
        return age < config.REAUTH_SECONDS


def get_ctx(request: Request) -> Ctx | None:
    sid = request.session.get("sid")
    row = db.get_session(sid) if isinstance(sid, str) else None
    if row is None:
        request.session.pop("sid", None)
        return None
    return Ctx(
        token=sid,
        user_id=row["user_id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        has_2fa=bool(row["has_2fa"]),
        must_change_password=bool(row["must_change_password"]),
        auth_at=row["auth_at"],
        totp_pending=row["totp_pending"],
    )


def require_user(request: Request) -> Ctx:
    ctx = get_ctx(request)
    if ctx is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    if ctx.setup_required:
        raise HTTPException(status_code=403, detail="account_setup_required")
    return ctx


def require_admin(ctx: Ctx = Depends(require_user)) -> Ctx:
    if not ctx.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return ctx


def require_recent_auth(ctx: Ctx = Depends(require_admin)) -> Ctx:
    if not ctx.recently_authenticated:
        raise HTTPException(status_code=403, detail="reauth_required")
    return ctx


def session_max_age(is_admin: bool) -> int:
    return config.ADMIN_SESSION_MAX_AGE if is_admin else config.SESSION_MAX_AGE


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------ throttling

# Failed-attempt timestamps per key ("ip:<addr>", "user:<name>"), in memory.
_failures: dict[str, deque] = {}
_MAX_TRACKED_KEYS = 10_000


def recent_failures(key: str) -> int:
    q = _failures.get(key)
    if not q:
        return 0
    cutoff = time.monotonic() - config.LOGIN_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        del _failures[key]
        return 0
    return len(q)


def record_failure(*keys: str) -> None:
    for key in keys:
        _failures.setdefault(key, deque()).append(time.monotonic())
    while len(_failures) > _MAX_TRACKED_KEYS:  # drop the oldest-tracked keys
        _failures.pop(next(iter(_failures)))


def clear_failures(*keys: str) -> None:
    for key in keys:
        _failures.pop(key, None)


def throttle_keys(request: Request, username: str) -> tuple[str, str]:
    return f"ip:{client_ip(request)}", f"user:{username.lower()}"


def is_throttled(ip_key: str, user_key: str) -> bool:
    return (
        recent_failures(ip_key) >= config.LOGIN_MAX_FAILURES
        or recent_failures(user_key) >= config.LOGIN_MAX_FAILURES_PER_USER
    )


# scrypt uses ~16 MB and tens of ms per check; run it off the event loop and
# limit how many run at once so login floods can't starve page delivery.
_hash_slots = asyncio.Semaphore(2)


async def check_password(password: str, stored_hash: str) -> bool:
    if len(password) > config.MAX_PASSWORD_LENGTH:
        return False
    async with _hash_slots:
        return await run_in_threadpool(verify_password, password, stored_hash)


def verify_totp(user_id: int, code: str) -> bool:
    """Check a 2FA code for a user and mark it used, so it can't be replayed."""
    user = db.get_user_by_id(user_id)
    if not user or not user["totp_secret"]:
        return False
    step = totp_match(user["totp_secret"], code, user["totp_last_step"])
    return step is not None and db.use_totp_step(user_id, step)


def page(request: Request, name: str, ctx: Ctx | None, status_code: int = 200, **extra):
    """Render a template with the standard header context."""
    context = {
        "user": ctx.username if ctx else None,
        "is_admin": bool(ctx and ctx.is_admin and not ctx.setup_required),
        "setup_required": bool(ctx and ctx.setup_required),
        **extra,
    }
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def password_problem(new: str, confirm: str) -> str | None:
    """Validation message for a new password, or None if it is acceptable."""
    if new != confirm:
        return "The new passwords don't match."
    if len(new) < config.MIN_PASSWORD_LENGTH:
        return f"Use at least {config.MIN_PASSWORD_LENGTH} characters."
    if len(new) > config.MAX_PASSWORD_LENGTH:
        return f"Use at most {config.MAX_PASSWORD_LENGTH} characters."
    return None
