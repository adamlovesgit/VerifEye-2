"""Unit tests for local embedding persistence and matching."""

import sys
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.storage import EmbeddingStore  # noqa: E402


class EmbeddingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EmbeddingStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_round_trip_preserves_float32_vector_and_metadata(self) -> None:
        identity_id = self.store.upsert_identity("person-1", "Ada Lovelace")
        vector = np.arange(1, 513, dtype=np.float32)
        embedding_id = self.store.add_embedding(
            identity_id, vector, source_path="ada.npz", detection_score=0.98,
            metadata={"face_index": 0},
        )

        record = self.store.get_embedding(embedding_id)

        self.assertIsNotNone(record)
        self.assertEqual(record.embedding.shape, (512,))
        self.assertEqual(record.embedding.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(record.embedding)), 1.0, places=6)
        self.assertEqual(record.metadata, {"face_index": 0})

    def test_cosine_search_returns_closest_identity_first(self) -> None:
        ada = self.store.upsert_identity("ada", "Ada")
        grace = self.store.upsert_identity("grace", "Grace")
        self.store.add_embedding(ada, [1.0, 0.0, 0.0])
        self.store.add_embedding(grace, [0.0, 1.0, 0.0])

        matches = self.store.find_matches([0.9, 0.1, 0.0], limit=1)

        self.assertEqual(matches[0].external_id, "ada")
        self.assertGreater(matches[0].similarity, 0.99)

    def test_matching_only_searches_the_requested_users_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "owned.db"
            with EmbeddingStore(database): pass
            connection = sqlite3.connect(database)
            try:
                connection.execute("ALTER TABLE identities ADD COLUMN user_id INTEGER REFERENCES users(id)")
                connection.executemany(
                    "INSERT INTO users(email,display_name,password_hash,password_salt) VALUES(?,?,?,?)",
                    [
                        ("one@example.com", "One", b"hash", b"salt"),
                        ("two@example.com", "Two", b"hash", b"salt"),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            with EmbeddingStore(database) as store:
                first = store.upsert_identity("first", "First user", 1)
                second = store.upsert_identity("second", "Second user", 2)
                store.add_embedding(first, [1.0, 0.0])
                store.add_embedding(second, [1.0, 0.0])

                first_matches = store.find_matches([1.0, 0.0], user_id=1)
                second_matches = store.find_matches([1.0, 0.0], user_id=2)

            self.assertEqual([match.identity_id for match in first_matches], [first])
            self.assertEqual([match.identity_id for match in second_matches], [second])

    def test_rejects_invalid_embedding(self) -> None:
        identity_id = self.store.upsert_identity("person-1", "Ada")
        with self.assertRaises(ValueError):
            self.store.add_embedding(identity_id, np.zeros(512, dtype=np.float32))


    def test_lists_identity_metadata_and_deletes_embeddings(self) -> None:
        identity_id = self.store.upsert_identity("ada", "Ada Lovelace")
        self.store.add_embedding(
            identity_id, [1.0, 0.0], source_path="ada/face.png",
            detection_score=.97, metadata={"original_name": "portrait.png"},
        )
        identities = self.store.list_identities()
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0].display_name, "Ada Lovelace")
        self.assertEqual(identities[0].embeddings[0].metadata["original_name"], "portrait.png")
        self.assertEqual(self.store.delete_identity(identity_id), ["ada/face.png"])
        self.assertEqual(self.store.list_identities(), [])


if __name__ == "__main__":
    unittest.main()
