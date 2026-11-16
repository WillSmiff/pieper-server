# Pager server

A small pager service: users sign in to a website, type a message, and it is
sent to every connected pager device (LilyGo T-Display S3) over a WebSocket.

- **Web UI**: username/password login, a compose box with a character counter, live device
  online/offline status, and recent pages with how many devices received each one.
- **Devices**: each pager has its own secret token and holds one WebSocket connection to the
  server. Pages are pushed to it right away. If a device was offline, it catches up with `sync`.
- **Storage**: SQLite in `data/`. Passwords are hashed with scrypt, and device tokens are stored
  only as SHA-256 hashes.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

Create users and devices:

```bash
python manage.py add-user alice          # prompts for a password (min 8 chars)
python manage.py add-device pager-01     # prints the device token ONCE – put it in the firmware
python manage.py users | devices
python manage.py passwd alice | del-user alice
python manage.py rotate-device pager-01 | del-device pager-01
```

Run:

```bash
uvicorn pager_server.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Run only **one** worker process, because connected devices are
tracked in memory.

To test without hardware, use the simulated pager:

```bash
python tools/fake_device.py ws://localhost:8000/ws/device <token>
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PAGER_DATA_DIR` | `./data` | Where the database and generated secret key live |
| `PAGER_SECRET_KEY` | auto-generated in `data/secret_key` | Signs session cookies |
| `PAGER_COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS |
| `PAGER_MAX_MESSAGE_LENGTH` | `200` | Max characters per page |
| `PAGER_SESSION_MAX_AGE` | `604800` | Login lifetime in seconds |
| `PAGER_LOGIN_MAX_FAILURES` / `PAGER_LOGIN_WINDOW_SECONDS` | `10` / `900` | Login throttling per IP |
| `PAGER_SYNC_LIMIT` | `20` | Max missed pages returned per `sync` |

## Deploying with HTTPS

Put a reverse proxy such as Caddy in front of the server. It handles TLS certificates
automatically and forwards WebSockets without extra configuration:

```
pager.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then set `PAGER_COOKIE_SECURE=1`, and have the devices connect to
`wss://pager.example.com/ws/device`. Without TLS, passwords and device tokens travel in plain text.

## Device protocol

Connect a WebSocket to `/ws/device` and authenticate with either:

- header `Authorization: Bearer <token>` (preferred), or
- query string `/ws/device?token=<token>`

A bad token is rejected during the handshake with HTTP 403. All frames are JSON text.

**Server → device**

```json
{"type": "welcome", "device": "pager-01", "max_length": 200, "server_time": "2026-11-16T11:33:11+00:00"}
{"type": "page", "id": 42, "from": "alice", "text": "Meeting in 5 min\nRoom 2", "ts": "2026-11-16T11:33:11+00:00"}
{"type": "pong", "server_time": "..."}
{"type": "error", "error": "..."}
```

`text` is UTF-8 and may contain `\n`. Other control characters are stripped. Timestamps are in UTC.

**Device → server**

```json
{"type": "ack",  "id": 42}         // confirms the page was shown (drives "received by N/M" in the UI)
{"type": "sync", "since": 41}      // send right after connecting: pages with id > since are re-sent, oldest first
{"type": "ping"}                   // optional app-level keepalive
```

Suggested firmware behaviour (for example with the `arduinoWebSockets` library, using
`beginSSL` for wss):

1. Keep the last page `id` in NVS (`Preferences`).
2. On connect, send `{"type":"sync","since":<last_id>}`.
3. On each `page`, ignore it if `id <= last_id`. Otherwise display it, buzz or vibrate, save the
   id, and send an `ack`.
4. Reconnect with backoff (1 s, 2 s, 4 s … up to 30 s). Enable heartbeat
   (`enableHeartbeat(15000, 3000, 2)`) so dead links are noticed. The server also pings every 20 s.

If the same device connects twice, the server closes the older connection with code 4000.

## HTTP API (for the web UI, needs a session cookie)

- `POST /api/messages` with `{"text": "..."}` returns `{"id": 42, "sent_to": 3}`
- `GET /api/status` returns devices (online, last_seen) and the 25 most recent pages with ack counts
- `GET /healthz` is an unauthenticated liveness check
