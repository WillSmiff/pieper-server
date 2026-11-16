"""Tracks connected pager devices and broadcasts messages to them."""

import asyncio
import logging

from fastapi import WebSocket

log = logging.getLogger("pager.hub")

SEND_TIMEOUT = 5.0


class Hub:
    def __init__(self) -> None:
        self._conns: dict[int, WebSocket] = {}  # device_id -> socket
        self._lock = asyncio.Lock()

    async def register(self, device_id: int, ws: WebSocket) -> None:
        async with self._lock:
            old = self._conns.get(device_id)
            self._conns[device_id] = ws
        if old is not None:
            # Same device reconnected before the old socket was noticed dead.
            try:
                await old.close(code=4000, reason="replaced by new connection")
            except Exception:
                pass

    async def unregister(self, device_id: int, ws: WebSocket) -> None:
        async with self._lock:
            if self._conns.get(device_id) is ws:
                del self._conns[device_id]

    def online_ids(self) -> set[int]:
        return set(self._conns)

    async def broadcast(self, payload: dict) -> int:
        """Send payload to every connected device. Returns number of successful sends."""
        async with self._lock:
            targets = list(self._conns.items())

        async def send(device_id: int, ws: WebSocket) -> bool:
            try:
                await asyncio.wait_for(ws.send_json(payload), SEND_TIMEOUT)
                return True
            except Exception as exc:
                log.warning("send to device %s failed: %s", device_id, exc)
                await self.unregister(device_id, ws)
                return False

        results = await asyncio.gather(*(send(d, w) for d, w in targets))
        return sum(results)


hub = Hub()
