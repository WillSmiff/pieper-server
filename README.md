# Pager server

A small pager service: users sign in to a website, type a message, and it is
sent to every connected pager device (LilyGo T-Display S3) over a WebSocket.

- **Web UI**: username/password login, a compose box with a character counter, live device
  online/offline status, and recent pages with how many devices received each one.
- **Devices**: each pager has its own secret token and holds one WebSocket connection to the
  server. Pages are pushed to it right away. If a device was offline, it catches up with `sync`.
- **Storage**: SQLite in `data/`. Passwords are hashed with scrypt, and device tokens and login
  session tokens are stored only as SHA-256 hashes.
- **Sessions** are stored server-side and expire after 7 days. Logging out, changing a password
  or deleting a user ends them immediately. Pages older than 90 days are deleted automatically.

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
python manage.py passwd alice | del-user alice   # both also end alice's sessions
python manage.py logout alice                    # end all of alice's web sessions
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
| `PAGER_SESSION_MAX_AGE` | `604800` | Login lifetime in seconds (absolute, not extended by activity) |
| `PAGER_LOGIN_MAX_FAILURES` / `PAGER_LOGIN_WINDOW_SECONDS` | `10` / `900` | Failed logins allowed per IP per window |
| `PAGER_LOGIN_MAX_FAILURES_PER_USER` | `20` | Failed logins allowed per username per window |
| `PAGER_RETENTION_DAYS` | `90` | Delete pages older than this (`0` = keep forever) |
| `PAGER_SYNC_LIMIT` | `20` | Max missed pages returned per `sync` |

## Deploying with HTTPS

Ready-made files for an Ubuntu VPS are in [`deploy/`](deploy/):

- `pager.service`: a sandboxed systemd unit (code in `/opt/pager`, data in `/var/lib/pager`)
- `Caddyfile`: HTTPS, security headers (HSTS, CSP, …), a request size limit, and a `www` redirect
- `pager-backup`: a daily database backup for `/etc/cron.daily`, keeping 14 days

Caddy handles TLS certificates automatically and forwards WebSockets without extra
configuration. Devices connect to `wss://<domain>/ws/device`. Without TLS, passwords and device
tokens travel in plain text.

Deploying an update on the VPS:

```bash
cd /opt/pager && sudo -u pager git pull && sudo -u pager .venv/bin/pip install -r requirements.txt
sudo cp deploy/pager.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart pager
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

When using `manage.py` on the VPS, always set `PAGER_DATA_DIR=/var/lib/pager`. It prints which
database it uses, so a mistake is easy to spot.

## Device protocol

Connect a WebSocket to `/ws/device` and authenticate with the header
`Authorization: Bearer <token>`. Tokens in the URL are not accepted, because URLs end up in logs.
Incoming frames are limited to 64 KB.

A missing or bad token is rejected during the handshake with HTTP 403. All frames are JSON text.

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
- `GET /healthz` is an unauthenticated liveness check that returns `{"ok": true}`
