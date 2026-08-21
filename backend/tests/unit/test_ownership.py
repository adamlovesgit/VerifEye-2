"""Regression coverage for schemas that require durable user ownership."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cryptography.fernet import Fernet

from verifeye.cameras.repository import CameraRepository
from verifeye.cameras.security import CredentialCipher
from verifeye.events import EventRepository, utcnow
from verifeye.notifications import NotificationRepository
from verifeye.storage import EmbeddingStore


class OwnershipWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "owned.db"
        with EmbeddingStore(self.database): pass
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO users(email,display_name,password_hash,password_salt) VALUES(?,?,?,?)",
                ("owner@example.com", "Owner", b"hash", b"salt"),
            )
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
            for table in ("cameras", "camera_events", "identities", "notification_rules", "notification_deliveries"):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id)")
                connection.execute(f"""CREATE TRIGGER require_{table}_user_insert
                    BEFORE INSERT ON {table} WHEN NEW.user_id IS NULL
                    BEGIN SELECT RAISE(ABORT, 'user_id is required'); END""")
            connection.commit()
        finally:
            connection.close()
    def tearDown(self): self.temp.cleanup()

    def test_all_triggered_writes_receive_their_owner(self):
        cameras = CameraRepository(
            self.database, CredentialCipher(Fernet.generate_key().decode())
        )
        camera = cameras.create(
            "Door", "rtsp://camera.local/sub", True, user_id=self.user_id
        )
        with EmbeddingStore(self.database) as store:
            identity_id = store.upsert_identity("owner-face", "Owner", self.user_id)
            store.add_embedding(identity_id, [1.0, 0.0])

        events = EventRepository(self.database)
        event = events.accept_event(camera.id, "motion-1", "motion", utcnow(), {})

        notifications = NotificationRepository(self.database)
        rule_id = notifications.save_rule({
            "identityId": identity_id, "isFallback": False,
            "emailAddress": "owner@example.com", "phoneNumber": "",
            "emailEnabled": True, "smsEnabled": False,
            "outcomes": ["recognized"], "cameraIds": [camera.id],
        }, user_id=self.user_id)
        delivery_id = notifications.enqueue_test(
            rule_id, "email", "http://127.0.0.1:8000", self.user_id
        )

        connection = sqlite3.connect(self.database)
        try:
            for table, row_id in (
                ("cameras", camera.id), ("identities", identity_id),
                ("camera_events", event.id), ("notification_rules", rule_id),
                ("notification_deliveries", delivery_id),
            ):
                owner = connection.execute(
                    f"SELECT user_id FROM {table} WHERE id=?", (row_id,)
                ).fetchone()[0]
                self.assertEqual(owner, self.user_id, table)
        finally:
            connection.close()


if __name__ == "__main__": unittest.main()
