#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reqapi.security import hash_password  # noqa: E402
from reqapi.storage import Storage  # noqa: E402


DEFAULT_DB = ROOT / "data" / "reqapi.sqlite3"
MIN_PASSWORD_LENGTH = 12


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset the REQAPI admin password without removing application data."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite database path. Default: {DEFAULT_DB}",
    )
    args = parser.parse_args()

    password = getpass.getpass("New admin password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"The admin password must be at least {MIN_PASSWORD_LENGTH} characters long.",
            file=sys.stderr,
        )
        return 2

    confirmation = getpass.getpass("Confirm admin password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2

    storage = Storage(args.db.resolve())
    admin = storage.configure_single_admin(hash_password(password))
    with storage.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (admin["id"],))

    print(f"Password reset for {admin['username']}. Existing admin sessions were closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
