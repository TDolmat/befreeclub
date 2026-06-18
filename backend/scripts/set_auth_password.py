"""Port scripts/set-auth-password.ts. Tworzy/aktualizuje konto logowania do
panelu w admin.users i uniewaznia wszystkie sesje konta.

Exit codes: 0 sukces, 1 runtime error / abort, 2 blad argumentow/walidacji.
"""

import asyncio
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.db import async_session_maker, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

HELP = """
Usage: set-auth-password --email <email> [--password <password>]

Creates or updates a panel-admin login account in auth_accounts.

If --password is omitted, you'll be prompted to enter it interactively
(no echo to terminal, no shell history leak — recommended).

Examples:
  set-auth-password --email tomasz@befreeclub.pl
  set-auth-password --email krystian@befreeclub.pl --password '...'
"""


def print_help() -> None:
    print(HELP)


def parse_args() -> tuple[str, str | None]:
    email: str | None = None
    password: str | None = None
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--email" and i + 1 < len(argv):
            i += 1
            email = argv[i]
        elif a == "--password" and i + 1 < len(argv):
            i += 1
            password = argv[i]
        elif a in ("-h", "--help"):
            print_help()
            sys.exit(0)
        else:
            print(f"Unknown argument: {a}", file=sys.stderr)
            print_help()
            sys.exit(2)
        i += 1
    if not email:
        print("Missing required --email <address>", file=sys.stderr)
        print_help()
        sys.exit(2)
    return email, password


def prompt_password_silently(question: str) -> str:
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    return getpass.getpass(question)


async def main() -> None:
    email_arg, password = parse_args()
    email_norm = email_arg.strip().lower()

    if not EMAIL_RE.match(email_norm):
        print(f'Email looks invalid: "{email_norm}"', file=sys.stderr)
        sys.exit(2)

    if not password:
        password = prompt_password_silently(f"Password for {email_norm}: ")
        if not password:
            print("Empty password — aborting.", file=sys.stderr)
            sys.exit(2)
        confirm = prompt_password_silently("Confirm password:    ")
        if password != confirm:
            print("Passwords do not match — aborting.", file=sys.stderr)
            sys.exit(2)

    if len(password) < 12:
        print(f"Password too short ({len(password)} chars). Minimum 12.", file=sys.stderr)
        sys.exit(2)

    pw_hash = hash_password(password)

    async with async_session_maker() as session:
        await session.execute(
            text("""
                INSERT INTO admin.users (email, password_hash)
                VALUES (:email, :hash)
                ON CONFLICT (email)
                DO UPDATE SET password_hash = :hash, updated_at = now()
            """),
            {"email": email_norm, "hash": pw_hash},
        )
        # Zmiana hasla = wylogowanie wszedzie.
        await session.execute(
            text("""
                DELETE FROM admin.sessions
                WHERE user_id = (SELECT id FROM admin.users WHERE email = :email)
            """),
            {"email": email_norm},
        )
        await session.commit()
    await engine.dispose()

    print(f"✓ Password set for {email_norm}. Existing sessions invalidated.")
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("✗ Failed: aborted", file=sys.stderr)
        sys.exit(1)
    except Exception as err:
        print(f"✗ Failed: {err}", file=sys.stderr)
        sys.exit(1)
