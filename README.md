# Pieper - Pager server
<p align="center">
  <img src="assets/img/logo.svg" alt="a pager displaying a pieper"/>
</p>
A small pager service: users sign in to a website, type a message, and it is
sent to every connected pager device over a WebSocket.

- **Web UI**: username/password login, a compose box with a character counter, live device
  online/offline status, and recent pages with how many devices received each one.
- **Drawings**: the **Drawing** tab has a 320×170 black-and-white drawing pad (pen, eraser, brush
  sizes, undo). A drawing is sent as a 1-bit bitmap, with an optional caption.
- **Devices**: each pager has its own secret token and holds one WebSocket connection to the
  server. Pages are pushed to it right away. If a device was offline, it catches up with `sync`.
- **Storage**: SQLite in `data/`. Passwords are hashed with scrypt, and device tokens and login
  session tokens are stored only as SHA-256 hashes.
- **Sessions** are stored server-side and expire after 7 days (12 hours for admins). Logging out,
  changing a password or deleting a user ends them immediately. Pages older than 90 days are
  deleted automatically.
- **Accounts**: users can change their own password and turn on two-factor authentication (TOTP,
  any authenticator app) on the Account page.
- **Admin backoffice** (`/admin`): manage users, devices and sessions, see the page history and an
  audit log of all admin actions. See [Admin backoffice](#admin-backoffice).
- **Phone line** (optional): call a phone number, enter a PIN and press a key to send one of nine
  preset pages, or type digits to send a numeric page. Runs through Asterisk and a SIP trunk on the same VPS. See
  [Phone line](#phone-line-asterisk--sip-trunk).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

Create the first admin. After that, everything else can be done in the web backoffice:

```bash
python manage.py add-user alice --admin  # prompts for a password (min 10 chars)
```

At first sign-in, alice has to set up two-factor authentication before she can do anything else.

`manage.py` remains available for emergencies, such as when no admin can sign in:

```bash
python manage.py make-admin alice | revoke-admin alice
python manage.py passwd alice | reset-2fa alice | logout alice | del-user alice
python manage.py rename-user alice alicia | wipe-history
python manage.py add-device pager-01 | rotate-device pager-01 | del-device pager-01
python manage.py users | devices
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
| `PAGER_ADMIN_SESSION_MAX_AGE` | `43200` | Login lifetime for admins |
| `PAGER_REAUTH_SECONDS` | `600` | Admin changes need a password + 2FA check this recent |
| `PAGER_MIN_PASSWORD_LENGTH` | `10` | Minimum password length |
| `PAGER_TOTP_ISSUER` | `Pager` | Name shown in authenticator apps |
| `PAGER_LOGIN_MAX_FAILURES` / `PAGER_LOGIN_WINDOW_SECONDS` | `10` / `900` | Failed logins allowed per IP per window |
| `PAGER_LOGIN_MAX_FAILURES_PER_USER` | `20` | Failed logins allowed per username per window |
| `PAGER_RETENTION_DAYS` | `90` | Delete pages older than this (`0` = keep forever) |
| `PAGER_SYNC_LIMIT` | `20` | Max missed pages returned per `sync` |

## Admin backoffice

Admins see an **Admin** link in the header. The backoffice covers what used to need SSH:

| Area | Actions |
|---|---|
| Users | Add a user (they get a one-time temporary password), rename, reset a forgotten password to a new temporary one, make or remove admin, reset 2FA (lost phone), sign a user out everywhere, delete |
| Devices | Add a device or issue a new token (both shown once), delete. Revoked devices are disconnected immediately |
| Audit log | Who did what and when: logins, failed 2FA codes, password and 2FA changes, and all admin actions |
| Page history | The last 200 pages with delivery counts; wipe the whole history. Page numbers keep counting up afterwards, so pagers don't mistake new pages for ones they've seen |

Security rules:

- Every `/admin` and `/api/admin/*` route requires an admin account; others get a 403.
- Admin accounts must use two-factor authentication. Until it is set up, an admin can only reach
  the Account page.
- Every change in the backoffice needs a password + 2FA confirmation from the last 10 minutes. The
  page asks for it when needed.
- You can't change your own account from the backoffice (use the Account page), and the last
  admin can't be demoted or deleted.
- Temporary passwords and device tokens are shown once and never cached by the browser.
- Requests that change something are rejected if they come from another website (an `Origin`
  check on top of the `SameSite=strict` cookie).

Server operations (deploys, restarts, logs, backups) deliberately stay on SSH. The web app runs
without any system privileges.

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

After upgrading to the version with the admin backoffice, make your own account an admin once:

```bash
cd /opt/pager && sudo -u pager PAGER_DATA_DIR=/var/lib/pager .venv/bin/python manage.py make-admin <you>
```

When using `manage.py` on the VPS, always set `PAGER_DATA_DIR=/var/lib/pager`. It prints which
database it uses, so a mistake is easy to spot.

## Phone line (Asterisk + SIP trunk)

Callers can send one of up to nine **preset pages** by phone: they call the number, enter the
phone PIN followed by `#`, press the preset key, hear it read back, and press `1` to send.
Instead of a preset key, they can press `*`, type up to 20 digits and end with `#` to send a
**numeric page**: just those digits, for example a number to call back. Pages arrive as sent by
"phone line". Presets and the PIN are managed on the admin page (**Phone line**
section). Every phone page, and every wrong PIN, is in the audit log with the caller's number.

How it fits together: the SIP provider sends calls to Asterisk on the VPS. The dialplan
([`deploy/asterisk/extensions.conf`](deploy/asterisk/extensions.conf)) asks for the PIN and
preset, and the AGI script ([`pager-agi.py`](deploy/asterisk/pager-agi.py)) passes them to the
pager server at `http://127.0.0.1:8000/internal/phone/page`. That endpoint:

- only accepts requests made directly on the VPS (Caddy also refuses `/internal/*` from outside);
- needs a service token that only Asterisk has (`manage.py phone-token`);
- checks the PIN (6–12 digits, stored as a scrypt hash). After 5 wrong PINs within 15 minutes,
  phone paging pauses until that window has passed.

Nothing in the dialplan can dial out, so the trunk can't be used for toll fraud. Also set a
low credit limit, or turn off outgoing calls, in your provider's portal.

### Setting it up

1. **Provider.** Get an inbound number with a SIP account from a provider that supports
   registration. Note the SIP server, username and password, and the provider's signalling and
   media (RTP) IP addresses. Ask them if they're not listed.
2. **Install Asterisk** (from Ubuntu's `universe` repository) and the English prompts:
   ```bash
   sudo apt install asterisk asterisk-core-sounds-en
   ```
3. **Configuration.** Install the files, then fill in the four placeholders (`SIP_SERVER`,
   `SIP_USERNAME`, `SIP_PASSWORD`, `PROVIDER_IP`) in the *installed* `pjsip.conf`. That way the
   password never ends up in the git checkout.
   ```bash
   cd /opt/pager
   for f in pjsip extensions rtp modules; do
     sudo install -m 640 -o root -g asterisk deploy/asterisk/$f.conf /etc/asterisk/$f.conf
   done
   sudo install -D -m 755 deploy/asterisk/pager-agi.py /usr/local/lib/pager/pager-agi.py
   sudo nano /etc/asterisk/pjsip.conf
   ```
   When updating later, don't reinstall `pjsip.conf` over your filled-in copy.
4. **Service token** for Asterisk, written straight into a file that only root and Asterisk can
   read:
   ```bash
   sudo -u pager PAGER_DATA_DIR=/var/lib/pager .venv/bin/python manage.py phone-token \
     | sudo install -m 640 -o root -g asterisk /dev/stdin /etc/asterisk/pager-token
   ```
5. **Firewall:** open SIP and RTP to the provider only. Use the provider's real addresses; media
   often comes from different IPs than signalling.
   ```bash
   sudo ufw allow from <provider-signalling-ip-or-subnet> to any port 5060 proto udp
   sudo ufw allow from <provider-media-ip-or-subnet> to any port 10000:10099 proto udp
   ```
6. **Start** it, and check that the registration works and nothing else is listening:
   ```bash
   sudo systemctl restart asterisk
   sudo asterisk -rx "pjsip show registrations"     # should say "Registered"
   sudo ss -lntup | grep asterisk                   # expect only UDP 5060 (plus RTP during calls)
   ```
7. **Presets and PIN:** on the admin page, add presets under **Phone line** and set the PIN. The
   section shows "On" when both the token and the PIN are set. Then call the number to test.

After changing `extensions.conf` later, run `sudo asterisk -rx "dialplan reload"`. Use
`sudo asterisk -rvvv` to watch calls live. The verbose console shows the digits callers type,
so don't leave a verbose console log running unattended.

`manage.py` also has `phone-pin`, `phone-off` (removes the PIN and turns phone paging off),
`phone-status` and `phone-token` (a new token; the old one stops working).

## Device protocol

Connect a WebSocket to `/ws/device` and authenticate with the header
`Authorization: Bearer <token>`. Tokens in the URL are not accepted, because URLs end up in logs.
Incoming frames are limited to 64 KB.

A missing or bad token is rejected during the handshake with HTTP 403. All frames are JSON text.

**Server → device**

```json
{"type": "welcome", "device": "pager-01", "max_length": 200,
 "drawing": {"width": 320, "height": 170, "format": "1bpp"}, "server_time": "2026-11-16T11:33:11+00:00"}
{"type": "page", "id": 42, "kind": "text", "from": "alice", "text": "Meeting in 5 min\nRoom 2", "ts": "2026-11-16T11:33:11+00:00"}
{"type": "page", "id": 43, "kind": "drawing", "from": "alice", "text": "optional caption", "ts": "...",
 "image": {"width": 320, "height": 170, "format": "1bpp", "data": "<base64, 6800 bytes decoded>"}}
{"type": "pong", "server_time": "..."}
{"type": "error", "error": "..."}
```

`text` is UTF-8 and may contain `\n`. Other control characters are stripped. Timestamps are in UTC.
For a drawing, `text` is the caption and may be empty.

**Drawings** are 1 bit per pixel, 320×170, rows top to bottom. Each row is packed most significant
bit first (the leftmost pixel is bit 7 of the first byte) and padded to whole bytes (40 bytes per
row). A set bit is ink. This is exactly the format of Adafruit-GFX / TFT_eSPI `drawBitmap`, so on
the T-Display S3 used for prototyping (landscape, `setRotation(1)`), after base64-decoding `image.data`:

```cpp
tft.fillScreen(TFT_WHITE);
tft.drawBitmap(0, 0, bitmap, 320, 170, TFT_BLACK);   // or swap colours for white-on-black
```

A drawing page is about 9 KB of JSON, so give the WebSocket client a receive buffer of at least
16 KB. A `sync` can send up to 20 pages back to back.

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

- `POST /api/messages` with `{"text": "..."}` returns `{"id": 42, "sent_to": 3}`. For a drawing,
  send `{"text": "<optional caption>", "drawing": "<base64 bitmap>"}` (format as above; an empty
  drawing is rejected).
- `GET /api/status` returns devices (online, last_seen) and the 25 most recent pages with ack
  counts. Drawing pages have `has_image: true`.
- `GET /api/messages/{id}/image` returns a page's drawing as `{"width", "height", "format", "data"}`
- `GET /healthz` is an unauthenticated liveness check that returns `{"ok": true}`
