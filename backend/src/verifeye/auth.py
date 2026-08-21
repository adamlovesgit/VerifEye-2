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
    role: str


class AuthError(ValueError):
    """Raised when credentials or account data are invalid."""


class SetupComplete(AuthError):
    """Raised when public administrator registration is already closed."""


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )


class AuthStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def _validated_credentials(email: str, display_name: str, password: str) -> tuple[str, str]:
        email = normalize_email(email)
        display_name = display_name.strip()
        if "@" not in email or len(email) > 254:
            raise AuthError("Enter a valid email address.")
        if not display_name or len(display_name) > 100:
            raise AuthError("Enter your name.")
        if len(password) < 8:
            raise AuthError("Password must be at least 8 characters.")
        return email, display_name

    def create_user(
        self, email: str, display_name: str, password: str, role: str = "admin"
    ) -> User:
        email, display_name = self._validated_credentials(email, display_name, password)
        if role not in {"admin", "guest"}:
            raise AuthError("Account role is invalid.")
        salt = secrets.token_bytes(16)
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """INSERT INTO users(email, display_name, password_hash, password_salt, role)
                       VALUES (?, ?, ?, ?, ?)""",
                    (email, display_name, hash_password(password, salt), salt, role),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("An account with this email or role already exists.") from exc
        return User(int(cursor.lastrowid), email, display_name, role)

    def setup_required(self) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM users WHERE role = 'admin'"
        ).fetchone() is None

    def create_initial_admin(self, email: str, display_name: str, password: str) -> User:
        email, display_name = self._validated_credentials(email, display_name, password)
        salt = secrets.token_bytes(16)
        try:
            with self.connection:
                if not self.setup_required():
                    raise SetupComplete("Administrator setup is already complete.")
                cursor = self.connection.execute(
                    """INSERT INTO users(email, display_name, password_hash, password_salt, role)
                       VALUES (?, ?, ?, ?, 'admin')""",
                    (email, display_name, hash_password(password, salt), salt),
                )
        except SetupComplete:
            raise
        except sqlite3.IntegrityError as exc:
            # The unique role index is the final guard against concurrent setup.
            if not self.setup_required():
                raise SetupComplete("Administrator setup is already complete.") from exc
            raise AuthError("An account with this email already exists.") from exc
        return User(int(cursor.lastrowid), email, display_name, "admin")

    def guest(self) -> User | None:
        row = self.connection.execute(
            "SELECT id, email, display_name, role FROM users WHERE role = 'guest'"
        ).fetchone()
        return self._user(row) if row else None

    def replace_guest(self, email: str, display_name: str, password: str) -> User:
        email, display_name = self._validated_credentials(email, display_name, password)
        salt = secrets.token_bytes(16)
        try:
            with self.connection:
                row = self.connection.execute(
                    "SELECT id FROM users WHERE role = 'guest'"
                ).fetchone()
                if row:
                    guest_id = int(row["id"])
                    self.connection.execute(
                        """UPDATE users SET email=?, display_name=?, password_hash=?, password_salt=?
                           WHERE id=?""",
                        (email, display_name, hash_password(password, salt), salt, guest_id),
                    )
                    self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (guest_id,))
                    self.connection.execute("DELETE FROM preview_grants WHERE user_id = ?", (guest_id,))
                else:
                    cursor = self.connection.execute(
                        """INSERT INTO users(email, display_name, password_hash, password_salt, role)
                           VALUES (?, ?, ?, ?, 'guest')""",
                        (email, display_name, hash_password(password, salt), salt),
                    )
                    guest_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise AuthError("That email address is already in use.") from exc
        return User(guest_id, email, display_name, "guest")

    def revoke_guest(self) -> bool:
        with self.connection:
            cursor = self.connection.execute("DELETE FROM users WHERE role = 'guest'")
        return cursor.rowcount > 0

    def reset_admin_password(self, password: str) -> User:
        row = self.connection.execute(
            "SELECT id, email, display_name, role FROM users WHERE role = 'admin'"
        ).fetchone()
        if row is None:
            raise AuthError("No administrator account exists.")
        self._validated_credentials(row["email"], row["display_name"], password)
        salt = secrets.token_bytes(16)
        with self.connection:
            self.connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
                (hash_password(password, salt), salt, row["id"]),
            )
            self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            self.connection.execute("DELETE FROM preview_grants WHERE user_id = ?", (row["id"],))
        return self._user(row)

    def authenticate(self, email: str, password: str) -> User:
        row = self.connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (normalize_email(email),),
        ).fetchone()
        if row is None or not hmac.compare_digest(
            row["password_hash"], hash_password(password, row["password_salt"])
        ):
            raise AuthError("Email or password is incorrect.")
        return self._user(row)

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
            """SELECT u.id, u.email, u.display_name, u.role, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ?""",
            (self._token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            self.delete_session(token)
            return None
        return self._user(row)

    def delete_session(self, token: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(token),)
            )

    def create_preview_grant(self, user_id: int, camera_id: int, lifetime_seconds: int = 90) -> str:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
        with self.connection:
            self.connection.execute(
                "DELETE FROM preview_grants WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
            self.connection.execute(
                """INSERT INTO preview_grants(token_hash, user_id, camera_id, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (self._token_hash(token), user_id, camera_id, expires.isoformat()),
            )
        return token

    def user_for_preview_grant(self, token: str, camera_id: int) -> User | None:
        row = self.connection.execute(
            """SELECT u.id, u.email, u.display_name, u.role, p.expires_at
               FROM preview_grants p JOIN users u ON u.id = p.user_id
               WHERE p.token_hash = ? AND p.camera_id = ?""",
            (self._token_hash(token), camera_id),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            with self.connection:
                self.connection.execute(
                    "DELETE FROM preview_grants WHERE token_hash = ?", (self._token_hash(token),)
                )
            return None
        return self._user(row)

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(int(row["id"]), row["email"], row["display_name"], row["role"])

    @staticmethod
    def _token_hash(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
