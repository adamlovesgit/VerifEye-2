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
        self.assertEqual(owners, {"sessions"})
        self.assertNotIn("schema_version", tables)

    def test_notification_rule_shape_and_uniqueness_are_enforced(self):
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
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO notification_rules(
                           identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                       ) VALUES(NULL,'unknown_face',0,0,'now','now')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO notification_rules(
                           identity_id,rule_type,email_enabled,sms_enabled,created_at,updated_at
                       ) VALUES(1,'no_face',0,0,'now','now')"""
                )


if __name__ == "__main__":
    unittest.main()
