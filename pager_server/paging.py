"""Creating and delivering pages; shared by the web UI and the phone line."""

import base64
import logging
import re

from . import config, db
from .hub import hub

log = logging.getLogger("pager")

_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def drawing_json(image: bytes) -> dict:
    return {
        "width": config.DRAWING_WIDTH,
        "height": config.DRAWING_HEIGHT,
        "format": "1bpp",
        "data": base64.b64encode(image).decode(),
    }


def page_payload(msg: dict) -> dict:
    payload = {
        "type": "page",
        "id": msg["id"],
        "from": msg["sender"],
        "text": msg["body"],  # for a drawing: the optional caption
        "ts": msg["created_at"],
        "kind": "drawing" if msg.get("image") else "text",
    }
    if msg.get("image"):
        payload["image"] = drawing_json(msg["image"])
    return payload


async def send(sender: str, text: str, image: bytes | None = None) -> tuple[int, int]:
    """Store a page and push it to all connected devices; returns (page id, devices reached)."""
    msg = db.add_message(sender, text, image)
    delivered = await hub.broadcast(page_payload(msg))
    log.info("%s #%s from %s sent to %d device(s)",
             "drawing" if image else "page", msg["id"], sender, delivered)
    return msg["id"], delivered
