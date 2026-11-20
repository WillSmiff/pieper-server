#!/usr/bin/env python3
"""Asterisk AGI script: passes a phone caller's PIN and preset choice to the pager server.

Called from extensions.conf as AGI(/usr/local/lib/pager/pager-agi.py,check|send|number).
Reads the channel variables PIN, PRESET (send), NUMBER (number) and CALLERID(num) (not
passed as arguments, so the PIN doesn't appear in Asterisk's command logging), POSTs them
to the pager server on 127.0.0.1, and sets PAGER_RESULT to one of:

    pin_ok     (check) PIN is correct
    ok         (send/number) page sent
    bad_pin    wrong PIN
    no_preset  nothing configured on that key
    bad_number the typed number isn't 1-20 digits
    locked     too many wrong PINs recently; phone paging is paused
    disabled   no phone PIN set on the server
    error      anything else (server down, bad token, ...)

Install:
    sudo install -D -m 755 deploy/asterisk/pager-agi.py /usr/local/lib/pager/pager-agi.py
The service token lives in /etc/asterisk/pager-token (root:asterisk, mode 640).
Standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# The environment overrides are for testing; Asterisk normally doesn't set them.
URL = os.environ.get("PAGER_AGI_URL", "http://127.0.0.1:8000/internal/phone/page")
TOKEN_FILE = os.environ.get("PAGER_AGI_TOKEN_FILE", "/etc/asterisk/pager-token")
TIMEOUT = 8  # seconds; the caller hears silence meanwhile


def read_env() -> dict:
    env = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition(": ")
        env[key] = value
    return env


def command(cmd: str) -> str:
    sys.stdout.write(cmd + "\n")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def get_var(expr: str) -> str:
    # Reply looks like: 200 result=1 (value)   or   200 result=0
    reply = command(f"GET FULL VARIABLE {expr}")
    if "result=1" in reply and "(" in reply:
        return reply[reply.index("(") + 1:reply.rindex(")")]
    return ""


def set_result(value: str) -> None:
    command(f'SET VARIABLE PAGER_RESULT "{value}"')


def call_server(payload: dict) -> str:
    try:
        with open(TOKEN_FILE, encoding="ascii") as f:
            token = f.read().strip()
    except OSError:
        return "error"
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp).get("result", "error")
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc).get("error", "error")
        except ValueError:
            return "error"
    except (OSError, ValueError):
        return "error"


def main() -> None:
    read_env()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = {"pin": get_var("${PIN}"), "caller": get_var("${CALLERID(num)}")}
    if mode == "send":
        preset = get_var("${PRESET}")
        if not preset.isdigit():
            set_result("no_preset")
            return
        payload["preset"] = int(preset)
    elif mode == "number":
        number = get_var("${NUMBER}")
        if not number.isdigit():
            set_result("bad_number")
            return
        payload["number"] = number
    elif mode != "check":
        set_result("error")
        return
    result = call_server(payload)
    known = {"pin_ok", "ok", "bad_pin", "no_preset", "bad_number", "locked", "disabled"}
    set_result(result if result in known else "error")


if __name__ == "__main__":
    main()
