"""Simulated pager for testing the server without hardware.

    python tools/fake_device.py ws://localhost:8000/ws/device <token>

Behaves like the real firmware should: connects, asks for missed pages,
prints each page, acks it, and reconnects with backoff if the link drops.
"""

import asyncio
import base64
import json
import sys

import websockets


def ascii_art(image: dict) -> str:
    """Render a 1bpp drawing in the terminal, 2x4 pixels per character."""
    w, h = image["width"], image["height"]
    data = base64.b64decode(image["data"])
    stride = (w + 7) // 8

    def ink(x: int, y: int) -> bool:
        return x < w and y < h and bool(data[y * stride + x // 8] & (0x80 >> (x % 8)))

    rows = []
    for y in range(0, h, 4):
        rows.append("".join(
            "#" if any(ink(x + dx, y + dy) for dx in range(2) for dy in range(4)) else "."
            for x in range(0, w, 2)
        ))
    return "\n".join(rows)


async def run(url: str, token: str) -> None:
    last_id = 0
    backoff = 1
    while True:
        try:
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                backoff = 1
                await ws.send(json.dumps({"type": "sync", "since": last_id}))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "welcome":
                        print(f"[connected as {msg['device']}]")
                    elif msg["type"] == "page":
                        if msg["id"] <= last_id:
                            continue
                        last_id = msg["id"]
                        print(f"\n*** PAGE #{msg['id']} from {msg['from']} at {msg['ts']}")
                        if msg.get("image"):
                            print(ascii_art(msg["image"]))
                        if msg["text"]:
                            print(msg["text"])
                        print()
                        await ws.send(json.dumps({"type": "ack", "id": msg["id"]}))
                    else:
                        print(f"[{msg}]")
        except (OSError, websockets.WebSocketException) as exc:
            print(f"[disconnected: {exc}; retrying in {backoff}s]")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except KeyboardInterrupt:
        pass
