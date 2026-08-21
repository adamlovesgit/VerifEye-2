"""Unit tests for local accounts and sessions."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.auth import AuthError, AuthStore, SetupComplete  # noqa: E402
from verifeye.storage import EmbeddingStore  # noqa: E402


class AuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EmbeddingStore(":memory:")
        self.auth = AuthStore(self.store._connection)

    def tearDown(self) -> None:
        self.store.close()

    def test_create_authenticate_and_restore_session(self) -> None:
        created = self.auth.create_user("ADA@Example.com", "Ada Lovelace", "correct horse")
        authenticated = self.auth.authenticate("ada@example.com", "correct horse")
        token = self.auth.create_session(authenticated.id)

        restored = self.auth.user_for_session(token)

        self.assertEqual(created.email, "ada@example.com")
        self.assertEqual(restored, created)

    def test_password_is_not_stored_as_plaintext(self) -> None:
        self.auth.create_user("ada@example.com", "Ada", "correct horse")
        row = self.store._connection.execute("SELECT password_hash FROM users").fetchone()
        self.assertNotEqual(row["password_hash"], b"correct horse")
        with self.assertRaises(AuthError):
            self.auth.authenticate("ada@example.com", "wrong password")

    def test_duplicate_email_is_rejected_case_insensitively(self) -> None:
        self.auth.create_user("ada@example.com", "Ada", "correct horse")
        with self.assertRaises(AuthError):
            self.auth.create_user("ADA@example.com", "Other Ada", "correct horse")

    def test_logout_invalidates_session(self) -> None:
        user = self.auth.create_user("ada@example.com", "Ada", "correct horse")
        token = self.auth.create_session(user.id)
        self.auth.delete_session(token)
        self.assertIsNone(self.auth.user_for_session(token))

    def test_initial_setup_creates_one_administrator(self) -> None:
        self.assertTrue(self.auth.setup_required())
        user = self.auth.create_initial_admin("owner@example.com", "Owner", "password123")
        self.assertEqual(user.role, "admin")
        self.assertFalse(self.auth.setup_required())
        with self.assertRaises(SetupComplete):
            self.auth.create_initial_admin("other@example.com", "Other", "password123")

    def test_guest_rotation_invalidates_sessions_and_preserves_one_guest(self) -> None:
        guest = self.auth.replace_guest("guest@example.com", "Guest", "password123")
        token = self.auth.create_session(guest.id)
        rotated = self.auth.replace_guest("viewer@example.com", "Viewer", "new-password")
        self.assertEqual(rotated.id, guest.id)
        self.assertIsNone(self.auth.user_for_session(token))
        self.assertEqual(self.auth.authenticate("viewer@example.com", "new-password").role, "guest")
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM users WHERE role='guest'").fetchone()[0],
            1,
        )

    def test_guest_revocation_deletes_account_and_sessions(self) -> None:
        guest = self.auth.replace_guest("guest@example.com", "Guest", "password123")
        token = self.auth.create_session(guest.id)
        self.assertTrue(self.auth.revoke_guest())
        self.assertFalse(self.auth.revoke_guest())
        self.assertIsNone(self.auth.user_for_session(token))

    def test_administrator_password_recovery_revokes_sessions(self) -> None:
        admin = self.auth.create_initial_admin("owner@example.com", "Owner", "password123")
        token = self.auth.create_session(admin.id)
        recovered = self.auth.reset_admin_password("replacement-password")
        self.assertEqual(recovered, admin)
        self.assertIsNone(self.auth.user_for_session(token))
        self.assertEqual(
            self.auth.authenticate("owner@example.com", "replacement-password"), admin
        )


if __name__ == "__main__":
    unittest.main()
