"""Unit tests for local embedding persistence and matching."""

import sys
from pathlib import Path
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

    def test_rejects_invalid_embedding(self) -> None:
        identity_id = self.store.upsert_identity("person-1", "Ada")
        with self.assertRaises(ValueError):
            self.store.add_embedding(identity_id, np.zeros(512, dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
