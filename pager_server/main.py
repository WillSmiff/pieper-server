"""Pager server: web UI for sending pages + WebSocket endpoint for pager devices."""

import asyncio
import base64
import binascii
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import admin, config, db, paging, phone
from .paging import clean_text, drawing_json, page_payload
from .auth import (
    Ctx, check_password, clear_failures, client_ip, get_ctx, is_throttled, page,
    password_problem, record_failure, require_user, session_max_age, throttle_keys, verify_totp,
)
from .hub import hub
from .security import DUMMY_HASH, new_totp_secret, totp_match, totp_uri

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pager")

BASE = Path(__file__).resolve().parent
PENDING_2FA_SECONDS = 300


async def housekeeping() -> None:
    """Hourly: drop expired sessions, old audit entries and pages past the retention period."""
    while True:
        try:
            sessions, pages = await run_in_threadpool(db.purge_expired, config.RETENTION_DAYS)
            if sessions or pages:
                log.info("housekeeping: removed %d expired session(s), %d old page(s)", sessions, pages)
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if db.admin_count() == 0:
        log.warning("no admin account yet: create one with `python manage.py make-admin <user>`")
    task = asyncio.create_task(housekeeping())
    yield
    task.cancel()


app = FastAPI(
    title="Pager server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="pager_session",
    max_age=config.SESSION_MAX_AGE,
    same_site="strict",
    https_only=config.COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(admin.router)
app.include_router(phone.router)


def _same_site_request(request: Request) -> bool:
    # Modern browsers always send Sec-Fetch-Site; trust it first. (The Origin header can
    # legitimately be "null" on same-site form posts, depending on the Referrer-Policy.)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    # Older browsers / non-browser clients: if an Origin is given, it must be this host.
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser cross-site request (those always carry one of the headers)
    return urlsplit(origin).netloc == request.headers.get("host", "")


@app.middleware("http")
async def origin_check_and_no_store(request: Request, call_next):
    # Defence in depth against cross-site requests, on top of the SameSite=strict cookie:
    # state-changing requests must come from this site's own pages.
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_site_request(request):
        return JSONResponse({"error": "Cross-site request blocked"}, status_code=403)
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        # Pages can contain one-time secrets (tokens, temporary passwords): never cache.
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/internal/")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    if exc.detail == "account_setup_required":
        return RedirectResponse("/account", status_code=303)
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


# ------------------------------------------------------------------ helpers

def decode_drawing(data: str) -> bytes:
    """Validate a base64 1-bit drawing from the web UI; returns the raw bitmap."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Drawing is not valid base64")
    if len(raw) != config.DRAWING_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Drawing must be {config.DRAWING_WIDTH}x{config.DRAWING_HEIGHT} pixels, 1 bit each",
        )
    if not any(raw):
        raise HTTPException(status_code=400, detail="Drawing is empty")
    return raw


def start_session(request: Request, user) -> None:
    old_sid = request.session.get("sid")
    if isinstance(old_sid, str):
        db.delete_session(old_sid)
    request.session.clear()
    request.session["sid"] = db.create_session(user["id"], session_max_age(bool(user["is_admin"])))
    db.audit(user["username"], "login", ip=client_ip(request))
    log.info("user %s logged in from %s", user["username"], client_ip(request))


def pending_2fa_user(request: Request):
    pending = request.session.get("pending_2fa")
    if not isinstance(pending, dict) or time.time() - pending.get("ts", 0) > PENDING_2FA_SECONDS:
        return None
    return db.get_user_by_id(pending.get("uid"))


def qr_svg(data: str) -> Markup:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return Markup(img.to_string(encoding="unicode"))  # generated by us, contains no user input


def too_many(request: Request, template: str, ctx: Ctx | None = None, **extra):
    return page(request, template, ctx, status_code=429,
                error="Too many failed attempts. Try again later.", **extra)


# ---------------------------------------------------------------- web pages

@app.get("/")
async def index(request: Request):
    ctx = get_ctx(request)
    if ctx is None:
        return RedirectResponse("/login", status_code=303)
    if ctx.setup_required:
        return RedirectResponse("/account", status_code=303)
    return page(
        request, "index.html", ctx,
        max_len=config.MAX_MESSAGE_LENGTH,
        drawing_width=config.DRAWING_WIDTH,
        drawing_height=config.DRAWING_HEIGHT,
    )


@app.get("/login")
async def login_form(request: Request):
    if get_ctx(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", None, error=None)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()[:64]
    ip_key, user_key = throttle_keys(request, username)
    if is_throttled(ip_key, user_key):
        return too_many(request, "login.html")
    user = db.get_user(username)
    ok = await check_password(password, user["password_hash"] if user else DUMMY_HASH)
    if not (user and ok):
        record_failure(ip_key, user_key)
        log.info("failed login from %s", client_ip(request))
        return page(request, "login.html", None, status_code=401,
                    error="Invalid username or password.")
    if user["totp_secret"]:
        # Password OK; the session only starts after the second factor.
        request.session.clear()
        request.session["pending_2fa"] = {"uid": user["id"], "ts": time.time()}
        return RedirectResponse("/login/2fa", status_code=303)
    clear_failures(ip_key, user_key)
    start_session(request, user)
    return RedirectResponse("/", status_code=303)


@app.get("/login/2fa")
async def login_2fa_form(request: Request):
    if pending_2fa_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return page(request, "login_2fa.html", None, error=None)


@app.post("/login/2fa")
async def login_2fa(request: Request, code: str = Form(...)):
    user = pending_2fa_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    ip_key, user_key = throttle_keys(request, user["username"])
    if is_throttled(ip_key, user_key):
        return too_many(request, "login_2fa.html")
    if not verify_totp(user["id"], code):
        record_failure(ip_key, user_key)
        db.audit(user["username"], "2fa_failed", ip=client_ip(request))
        return page(request, "login_2fa.html", None, status_code=401,
                    error="That code is not valid. Wait for a new code and try again.")
    clear_failures(ip_key, user_key)
    start_session(request, user)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    sid = request.session.get("sid")
    if isinstance(sid, str):
        db.delete_session(sid)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------- account pages

DONE_MESSAGES = {
    "password": "Your password was changed. Other sessions were signed out.",
    "2fa": "Two-factor authentication is now enabled.",
    "2fa_off": "Two-factor authentication was turned off.",
}


def account_page(request: Request, ctx: Ctx, status_code: int = 200, **extra):
    return page(request, "account.html", ctx, status_code=status_code,
                must_change=ctx.must_change_password, has_2fa=ctx.has_2fa,
                admin_account=ctx.is_admin, min_len=config.MIN_PASSWORD_LENGTH, **extra)


@app.get("/account")
async def account(request: Request, done: str | None = None):
    ctx = get_ctx(request)
    if ctx is None:
        return RedirectResponse("/login", status_code=303)
    return account_page(request, ctx, message=DONE_MESSAGES.get(done or ""))


@app.post("/account/password")
async def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                          confirm: str = Form(...)):
    ctx = get_ctx(request)
    if ctx is None:
        return RedirectResponse("/login", status_code=303)
    ip_key, user_key = throttle_keys(request, ctx.username)
    if is_throttled(ip_key, user_key):
        return account_page(request, ctx, 429, pw_error="Too many failed attempts. Try again later.")
    user = db.get_user_by_id(ctx.user_id)
    if not await check_password(current, user["password_hash"]):
        record_failure(ip_key, user_key)
        return account_page(request, ctx, 400, pw_error="Your current password is incorrect.")
    problem = password_problem(new, confirm)
    if problem is None and new == current:
        problem = "Choose a password different from the current one."
    if problem:
        return account_page(request, ctx, 400, pw_error=problem)
    db.set_password(ctx.username, new, must_change=False, keep_session=ctx.token)
    db.touch_auth(ctx.token)
    db.audit(ctx.username, "password_changed", ip=client_ip(request))
    return RedirectResponse("/account?done=password", status_code=303)


@app.get("/account/2fa")
async def setup_2fa_form(request: Request):
    ctx = get_ctx(request)
    if ctx is None:
        return RedirectResponse("/login", status_code=303)
    if ctx.has_2fa or ctx.must_change_password:
        return RedirectResponse("/account", status_code=303)
    secret = new_totp_secret()
    db.set_totp_pending(ctx.token, secret)
    return setup_2fa_page(request, ctx, secret)


def setup_2fa_page(request: Request, ctx: Ctx, secret: str, status_code: int = 200, error=None):
    uri = totp_uri(secret, ctx.username, config.TOTP_ISSUER)
    grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    return page(request, "account_2fa.html", ctx, status_code=status_code,
                qr=qr_svg(uri), secret=grouped, error=error)


@app.post("/account/2fa")
async def setup_2fa(request: Request, code: str = Form(...)):
    ctx = get_ctx(request)
    if ctx is None:
        return RedirectResponse("/login", status_code=303)
    if ctx.has_2fa or not ctx.totp_pending:
        return RedirectResponse("/account", status_code=303)
    ip_key, user_key = throttle_keys(request, ctx.username)
    if is_throttled(ip_key, user_key):
        return setup_2fa_page(request, ctx, ctx.totp_pending, 429,
                              "Too many failed attempts. Try again later.")
    step = totp_match(ctx.totp_pending, code, None)
    if step is None:
        record_failure(ip_key, user_key)
        return setup_2fa_page(request, ctx, ctx.totp_pending, 400,
                              "That code doesn't match. Check the time on your phone and try again.")
    db.set_totp(ctx.user_id, ctx.totp_pending, used_step=step)
    db.set_totp_pending(ctx.token, None)
    db.touch_auth(ctx.token)
    db.audit(ctx.username, "2fa_enabled", ip=client_ip(request))
    return RedirectResponse("/account?done=2fa", status_code=303)


@app.post("/account/2fa/disable")
async def disable_2fa(request: Request, password: str = Form(...), code: str = Form(...)):
    ctx = get_ctx(request)
    if ctx is None:
        return RedirectResponse("/login", status_code=303)
    if ctx.is_admin:
        return account_page(request, ctx, 403, tfa_error="Admins must keep two-factor authentication on.")
    if not ctx.has_2fa:
        return RedirectResponse("/account", status_code=303)
    ip_key, user_key = throttle_keys(request, ctx.username)
    if is_throttled(ip_key, user_key):
        return account_page(request, ctx, 429, tfa_error="Too many failed attempts. Try again later.")
    user = db.get_user_by_id(ctx.user_id)
    if not (await check_password(password, user["password_hash"]) and verify_totp(ctx.user_id, code)):
        record_failure(ip_key, user_key)
        return account_page(request, ctx, 400, tfa_error="Password or code is incorrect.")
    db.set_totp(ctx.user_id, None)
    db.audit(ctx.username, "2fa_disabled", ip=client_ip(request))
    return RedirectResponse("/account?done=2fa_off", status_code=303)


# ---------------------------------------------------------------- JSON API

class PageIn(BaseModel):
    text: str = ""
    drawing: str | None = None  # base64, see config.DRAWING_*


class ReauthIn(BaseModel):
    password: str
    code: str = ""


@app.post("/api/messages")
async def send_page(body: PageIn, ctx: Ctx = Depends(require_user)):
    text = clean_text(body.text)
    image = decode_drawing(body.drawing) if body.drawing else None
    if not text and image is None:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(text) > config.MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Message exceeds {config.MAX_MESSAGE_LENGTH} characters"
        )
    page_id, delivered = await paging.send(ctx.username, text, image)
    return {"id": page_id, "sent_to": delivered}


@app.get("/api/status")
async def status(ctx: Ctx = Depends(require_user)):
    online = hub.online_ids()
    devices = [
        {"name": d["name"], "online": d["id"] in online, "last_seen": d["last_seen"]}
        for d in db.list_devices()
    ]
    messages = [dict(m) for m in db.recent_messages(25)]
    return {"devices": devices, "messages": messages, "device_count": len(devices),
            "drawing": {"width": config.DRAWING_WIDTH, "height": config.DRAWING_HEIGHT}}


@app.get("/api/messages/{message_id}/image")
async def page_image(message_id: int, ctx: Ctx = Depends(require_user)):
    image = db.message_image(message_id)
    if image is None:
        raise HTTPException(status_code=404, detail="No drawing for this page")
    return drawing_json(image)


@app.post("/api/reauth")
async def reauth(body: ReauthIn, request: Request, ctx: Ctx = Depends(require_user)):
    """Confirm identity again before sensitive (admin) actions."""
    ip_key, user_key = throttle_keys(request, ctx.username)
    if is_throttled(ip_key, user_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")
    user = db.get_user_by_id(ctx.user_id)
    ok = await check_password(body.password, user["password_hash"])
    if ok and ctx.has_2fa:
        ok = verify_totp(ctx.user_id, body.code)
    if not ok:
        record_failure(ip_key, user_key)
        raise HTTPException(status_code=400, detail="Password or code is incorrect")
    db.touch_auth(ctx.token)
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------- device socket

def _device_token(ws: WebSocket) -> str | None:
    # Header only: tokens in the URL would end up in access logs.
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


@app.websocket("/ws/device")
async def device_socket(ws: WebSocket):
    token = _device_token(ws)
    device = db.device_by_token(token) if token else None
    if device is None:
        await ws.close(code=1008)  # rejected during handshake -> HTTP 403
        return

    device_id, name = device["id"], device["name"]
    await ws.accept()
    await hub.register(device_id, ws)
    db.touch_device(device_id)
    log.info("device %s connected from %s", name, ws.client.host if ws.client else "?")

    try:
        await ws.send_json({
            "type": "welcome",
            "device": name,
            "max_length": config.MAX_MESSAGE_LENGTH,
            "drawing": {"width": config.DRAWING_WIDTH, "height": config.DRAWING_HEIGHT, "format": "1bpp"},
            "server_time": db.now(),
        })
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                kind = msg.get("type")
            except (ValueError, AttributeError):
                await ws.send_json({"type": "error", "error": "invalid JSON"})
                continue

            db.touch_device(device_id)
            if kind == "ack":
                try:
                    db.record_ack(int(msg["id"]), device_id)
                except (KeyError, TypeError, ValueError):
                    await ws.send_json({"type": "error", "error": "ack needs integer id"})
            elif kind == "sync":
                try:
                    since = int(msg.get("since", 0))
                except (TypeError, ValueError):
                    since = 0
                for row in db.messages_since(since, config.SYNC_LIMIT):
                    await ws.send_json(page_payload(dict(row)))
            elif kind == "ping":
                await ws.send_json({"type": "pong", "server_time": db.now()})
            else:
                await ws.send_json({"type": "error", "error": f"unknown type {kind!r}"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # network errors, device deleted, etc.
        log.warning("device %s socket error: %s", name, exc)
    finally:
        await hub.unregister(device_id, ws)
        db.touch_device(device_id)
        log.info("device %s disconnected", name)
