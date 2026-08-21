"""Unit tests for named, multi-identity enrollment."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.enrollment import EnrollmentError, EnrollmentService  # noqa: E402
from verifeye.storage import EmbeddingStore  # noqa: E402


class EnrollmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "verifeye.db"
        self.service = EnrollmentService(self.database, self.root / "uploads", Mock())
        self.face = SimpleNamespace(embedding=np.array([1.0, 0.0], dtype=np.float32), score=.97)

    def tearDown(self):
        self.temp.cleanup()

    @patch("verifeye.enrollment.process_frame")
    @patch("verifeye.enrollment.create_face_detector")
    @patch("verifeye.enrollment.cv2.imdecode")
    def test_each_named_enrollment_creates_a_distinct_identity(self, imdecode, detector_factory, process_frame):
        imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
        detector_factory.return_value = Mock()
        process_frame.return_value = [self.face]

        first = self.service.enroll("  Ada Lovelace  ", b"first", ".jpg", "ada.jpg")
        second = self.service.enroll("Grace Hopper", b"second", ".png", "grace.png")

        with EmbeddingStore(self.database) as store:
            identities = store.list_identities()
        self.assertEqual([item.display_name for item in identities], ["Ada Lovelace", "Grace Hopper"])
        self.assertNotEqual(first["identityId"], second["identityId"])
        self.assertEqual(first["displayName"], "Ada Lovelace")
        self.assertTrue(all(len(item.embeddings) == 1 for item in identities))
        source_paths = [item.embeddings[0].source_path for item in identities]
        self.assertTrue(all(Path(path).parent == Path(".") for path in source_paths))

    def test_blank_name_is_rejected_before_face_processing(self):
        with self.assertRaisesRegex(EnrollmentError, "Enter a name"):
            self.service.enroll("   ", b"image", ".jpg", "face.jpg")


if __name__ == "__main__": unittest.main()
