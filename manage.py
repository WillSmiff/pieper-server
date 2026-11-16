"""Admin CLI for the pager server.

    python manage.py add-user alice
    python manage.py passwd alice
    python manage.py del-user alice
    python manage.py users
    python manage.py add-device pager-01      # prints the device token once
    python manage.py rotate-device pager-01   # issue a new token
    python manage.py del-device pager-01
    python manage.py devices
"""

import argparse
import getpass
import sqlite3
import sys

from pager_server import db


def ask_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if len(pw) < 8:
            print("Use at least 8 characters.")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords do not match.")
            continue
        return pw


def print_token(name: str, token: str) -> None:
    print(f"Device '{name}' token (store it in the firmware; it is not shown again):")
    print(f"\n    {token}\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Pager server administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("add-user", "passwd", "del-user"):
        sub.add_parser(cmd).add_argument("username")
    sub.add_parser("users")
    for cmd in ("add-device", "rotate-device", "del-device"):
        sub.add_parser(cmd).add_argument("name")
    sub.add_parser("devices")
    args = p.parse_args()

    db.init()

    if args.cmd == "add-user":
        if db.get_user(args.username):
            print(f"User '{args.username}' already exists.", file=sys.stderr)
            return 1
        db.add_user(args.username, ask_password())
        print(f"Created user '{args.username}'.")
    elif args.cmd == "passwd":
        if not db.get_user(args.username):
            print(f"No such user '{args.username}'.", file=sys.stderr)
            return 1
        db.set_password(args.username, ask_password())
        print("Password updated.")
    elif args.cmd == "del-user":
        if not db.delete_user(args.username):
            print(f"No such user '{args.username}'.", file=sys.stderr)
            return 1
        print(f"Deleted user '{args.username}'.")
    elif args.cmd == "users":
        for u in db.list_users():
            print(f"{u['username']:<24} created {u['created_at']}")
    elif args.cmd == "add-device":
        try:
            print_token(args.name, db.add_device(args.name))
        except sqlite3.IntegrityError:
            print(f"Device '{args.name}' already exists.", file=sys.stderr)
            return 1
    elif args.cmd == "rotate-device":
        token = db.rotate_device_token(args.name)
        if token is None:
            print(f"No such device '{args.name}'.", file=sys.stderr)
            return 1
        print_token(args.name, token)
    elif args.cmd == "del-device":
        if not db.delete_device(args.name):
            print(f"No such device '{args.name}'.", file=sys.stderr)
            return 1
        print(f"Deleted device '{args.name}'.")
    elif args.cmd == "devices":
        for d in db.list_devices():
            print(f"{d['name']:<24} last seen {d['last_seen'] or 'never'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
