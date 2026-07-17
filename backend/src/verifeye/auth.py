"""Local account and opaque session-token storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import sqlite3


PBKDF2_ITERATIONS = 600_000


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str


class AuthError(ValueError):
    """Raised when credentials or account data are invalid."""


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )


class AuthStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_user(self, email: str, display_name: str, password: str) -> User:
        email = normalize_email(email)
        display_name = display_name.strip()
        if "@" not in email or len(email) > 254:
            raise AuthError("Enter a valid email address.")
        if not display_name or len(display_name) > 100:
            raise AuthError("Enter your name.")
        if len(password) < 8:
            raise AuthError("Password must be at least 8 characters.")
        salt = secrets.token_bytes(16)
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """INSERT INTO users(email, display_name, password_hash, password_salt)
                       VALUES (?, ?, ?, ?)""",
                    (email, display_name, hash_password(password, salt), salt),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("An account with this email already exists.") from exc
        return User(int(cursor.lastrowid), email, display_name)

    def authenticate(self, email: str, password: str) -> User:
        row = self.connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (normalize_email(email),),
        ).fetchone()
        if row is None or not hmac.compare_digest(
            row["password_hash"], hash_password(password, row["password_salt"])
        ):
            raise AuthError("Email or password is incorrect.")
        return User(row["id"], row["email"], row["display_name"])

    def create_session(self, user_id: int, lifetime_hours: int = 24) -> str:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=lifetime_hours)
        with self.connection:
            self.connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (self._token_hash(token), user_id, expires.isoformat()),
            )
        return token

    def user_for_session(self, token: str) -> User | None:
        row = self.connection.execute(
            """SELECT u.id, u.email, u.display_name, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ?""",
            (self._token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            self.delete_session(token)
            return None
        return User(row["id"], row["email"], row["display_name"])

    def delete_session(self, token: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(token),)
            )

    @staticmethod
    def _token_hash(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
