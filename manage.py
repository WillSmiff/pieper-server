"""Admin CLI for the pager server: first-time setup and emergency access.

Day-to-day management is done in the web backoffice (/admin). Use this CLI to
create the first admin, or to get back in when no admin can sign in.

    python manage.py add-user alice [--admin]  # prompts for a password
    python manage.py make-admin alice          # grant admin (2FA setup is required at next sign-in)
    python manage.py revoke-admin alice
    python manage.py passwd alice              # set a password; ends alice's sessions
    python manage.py reset-2fa alice           # remove 2FA (lost phone); ends sessions
    python manage.py logout alice              # end all of alice's web sessions
    python manage.py del-user alice
    python manage.py rename-user alice alicia  # sessions stay valid; sign in with the new name
    python manage.py wipe-history              # delete all pages (page ids keep counting up)
    python manage.py users
    python manage.py add-device pager-01       # prints the device token once
    python manage.py rotate-device pager-01    # issue a new token (restart the server to drop
                                               # a connection that uses the old token)
    python manage.py del-device pager-01
    python manage.py devices
    python manage.py phone-token               # new token for Asterisk (printed on stdout only)
    python manage.py phone-pin                 # set the PIN callers enter (prompts)
    python manage.py phone-off                 # remove the PIN: phone paging is disabled
    python manage.py phone-status              # PIN/token set? which presets?

On the VPS, always run it as the pager user with PAGER_DATA_DIR=/var/lib/pager.
"""

import argparse
import getpass
import sqlite3
import sys

from pager_server import config, db, phone
from pager_server.security import hash_password, hash_token, new_device_token, valid_name


def ask_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if len(pw) < config.MIN_PASSWORD_LENGTH:
            print(f"Use at least {config.MIN_PASSWORD_LENGTH} characters.")
            continue
        if len(pw) > config.MAX_PASSWORD_LENGTH:
            print(f"Use at most {config.MAX_PASSWORD_LENGTH} characters.")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords do not match.")
            continue
        return pw


def print_token(name: str, token: str) -> None:
    print(f"Device '{name}' token (store it in the firmware; it is not shown again):")
    print(f"\n    {token}\n")


def fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Pager server administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true", help="give the user admin rights")
    for cmd in ("passwd", "del-user", "logout", "make-admin", "revoke-admin", "reset-2fa"):
        sub.add_parser(cmd).add_argument("username")
    ren = sub.add_parser("rename-user")
    ren.add_argument("username")
    ren.add_argument("new_username")
    sub.add_parser("wipe-history")
    sub.add_parser("users")
    for cmd in ("add-device", "rotate-device", "del-device"):
        sub.add_parser(cmd).add_argument("name")
    sub.add_parser("devices")
    for cmd in ("phone-token", "phone-pin", "phone-off", "phone-status"):
        sub.add_parser(cmd)
    args = p.parse_args()

    print(f"Database: {config.DB_PATH}  (set PAGER_DATA_DIR to change)", file=sys.stderr)
    db.init()

    def audit(action: str, target: str, details: str | None = None) -> None:
        db.audit("cli", action, target, details)

    name = getattr(args, "username", None) or getattr(args, "name", None)
    if args.cmd in ("passwd", "del-user", "logout", "make-admin", "revoke-admin", "reset-2fa",
                    "rename-user"):
        user = db.get_user(name)
        if user is None:
            return fail(f"No such user '{name}'.")
        name = user["username"]

    if args.cmd == "add-user":
        if not valid_name(name):
            return fail("Use 1-32 letters, digits, dots, dashes or underscores.")
        if db.get_user(name):
            return fail(f"User '{name}' already exists.")
        db.add_user(name, ask_password(), is_admin=args.admin)
        audit("user_created", name, "admin" if args.admin else "user")
        print(f"Created {'admin' if args.admin else 'user'} '{name}'.")
        if args.admin:
            print("They must set up two-factor authentication at first sign-in.")
    elif args.cmd == "make-admin":
        db.set_admin(name, True)
        audit("admin_granted", name)
        print(f"'{name}' is now an admin. Two-factor setup is required at next sign-in.")
    elif args.cmd == "revoke-admin":
        try:
            db.set_admin(name, False)
        except db.LastAdminError as exc:
            return fail(str(exc))
        audit("admin_revoked", name)
        print(f"'{name}' is no longer an admin.")
    elif args.cmd == "passwd":
        db.set_password(name, ask_password())
        audit("password_reset", name)
        print("Password updated; existing sessions were ended.")
    elif args.cmd == "reset-2fa":
        db.reset_totp(name)
        audit("2fa_reset", name)
        print(f"Two-factor authentication removed for '{name}'; their sessions were ended.")
    elif args.cmd == "logout":
        n = db.delete_user_sessions(name)
        audit("sessions_ended", name, f"{n} session(s)")
        print(f"Ended {n} session(s) for '{name}'.")
    elif args.cmd == "del-user":
        try:
            db.delete_user(name)
        except db.LastAdminError as exc:
            return fail(str(exc))
        audit("user_deleted", name)
        print(f"Deleted user '{name}'.")
    elif args.cmd == "rename-user":
        new = args.new_username
        if not valid_name(new):
            return fail("Use 1-32 letters, digits, dots, dashes or underscores.")
        try:
            db.rename_user(name, new)
        except sqlite3.IntegrityError:
            return fail(f"User '{new}' already exists.")
        audit("user_renamed", name, f"new name: {new}")
        print(f"Renamed '{name}' to '{new}'.")
    elif args.cmd == "wipe-history":
        if input("Delete the entire page history? Type 'yes' to confirm: ").strip() != "yes":
            return fail("Cancelled.")
        n = db.wipe_messages()
        audit("history_wiped", None, f"{n} page(s)")
        print(f"Deleted {n} page(s).")
    elif args.cmd == "users":
        for u in db.list_users():
            role = "admin" if u["is_admin"] else "user"
            tfa = "2FA on" if u["has_2fa"] else "2FA off"
            print(f"{u['username']:<24} {role:<6} {tfa:<8} created {u['created_at']}")
    elif args.cmd == "add-device":
        if not valid_name(name):
            return fail("Use 1-32 letters, digits, dots, dashes or underscores.")
        try:
            print_token(name, db.add_device(name))
        except sqlite3.IntegrityError:
            return fail(f"Device '{name}' already exists.")
        audit("device_created", name)
    elif args.cmd == "rotate-device":
        token = db.rotate_device_token(name)
        if token is None:
            return fail(f"No such device '{name}'.")
        audit("device_token_rotated", name)
        print_token(name, token)
    elif args.cmd == "del-device":
        if not db.delete_device(name):
            return fail(f"No such device '{name}'.")
        audit("device_deleted", name)
        print(f"Deleted device '{name}'.")
    elif args.cmd == "devices":
        for d in db.list_devices():
            print(f"{d['name']:<24} last seen {d['last_seen'] or 'never'}")
    elif args.cmd == "phone-token":
        token = new_device_token()
        db.set_setting(phone.TOKEN_SETTING, hash_token(token))
        audit("phone_token_rotated", None)
        print("New phone service token; the previous one no longer works. Put it in "
              "/etc/asterisk/pager-token (see README).", file=sys.stderr)
        print(token)  # alone on stdout, so it can be piped into the file
    elif args.cmd == "phone-pin":
        while True:
            pin = getpass.getpass("Phone PIN (digits): ").strip()
            if not phone.valid_pin(pin):
                print(phone.pin_rules())
            elif pin != getpass.getpass("Repeat PIN: ").strip():
                print("PINs do not match.")
            else:
                break
        db.set_setting(phone.PIN_SETTING, hash_password(pin))
        audit("phone_pin_changed", None)
        print("Phone PIN set.")
    elif args.cmd == "phone-off":
        db.set_setting(phone.PIN_SETTING, None)
        audit("phone_pin_removed", None, "phone paging disabled")
        print("Phone PIN removed; calls can no longer send pages.")
    elif args.cmd == "phone-status":
        print(f"PIN:   {'set' if db.get_setting(phone.PIN_SETTING) else 'not set (phone paging off)'}")
        print(f"Token: {'set' if db.get_setting(phone.TOKEN_SETTING) else 'not set (run phone-token)'}")
        presets = db.list_presets()
        print("Presets:" if presets else "Presets: none (add them on the admin page)")
        for pr in presets:
            print(f"  {pr['digit']}  {pr['body']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
