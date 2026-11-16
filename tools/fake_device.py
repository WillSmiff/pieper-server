"""Simulated pager for testing the server without hardware.

    python tools/fake_device.py ws://localhost:8000/ws/device <token>

Behaves like the real firmware should: connects, asks for missed pages,
prints each page, acks it, and reconnects with backoff if the link drops.
"""

import asyncio
import json
import sys

import websockets


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
                        print(f"\n*** PAGE #{msg['id']} from {msg['from']} at {msg['ts']}\n{msg['text']}\n")
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
