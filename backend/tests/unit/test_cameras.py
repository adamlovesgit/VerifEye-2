"""Camera persistence, buffering, fan-out, and orchestration tests."""

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cryptography.fernet import Fernet
from verifeye.cameras.models import Camera, ConnectionState, DuplicateCamera
from verifeye.cameras.repository import CameraRepository
from verifeye.cameras.runtime import CameraManager, FramePublisher, LatestFrame
from verifeye.cameras.security import CredentialCipher, validate_rtsp_url
from verifeye.storage import EmbeddingStore


class CameraRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.database = Path(self.temp.name) / "test.db"
        with EmbeddingStore(self.database): pass
        self.key = Fernet.generate_key().decode(); self.repository = CameraRepository(self.database, CredentialCipher(self.key))
    def tearDown(self): self.temp.cleanup()
    def test_url_is_encrypted_and_response_is_sanitized(self):
        camera = self.repository.create("Door", "rtsp://alice:secret@10.0.0.2:8554/live", True)
        self.assertEqual(camera.sanitized_host, "10.0.0.2:8554"); self.assertIn("secret", camera.url)
        raw = self.database.read_bytes(); self.assertNotIn(b"secret", raw); self.assertNotIn(b"alice", raw)
    def test_duplicate_url_is_rejected_despite_randomized_encryption(self):
        self.repository.create("One", "rtsp://10.0.0.2/live", False)
        with self.assertRaises(DuplicateCamera): self.repository.create("Two", "rtsp://10.0.0.2/live", False)
    def test_wrong_key_cannot_read_saved_credentials(self):
        camera = self.repository.create("Door", "rtsp://10.0.0.2/live", False)
        wrong = CameraRepository(self.database, CredentialCipher(Fernet.generate_key().decode()))
        with self.assertRaises(ValueError): wrong.get(camera.id)
    def test_manual_validation_is_syntax_only(self):
        self.assertEqual(validate_rtsp_url("rtsp://camera.local/live"), "rtsp://camera.local/live")
        with self.assertRaises(ValueError): validate_rtsp_url("http://camera.local/live")
    def test_recognition_url_is_encrypted_independently(self):
        camera = self.repository.create(
            "Door", "rtsp://camera.local/light", False, recognition_url="rtsp://camera.local/main"
        )
        self.assertEqual(camera.recognition_url, "rtsp://camera.local/main")
        raw = self.database.read_bytes()
        self.assertNotIn(b"rtsp://camera.local/light", raw)
        self.assertNotIn(b"rtsp://camera.local/main", raw)
    def test_equivalent_recognition_url_uses_lightweight_connection(self):
        camera = self.repository.create(
            "Door", "rtsp://CAMERA.local:554/live", False,
            recognition_url="RTSP://camera.local/live",
        )
        self.assertIsNone(camera.recognition_url)


class BufferTests(unittest.TestCase):
    def test_latest_frame_replaces_old_values(self):
        buffer = LatestFrame(); buffer.put("old"); buffer.put("new"); sequence, value = buffer.get_after(0, 0)
        self.assertEqual((sequence, value), (2, "new"))
    def test_publisher_fans_out_without_consumption(self):
        publisher = FramePublisher(); publisher.publish(b"frame")
        self.assertEqual(publisher.wait_after(0, 0), (1, b"frame")); self.assertEqual(publisher.wait_after(0, 0), (1, b"frame"))
        publisher.publish(b"new"); self.assertEqual(publisher.wait_after(1, 0), (2, b"new"))


class FakeRepository:
    def __init__(self): self.camera = Camera(1, "Door", "rtsp://host/live", "host", "manual", True); self.deleted = False
    def get(self, _): return self.camera
    def list(self): return [self.camera]
    def delete(self, _): self.deleted = True

class FakeWorker:
    def __init__(self, camera, *_): self.camera=camera; self.started=False; self.stopped=False
    @property
    def status(self):
        from verifeye.cameras.models import CameraStatus
        return CameraStatus(self.camera.id, self.camera.enabled, self.started and not self.stopped, ConnectionState.LIVE if self.started and not self.stopped else ConnectionState.STOPPED)
    def start(self): self.started=True
    def stop(self, _): self.stopped=True

class SlowWorker(FakeWorker):
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    def request_stop(self): self.stopped=True
    def finish_stop(self, _):
        self.cleanup_started.set()
        self.cleanup_release.wait(1)

class ManagerTests(unittest.TestCase):
    def test_one_worker_and_cleanup_before_delete(self):
        repository=FakeRepository(); manager=CameraManager(repository, object(), object(), worker_factory=FakeWorker)
        manager.start(1); first=manager._workers[1]; manager.start(1)
        self.assertIs(manager._workers[1], first); manager.delete(1)
        self.assertTrue(first.stopped); self.assertTrue(repository.deleted); self.assertFalse(manager.status(1).running)

    def test_stop_returns_without_waiting_for_cleanup(self):
        SlowWorker.cleanup_started.clear(); SlowWorker.cleanup_release.clear()
        repository=FakeRepository(); manager=CameraManager(repository, object(), object(), worker_factory=SlowWorker)
        manager.start(1)
        started=time.monotonic(); status=manager.stop(1); elapsed=time.monotonic()-started
        self.assertLess(elapsed, .2); self.assertFalse(status.running)
        self.assertNotIn(1, manager._workers)
        self.assertTrue(SlowWorker.cleanup_started.wait(.2))
        SlowWorker.cleanup_release.set()


if __name__ == "__main__": unittest.main()
