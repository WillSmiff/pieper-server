"""Pager server: web UI for sending pages + WebSocket endpoint for pager devices."""

import asyncio
import json
import logging
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import config, db
from .hub import hub
from .security import DUMMY_HASH, verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pager")

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE / "templates")


async def housekeeping() -> None:
    """Hourly: drop expired sessions and pages older than the retention period."""
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


# ------------------------------------------------------------------ helpers

def current_user(request: Request) -> str | None:
    """The logged-in user, based on a server-side session (revocable)."""
    sid = request.session.get("sid")
    user = db.session_user(sid) if isinstance(sid, str) else None
    if user is None and request.session:
        request.session.clear()
    return user


def require_user(request: Request) -> str:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Failed-login timestamps per key ("ip:<addr>" or "user:<name>"), in memory.
_failures: dict[str, deque] = {}
_MAX_TRACKED_KEYS = 10_000

# scrypt uses ~16 MB and tens of ms per check; run it off the event loop and
# limit how many run at once so login floods can't starve page delivery.
_hash_slots = asyncio.Semaphore(2)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _recent_failures(key: str) -> int:
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


def _record_failure(key: str) -> None:
    _failures.setdefault(key, deque()).append(time.monotonic())
    while len(_failures) > _MAX_TRACKED_KEYS:  # drop the oldest-tracked keys
        _failures.pop(next(iter(_failures)))


async def check_password(password: str, stored_hash: str) -> bool:
    async with _hash_slots:
        return await run_in_threadpool(verify_password, password, stored_hash)


def page_payload(msg: dict) -> dict:
    return {
        "type": "page",
        "id": msg["id"],
        "from": msg["sender"],
        "text": msg["body"],
        "ts": msg["created_at"],
    }


# ---------------------------------------------------------------- web pages

@app.get("/")
async def index(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "index.html", {"user": user, "max_len": config.MAX_MESSAGE_LENGTH}
    )


@app.get("/login")
async def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = client_ip(request)
    username = username.strip()[:64]
    ip_key, user_key = f"ip:{ip}", f"user:{username.lower()}"
    if (
        _recent_failures(ip_key) >= config.LOGIN_MAX_FAILURES
        or _recent_failures(user_key) >= config.LOGIN_MAX_FAILURES_PER_USER
    ):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many failed attempts. Try again later."}, status_code=429,
        )
    user = db.get_user(username)
    ok = await check_password(password, user["password_hash"] if user else DUMMY_HASH)
    if not (user and ok):
        _record_failure(ip_key)
        _record_failure(user_key)
        log.info("failed login from %s", ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password."}, status_code=401
        )
    _failures.pop(ip_key, None)
    _failures.pop(user_key, None)
    old_sid = request.session.get("sid")
    if isinstance(old_sid, str):
        db.delete_session(old_sid)
    request.session.clear()
    request.session["sid"] = db.create_session(user["id"])
    log.info("user %s logged in from %s", user["username"], ip)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    sid = request.session.get("sid")
    if isinstance(sid, str):
        db.delete_session(sid)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- JSON API

class PageIn(BaseModel):
    text: str


@app.post("/api/messages")
async def send_page(body: PageIn, request: Request):
    # JSON body + SameSite=strict cookie means cross-site forms can't forge this.
    user = require_user(request)
    text = clean_text(body.text)
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(text) > config.MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Message exceeds {config.MAX_MESSAGE_LENGTH} characters"
        )
    msg = db.add_message(user, text)
    delivered = await hub.broadcast(page_payload(msg))
    log.info("page #%s from %s sent to %d device(s)", msg["id"], user, delivered)
    return {"id": msg["id"], "sent_to": delivered}


@app.get("/api/status")
async def status(request: Request):
    require_user(request)
    online = hub.online_ids()
    devices = [
        {"name": d["name"], "online": d["id"] in online, "last_seen": d["last_seen"]}
        for d in db.list_devices()
    ]
    messages = [dict(m) for m in db.recent_messages(25)]
    return {"devices": devices, "messages": messages, "device_count": len(devices)}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


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
    except Exception as exc:  # network errors etc.
        log.warning("device %s socket error: %s", name, exc)
    finally:
        await hub.unregister(device_id, ws)
        db.touch_device(device_id)
        log.info("device %s disconnected", name)
