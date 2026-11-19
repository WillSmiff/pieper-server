"""Admin backoffice: manage users, devices and sessions, and read the audit log.

Every route in this router requires an admin (router-level dependency, so a new
route can't accidentally be left open). Every change additionally requires a
recent password/2FA confirmation (require_recent_auth).
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import db
from .auth import Ctx, client_ip, page, require_admin, require_recent_auth
from .hub import hub
from .security import new_temp_password, valid_name

router = APIRouter(dependencies=[Depends(require_admin)])

NAME_RULES = "Use 1-32 letters, digits, dots, dashes or underscores."


class NewUser(BaseModel):
    username: str
    is_admin: bool = False


class RoleIn(BaseModel):
    is_admin: bool


class RenameIn(BaseModel):
    new_username: str


class NewDevice(BaseModel):
    name: str


def _target_user(ctx: Ctx, username: str):
    if username.lower() == ctx.username.lower():
        raise HTTPException(status_code=400, detail="Use the Account page to change your own account")
    user = db.get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


def _audit(request: Request, ctx: Ctx, action: str, target: str, details: str | None = None):
    db.audit(ctx.username, action, target, details, client_ip(request))


# ------------------------------------------------------------------- page

@router.get("/admin")
async def admin_page(request: Request, ctx: Ctx = Depends(require_admin)):
    return page(request, "admin.html", ctx)


# ------------------------------------------------------------------ users

@router.get("/api/admin/users")
async def list_users(ctx: Ctx = Depends(require_admin)):
    return {"me": ctx.username, "users": [
        {
            "username": u["username"],
            "is_admin": bool(u["is_admin"]),
            "has_2fa": bool(u["has_2fa"]),
            "must_change_password": bool(u["must_change_password"]),
            "sessions": u["sessions"],
            "created_at": u["created_at"],
        }
        for u in db.list_users()
    ]}


@router.post("/api/admin/users")
async def create_user(body: NewUser, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    username = body.username.strip()
    if not valid_name(username):
        raise HTTPException(status_code=400, detail=NAME_RULES)
    temp = new_temp_password()
    try:
        db.add_user(username, temp, is_admin=body.is_admin, must_change=True)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That username already exists")
    _audit(request, ctx, "user_created", username, "admin" if body.is_admin else "user")
    return {"username": username, "temp_password": temp}


@router.post("/api/admin/users/{username}/reset-password")
async def reset_password(username: str, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    user = _target_user(ctx, username)
    temp = new_temp_password()
    db.set_password(user["username"], temp, must_change=True)  # also ends their sessions
    _audit(request, ctx, "password_reset", user["username"])
    return {"username": user["username"], "temp_password": temp}


@router.post("/api/admin/users/{username}/role")
async def set_role(username: str, body: RoleIn, request: Request,
                   ctx: Ctx = Depends(require_recent_auth)):
    user = _target_user(ctx, username)
    try:
        db.set_admin(user["username"], body.is_admin)  # also ends their sessions
    except db.LastAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, ctx, "admin_granted" if body.is_admin else "admin_revoked", user["username"])
    return {"ok": True}


@router.post("/api/admin/users/{username}/rename")
async def rename_user(username: str, body: RenameIn, request: Request,
                      ctx: Ctx = Depends(require_recent_auth)):
    # Renaming yourself is allowed: it changes nothing about your rights or sessions.
    user = db.get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    new = body.new_username.strip()
    if not valid_name(new):
        raise HTTPException(status_code=400, detail=NAME_RULES)
    if new == user["username"]:
        raise HTTPException(status_code=400, detail="That is already the username")
    try:
        db.rename_user(user["username"], new)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That username already exists")
    _audit(request, ctx, "user_renamed", user["username"], f"new name: {new}")
    return {"username": new}


@router.post("/api/admin/users/{username}/reset-2fa")
async def reset_2fa(username: str, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    user = _target_user(ctx, username)
    db.reset_totp(user["username"])
    _audit(request, ctx, "2fa_reset", user["username"])
    return {"ok": True}


@router.post("/api/admin/users/{username}/logout")
async def logout_user(username: str, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    user = _target_user(ctx, username)
    n = db.delete_user_sessions(user["username"])
    _audit(request, ctx, "sessions_ended", user["username"], f"{n} session(s)")
    return {"ended": n}


@router.delete("/api/admin/users/{username}")
async def delete_user(username: str, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    user = _target_user(ctx, username)
    try:
        db.delete_user(user["username"])
    except db.LastAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, ctx, "user_deleted", user["username"])
    return {"ok": True}


# ---------------------------------------------------------------- devices

@router.get("/api/admin/devices")
async def list_devices(ctx: Ctx = Depends(require_admin)):
    online = hub.online_ids()
    return {"devices": [
        {"name": d["name"], "online": d["id"] in online,
         "created_at": d["created_at"], "last_seen": d["last_seen"]}
        for d in db.list_devices()
    ]}


@router.post("/api/admin/devices")
async def create_device(body: NewDevice, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    name = body.name.strip()
    if not valid_name(name):
        raise HTTPException(status_code=400, detail=NAME_RULES)
    try:
        token = db.add_device(name)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That device name already exists")
    _audit(request, ctx, "device_created", name)
    return {"name": name, "token": token}


def _device_id(name: str) -> int:
    match = next((d for d in db.list_devices() if d["name"].lower() == name.lower()), None)
    if match is None:
        raise HTTPException(status_code=404, detail="No such device")
    return match["id"]


@router.post("/api/admin/devices/{name}/rotate")
async def rotate_device(name: str, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    device_id = _device_id(name)
    token = db.rotate_device_token(name)
    await hub.kick(device_id)  # the old token must stop working right away
    _audit(request, ctx, "device_token_rotated", name)
    return {"name": name, "token": token}


@router.delete("/api/admin/devices/{name}")
async def delete_device(name: str, request: Request, ctx: Ctx = Depends(require_recent_auth)):
    device_id = _device_id(name)
    db.delete_device(name)
    await hub.kick(device_id)
    _audit(request, ctx, "device_deleted", name)
    return {"ok": True}


# ------------------------------------------------------- history and audit

@router.get("/api/admin/messages")
async def message_history(limit: int = 200, ctx: Ctx = Depends(require_admin)):
    return {"messages": [dict(m) for m in db.recent_messages(max(1, min(limit, 500)))]}


@router.delete("/api/admin/messages")
async def wipe_history(request: Request, ctx: Ctx = Depends(require_recent_auth)):
    n = db.wipe_messages()
    _audit(request, ctx, "history_wiped", None, f"{n} page(s)")
    return {"deleted": n}


@router.get("/api/admin/audit")
async def audit_log(limit: int = 200, ctx: Ctx = Depends(require_admin)):
    return {"entries": [dict(e) for e in db.recent_audit(max(1, min(limit, 1000)))]}
