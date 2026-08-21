"""Offline administrator password recovery for a local VerifEye installation."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verifeye.auth import AuthError, AuthStore  # noqa: E402
from verifeye.storage import EmbeddingStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the sole VerifEye administrator password.")
    parser.add_argument("--database", default="backend/data/verifeye.db", help="Path to verifeye.db")
    args = parser.parse_args()
    database = Path(args.database)
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    password = getpass("New administrator password: ")
    if password != getpass("Confirm password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 2
    try:
        with EmbeddingStore(database) as store:
            user = AuthStore(store._connection).reset_admin_password(password)
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Password reset for {user.email}; all administrator sessions were revoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
