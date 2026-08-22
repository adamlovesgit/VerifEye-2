"""Unit tests for named, multi-identity enrollment."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.enrollment import EnrollmentError, EnrollmentService  # noqa: E402
from verifeye.storage import EmbeddingStore  # noqa: E402
from verifeye.vision.types import BoundingBox  # noqa: E402


class EnrollmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "verifeye.db"
        self.service = EnrollmentService(self.database, self.root / "uploads", Mock())
        self.face = SimpleNamespace(
            embedding=np.array([1.0, 0.0], dtype=np.float32), score=.97,
            raw_box=BoundingBox(2, 2, 8, 8, .97), pad_box=BoundingBox(1, 1, 9, 9, .97),
            landmarks=[(3, 4), (7, 4), (5, 6)],
        )

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
        metadata = identities[0].embeddings[0].metadata
        self.assertEqual(metadata["raw_box"]["x1"], 2)
        self.assertEqual(metadata["padded_box"]["x2"], 9)
        self.assertEqual(metadata["landmarks"], [[3, 4], [7, 4], [5, 6]])
        self.assertEqual(metadata["image_size"], {"width": 10, "height": 10})

    def test_reference_image_draws_saved_boxes_and_landmarks(self):
        uploads = self.root / "uploads"
        uploads.mkdir()
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", frame)
        self.assertTrue(ok)
        (uploads / "face.png").write_bytes(encoded.tobytes())
        with EmbeddingStore(self.database) as store:
            identity_id = store.upsert_identity("person-1", "Ada")
            embedding_id = store.add_embedding(
                identity_id, [1.0, 0.0], source_path="face.png", detection_score=.97,
                metadata={
                    "raw_box": {"x1": 8, "y1": 8, "x2": 30, "y2": 30, "score": .97},
                    "padded_box": {"x1": 5, "y1": 5, "x2": 34, "y2": 34, "score": .97},
                    "landmarks": [[13, 15], [25, 15], [19, 23]],
                },
            )
        rendered = self.service.reference_image(embedding_id)
        annotated = cv2.imdecode(np.frombuffer(rendered, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(annotated.shape, frame.shape)
        self.assertGreater(int(annotated.max()), 100)

    def test_blank_name_is_rejected_before_face_processing(self):
        with self.assertRaisesRegex(EnrollmentError, "Enter a name"):
            self.service.enroll("   ", b"image", ".jpg", "face.jpg")


if __name__ == "__main__": unittest.main()
