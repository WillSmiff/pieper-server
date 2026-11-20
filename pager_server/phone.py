"""Sending preset pages by phone.

Asterisk (on the same VPS) answers calls from the SIP trunk, asks for the
phone PIN and a preset digit, and calls POST /internal/phone/page through the
AGI script in deploy/asterisk/. Instead of a preset, a caller can press * and type
digits to send those digits as the page ("numeric page"). That endpoint is protected by:

  - a service token (Authorization: Bearer ...), only known to Asterisk;
  - loopback only: requests that came through Caddy are refused (Caddy also
    blocks /internal/* itself);
  - the phone PIN, with a lockout after PHONE_MAX_FAILURES wrong attempts.
"""

import hmac
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import config, db, paging
from .auth import check_password, clear_failures, recent_failures, record_failure
from .security import hash_token

router = APIRouter()

PIN_SETTING = "phone_pin_hash"
TOKEN_SETTING = "phone_token_hash"
THROTTLE_KEY = "phone"
SENDER = "phone line"  # contains a space, so it can never be mistaken for a username
PIN_RE = re.compile(r"^[0-9]{%d,12}$" % config.PHONE_PIN_MIN_LENGTH)
NUMBER_RE = re.compile(r"^[0-9]{1,%d}$" % config.PHONE_NUMBER_MAX_LENGTH)


def valid_pin(pin: str) -> bool:
    return bool(PIN_RE.fullmatch(pin))


def pin_rules() -> str:
    return f"Use {config.PHONE_PIN_MIN_LENGTH} to 12 digits."


def enabled() -> bool:
    return db.get_setting(PIN_SETTING) is not None and db.get_setting(TOKEN_SETTING) is not None


class PhoneIn(BaseModel):
    pin: str
    preset: int | None = None  # send this preset page...
    number: str | None = None  # ...or a numeric page with these digits; omit both to only check the PIN
    caller: str = ""


def _check_local_service(request: Request) -> None:
    # Only Asterisk on this machine may call this. Requests proxied by Caddy also come from
    # 127.0.0.1, but always carry X-Forwarded-For.
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1") or "x-forwarded-for" in request.headers:
        raise HTTPException(status_code=404, detail="Not Found")
    expected = db.get_setting(TOKEN_SETTING)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not expected or not token or not hmac.compare_digest(hash_token(token), expected):
        raise HTTPException(status_code=401, detail="bad_token")


@router.post("/internal/phone/page")
async def phone_page(body: PhoneIn, request: Request):
    _check_local_service(request)
    caller = re.sub(r"[^0-9+]", "", body.caller)[:32] or "unknown"

    if recent_failures(THROTTLE_KEY) >= config.PHONE_MAX_FAILURES:
        return _result(429, "locked")
    pin_hash = db.get_setting(PIN_SETTING)
    if pin_hash is None:
        return _result(503, "disabled")
    if not (valid_pin(body.pin) and await check_password(body.pin, pin_hash)):
        record_failure(THROTTLE_KEY)
        db.audit(SENDER, "phone_pin_failed", None, f"caller {caller}")
        return _result(403, "bad_pin")

    if body.number is not None:
        if body.preset is not None or not NUMBER_RE.fullmatch(body.number):
            return _result(400, "bad_number")
        clear_failures(THROTTLE_KEY)
        page_id, delivered = await paging.send(SENDER, body.number)
        db.audit(SENDER, "phone_number_sent", body.number, f"caller {caller}, page #{page_id}")
        return {"result": "ok", "id": page_id, "sent_to": delivered}

    if body.preset is None:
        return _result(200, "pin_ok")
    preset = db.get_preset(body.preset) if 1 <= body.preset <= 9 else None
    if preset is None:
        return _result(404, "no_preset")

    clear_failures(THROTTLE_KEY)
    page_id, delivered = await paging.send(SENDER, preset["body"])
    db.audit(SENDER, "phone_page_sent", f"preset {body.preset}", f"caller {caller}, page #{page_id}")
    return {"result": "ok", "id": page_id, "sent_to": delivered}


def _result(status: int, result: str):
    if status == 200:
        return {"result": result}
    raise HTTPException(status_code=status, detail=result)
