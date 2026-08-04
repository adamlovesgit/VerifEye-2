"""Triggered camera runtime behavior without camera hardware."""

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.modules.setdefault("cv2", SimpleNamespace(
    IMWRITE_JPEG_QUALITY=1,
    FONT_HERSHEY_SIMPLEX=0,
    imencode=lambda _extension, frame, _options: (True, np.asarray(frame, dtype=np.uint8)),
))
sys.modules.setdefault("mediapipe", SimpleNamespace())

from verifeye.cameras.models import Camera, RecognitionSessionState
from verifeye.cameras.runtime import CameraWorker


class StreamingSource:
    def __init__(self):
        self.closed = threading.Event()
        self.value = 0
    def frames(self):
        while not self.closed.is_set():
            self.value += 1
            yield np.full((12, 12, 3), self.value % 255, dtype=np.uint8)
            self.closed.wait(.005)
    def close(self): self.closed.set()


class Detector:
    def __init__(self): self.closed = False
    def close(self): self.closed = True


class Matcher:
    def match(self, _embedding): raise AssertionError("No faces were returned")


def wait_for(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(): return
        time.sleep(.005)
    raise AssertionError("Timed out waiting for runtime state")


class TriggeredRuntimeTests(unittest.TestCase):
    def make_worker(self, calls, detectors):
        camera = Camera(1, "Door", "rtsp://host/light", "host", "manual", True)
        source = StreamingSource()
        def process(frame, detector, engine, _config):
            calls.append((int(frame[0, 0, 0]), detector, engine))
            return []
        worker = CameraWorker(
            camera, object(), Matcher(), fps=100, timeout=.05,
            source_factory=lambda _url, _timeout: source,
            pre_roll_seconds=.08, recognition_window_seconds=.15,
            max_session_seconds=.3, pre_roll_max_frames=20,
            detector_factory=lambda _confidence: detectors.append(Detector()) or detectors[-1],
            frame_processor=process,
        )
        return worker, source

    def test_idle_capture_makes_zero_recognition_calls(self):
        calls, detectors = [], []
        worker, source = self.make_worker(calls, detectors)
        worker.start()
        try:
            wait_for(lambda: source.value >= 3)
            self.assertEqual(calls, [])
            self.assertEqual(detectors, [])
        finally:
            worker.stop(1)

    def test_preview_rate_is_independent_from_recognition_sampling(self):
        calls, detectors = [], []
        worker, source = self.make_worker(calls, detectors)
        worker.period = .1
        worker.preview_period = .02
        worker.start()
        try:
            wait_for(lambda: worker.publisher.wait_after(0, 0)[0] >= 1)
            time.sleep(.16)
            preview_sequence = worker.publisher.wait_after(0, 0)[0]
            self.assertGreaterEqual(preview_sequence, 5)
            self.assertLessEqual(worker._sequence, 3)
            self.assertEqual(calls, [])
        finally:
            worker.stop(1)

    def test_trigger_processes_preroll_then_live_frames(self):
        calls, detectors = [], []
        worker, source = self.make_worker(calls, detectors)
        worker.start()
        try:
            wait_for(lambda: source.value >= 5)
            boundary = source.value
            worker.trigger_recognition()
            wait_for(lambda: worker.status.recognition_session_state == RecognitionSessionState.IDLE)
            values = [item[0] for item in calls]
            self.assertTrue(any(value <= boundary for value in values), values)
            self.assertTrue(any(value > boundary for value in values), values)
            self.assertEqual(values, sorted(values))
            self.assertEqual(len(detectors), 1)
            self.assertTrue(detectors[0].closed)
        finally:
            worker.stop(1)

    def test_overlapping_trigger_extends_one_session(self):
        calls, detectors = [], []
        worker, source = self.make_worker(calls, detectors)
        worker.start()
        try:
            wait_for(lambda: source.value >= 3)
            first = worker.trigger_recognition()
            time.sleep(.05)
            second = worker.trigger_recognition()
            self.assertTrue(second.extended)
            self.assertGreater(second.deadline, first.deadline)
            self.assertLessEqual(second.deadline, second.maximum_deadline)
            wait_for(lambda: worker.status.recognition_session_state == RecognitionSessionState.IDLE)
            self.assertEqual(len(detectors), 1)
        finally:
            worker.stop(1)


if __name__ == "__main__": unittest.main()
