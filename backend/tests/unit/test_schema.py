"""Fresh-schema invariants and idempotent initialization."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from verifeye.storage import EmbeddingStore


class FreshSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "verifeye.db"
        with EmbeddingStore(self.database):
            pass

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def test_reopening_fresh_schema_is_idempotent(self):
        with EmbeddingStore(self.database) as store:
            identity_id = store.upsert_identity("person-1", "Ada")
        with EmbeddingStore(self.database) as store:
            self.assertEqual(store.list_identities()[0].id, identity_id)

    def test_only_authentication_tables_contain_user_id(self):
        with closing(self.connect()) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            owners = {
                table for table in tables
                if "user_id" in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            }
        self.assertEqual(owners, {"sessions", "preview_grants"})
        self.assertNotIn("schema_version", tables)

    def test_database_allows_only_one_user_per_role(self):
        with closing(self.connect()) as connection:
            values = (b"hash", b"salt")
            connection.execute(
                "INSERT INTO users(email,display_name,password_hash,password_salt,role) VALUES('a@x.test','A',?,?,'admin')",
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO users(email,display_name,password_hash,password_salt,role) VALUES('b@x.test','B',?,?,'admin')",
                    values,
                )
            connection.execute(
                "INSERT INTO users(email,display_name,password_hash,password_salt,role) VALUES('g@x.test','G',?,?,'guest')",
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO users(email,display_name,password_hash,password_salt,role) VALUES('h@x.test','H',?,?,'guest')",
                    values,
                )

    def test_single_plan_a_user_is_promoted_to_administrator(self):
        legacy = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute(
                """CREATE TABLE users(
                       id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                       display_name TEXT NOT NULL, password_hash BLOB NOT NULL,
                       password_salt BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            connection.execute(
                "INSERT INTO users(email,display_name,password_hash,password_salt) VALUES('owner@x.test','Owner',x'01',x'02')"
            )
            connection.commit()
        with EmbeddingStore(legacy) as store:
            row = store._connection.execute("SELECT role FROM users").fetchone()
            self.assertEqual(row["role"], "admin")

    def test_notification_rule_shape_is_enforced_and_targets_can_repeat(self):
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO identities(external_id,display_name) VALUES('person-1','Ada')"
            )
            connection.execute(
                """INSERT INTO notification_rules(
                       identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                   ) VALUES(1,'identity',0,0,'now','now')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO notification_rules(
                           identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                       ) VALUES(NULL,'identity',0,0,'now','now')"""
                )
            connection.execute(
                """INSERT INTO notification_rules(
                       identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                   ) VALUES(NULL,'unknown_face',0,0,'now','now')"""
            )
            connection.execute(
                """INSERT INTO notification_rules(
                       identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                   ) VALUES(NULL,'unknown_face',0,0,'now','now')"""
            )
            connection.execute(
                """INSERT INTO notification_rules(
                       identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                   ) VALUES(1,'identity',0,0,'now','now')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO notification_rules(
                           identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                       ) VALUES(1,'no_face',0,0,'now','now')"""
                )

    def test_legacy_notification_rule_constraints_are_migrated(self):
        legacy = Path(self.temp.name) / "legacy-notifications.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(
                (Path(__file__).resolve().parents[2] / "src/verifeye/storage/schema.sql").read_text()
            )
            connection.execute("DROP TABLE notification_rules")
            connection.execute(
                """CREATE TABLE notification_rules (
                       id INTEGER PRIMARY KEY,
                       identity_id INTEGER UNIQUE REFERENCES identities(id) ON DELETE CASCADE,
                       rule_type TEXT NOT NULL CHECK (rule_type IN ('identity', 'unknown_face', 'no_face', 'system_error')),
                       email_address TEXT, phone_number TEXT,
                       email_enabled INTEGER NOT NULL DEFAULT 0, sms_enabled INTEGER NOT NULL DEFAULT 0,
                       version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                       CHECK ((rule_type = 'identity' AND identity_id IS NOT NULL) OR
                              (rule_type <> 'identity' AND identity_id IS NULL))
                   )"""
            )
            connection.execute(
                "CREATE UNIQUE INDEX ux_notification_rules_session_type "
                "ON notification_rules(rule_type) WHERE rule_type <> 'identity'"
            )
            connection.execute("INSERT INTO identities(external_id,display_name) VALUES('person-1','Ada')")
            connection.execute(
                "INSERT INTO cameras(name,encrypted_url,url_fingerprint,sanitized_host) "
                "VALUES('Door',x'01','door-fingerprint','door.local')"
            )
            connection.execute(
                "INSERT INTO notification_rules(identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at) "
                "VALUES(1,'identity',0,0,'now','now')"
            )
            connection.execute("INSERT INTO notification_rule_cameras(rule_id,camera_id) VALUES(1,1)")
            connection.commit()
        with EmbeddingStore(legacy):
            pass
        with closing(sqlite3.connect(legacy)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT camera_id FROM notification_rule_cameras WHERE rule_id=1").fetchone()[0],
                1,
            )
            connection.execute(
                "INSERT INTO notification_rules(identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at) "
                "VALUES(1,'identity',0,0,'now','now')"
            )
            connection.execute(
                "INSERT INTO notification_rules(identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at) "
                "VALUES(NULL,'unknown_face',0,0,'now','now')"
            )


if __name__ == "__main__":
    unittest.main()
