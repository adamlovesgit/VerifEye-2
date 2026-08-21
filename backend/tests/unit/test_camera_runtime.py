"""Capture ownership, pre-roll API, and explicit recognition-mode behavior."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.modules.setdefault("mediapipe", SimpleNamespace())

from verifeye.cameras.models import Camera
from verifeye.cameras.runtime import (
    FrameRecord, PreRollBuffer, PreRollCapture, RecognitionCapture, RecognitionSessionManager,
    _RecognitionRuntime,
)


UTC = timezone.utc


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.005)
    raise AssertionError("Timed out")


class StreamingSource:
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    def __init__(self):
        self.closed = threading.Event(); self.value = 0
        with self.lock:
            type(self).active += 1
            type(self).maximum_active = max(type(self).maximum_active, type(self).active)
    def frames(self):
        try:
            while not self.closed.is_set():
                self.value += 1
                yield np.full((12, 12, 3), self.value % 255, dtype=np.uint8)
                self.closed.wait(.005)
        finally: self.close()
    def close(self):
        if not self.closed.is_set():
            self.closed.set()
            with self.lock: type(self).active -= 1


class ReconnectingSource(StreamingSource):
    created = 0
    def __init__(self):
        type(self).created += 1
        super().__init__()
    def frames(self):
        try:
            yield np.zeros((12, 12, 3), dtype=np.uint8)
            raise OSError("synthetic disconnect")
        finally:
            self.close()


class BlockingSource:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.close_threads = []
    def frames(self):
        self.entered.set(); self.release.wait()
        if False: yield None
    def close(self): self.close_threads.append(threading.get_ident())


class PreRollTests(unittest.TestCase):
    def test_snapshot_between_is_utc_api_and_buffer_evicts_monotonically(self):
        buffer = PreRollBuffer(5, 10)
        frame = np.zeros((1, 1, 3), np.uint8)
        for sequence in range(1, 4):
            buffer.put(FrameRecord(1, sequence, 1, 100 + sequence, 200 + sequence,
                                   "lightweight", frame))
        self.assertEqual([r.sequence for r in buffer.snapshot_between(101.5, 103)], [2, 3])
        buffer.put(FrameRecord(1, 4, 1, 104, 210, "lightweight", frame))
        self.assertEqual([r.sequence for r in buffer.snapshot_between(0, 999)], [4])

    def test_exactly_one_continuous_decoder_and_clean_stop(self):
        StreamingSource.active = StreamingSource.maximum_active = 0
        camera = Camera(1, "Door", "rtsp://camera", "camera", "manual", True)
        capture = PreRollCapture(camera, "rtsp://router/preview", 100, .1,
                                 source_factory=lambda *_: StreamingSource())
        capture.start()
        try:
            wait_for(lambda: capture.status.ready)
            self.assertEqual(StreamingSource.maximum_active, 1)
            now = time.time()
            self.assertTrue(capture.snapshot_between(now - 1, now + 1))
        finally: capture.stop(1)
        self.assertEqual(StreamingSource.active, 0)

    def test_reconnect_closes_the_previous_decoder_before_replacement(self):
        ReconnectingSource.active = ReconnectingSource.maximum_active = ReconnectingSource.created = 0
        camera = Camera(1, "Door", "rtsp://camera", "camera", "manual", True)
        capture = PreRollCapture(camera, "rtsp://router/preview", 100, .1,
                                 source_factory=lambda *_: ReconnectingSource(),
                                 reconnect_delay=lambda _attempt: 0)
        capture.start()
        try: wait_for(lambda: ReconnectingSource.created >= 3)
        finally: capture.stop(1)
        self.assertEqual(ReconnectingSource.maximum_active, 1)
        self.assertEqual(ReconnectingSource.active, 0)


class RecognitionCaptureTests(unittest.TestCase):
    def test_stop_does_not_close_source_from_the_calling_thread(self):
        source = BlockingSource()
        capture = RecognitionCapture(1, "rtsp://router/main", 2, .01,
                                     source_factory=lambda *_: source)
        self.assertTrue(capture.start())
        self.assertTrue(source.entered.wait(1))
        caller = threading.get_ident()
        with self.assertRaises(TimeoutError): capture.stop(.01)
        self.assertEqual(source.close_threads, [])
        source.release.set(); capture._thread.join(1)
        self.assertFalse(capture._thread.is_alive())
        self.assertEqual(len(source.close_threads), 1)
        self.assertNotEqual(source.close_threads[0], caller)


class FakePreRoll:
    def __init__(self): self.sequence = 0; self.latest_calls = 0
    def snapshot_between(self, *_): return ()
    def latest_after(self, sequence, timeout=None):
        self.latest_calls += 1
        time.sleep(min(timeout or 0, .005))
        return sequence, None


class FakeCameras:
    def __init__(self): self.capture = FakePreRoll()
    def preroll(self, _camera_id): return self.capture


class FakeSink:
    def __init__(self): self.active_modes = []; self.terminal_count = 0; self.terminal_errors = []
    def session_spec(self, _session_id):
        now = datetime.now(UTC)
        return {"interval_start": now - timedelta(seconds=5), "trigger_at": now,
                "interval_end": now + timedelta(seconds=.08),
                "maximum_end": now + timedelta(seconds=.2)}
    def active(self, _session_id, mode, error=None): self.active_modes.append((mode, error))
    def results(self, *_): pass
    def terminal(self, _session_id, error):
        self.terminal_count += 1; self.terminal_errors.append(error)


class FakeInferenceSession:
    def process(self, _record): return []
    def close(self): pass
class FakeInference:
    def __init__(self): self.opened = 0
    def open_session(self):
        self.opened += 1
        return FakeInferenceSession()


class FailingCapture:
    starts = 0
    def __init__(self, *_):
        self.error = "main unavailable"; self.failed = True; self.done = threading.Event(); self.done.set()
    def start(self): type(self).starts += 1; return False
    def stop(self, _timeout): pass


class DisconnectingCapture:
    starts = active = maximum_active = stops = 0
    def __init__(self, *_):
        self.error = None; self.failed = False; self.done = threading.Event()
    def start(self):
        type(self).starts += 1; type(self).active += 1
        type(self).maximum_active = max(type(self).maximum_active, type(self).active)
        return True
    def latest_after(self, sequence, _timeout=None):
        self.failed, self.error = True, "main disconnected"; self.done.set()
        return sequence, None
    def stop(self, _timeout):
        if type(self).active:
            type(self).active -= 1; type(self).stops += 1


class CleanupTimeoutCapture:
    def __init__(self, *_):
        self.error = None; self.failed = False; self.done = threading.Event()
    def start(self): return True
    def latest_after(self, sequence, timeout=None):
        time.sleep(min(timeout or 0, .005)); return sequence, None
    def stop(self, _timeout): raise TimeoutError("synthetic decoder cleanup timeout")


class RecognitionModeTests(unittest.TestCase):
    def make_manager(self, camera, media, capture_factory=FailingCapture):
        repository = SimpleNamespace(get=lambda _id: camera)
        cameras, sink, inference = FakeCameras(), FakeSink(), FakeInference()
        manager = RecognitionSessionManager(repository, cameras, media, inference, sink,
                                            recognition_fps=100, timeout=.01,
                                            capture_factory=capture_factory)
        return manager, cameras, sink

    def test_session_opens_installation_level_identity_matching(self):
        camera = Camera(1, "Door", "rtsp://light", "host", "manual", True)
        media = SimpleNamespace(recognition_url=lambda _camera: None)
        inference = FakeInference()
        manager = RecognitionSessionManager(
            SimpleNamespace(get=lambda _id: camera), FakeCameras(), media, inference, FakeSink(),
            recognition_fps=100, timeout=.01, capture_factory=FailingCapture,
        )

        manager.request(1, 10)
        wait_for(lambda: inference.opened == 1)

        self.assertEqual(inference.opened, 1)

    def test_shared_lightweight_uses_latest_and_opens_zero_recognition_decoders(self):
        camera = Camera(1, "Door", "rtsp://light", "host", "manual", True)
        media = SimpleNamespace(recognition_url=lambda _camera: None)
        FailingCapture.starts = 0
        manager, cameras, sink = self.make_manager(camera, media)
        manager.request(1, 10)
        wait_for(lambda: sink.terminal_count == 1)
        self.assertEqual(sink.active_modes[0][0], "shared_lightweight")
        self.assertGreater(cameras.capture.latest_calls, 0)
        self.assertEqual(FailingCapture.starts, 0)

    def test_dedicated_failure_transitions_once_and_next_session_retries(self):
        camera = Camera(1, "Door", "rtsp://light", "host", "manual", True,
                        recognition_url="rtsp://main")
        media = SimpleNamespace(recognition_url=lambda _camera: SimpleNamespace(url="rtsp://router/main"))
        FailingCapture.starts = 0
        manager, cameras, sink = self.make_manager(camera, media)
        manager.request(1, 10)
        manager.request(1, 10)  # extension must not create another decoder
        wait_for(lambda: sink.terminal_count == 1)
        self.assertEqual(FailingCapture.starts, 1)
        self.assertEqual([mode for mode, _ in sink.active_modes], ["lightweight_fallback"])
        self.assertGreater(cameras.capture.latest_calls, 0)
        manager.request(1, 11)
        wait_for(lambda: sink.terminal_count == 2)
        self.assertEqual(FailingCapture.starts, 2)

    def test_dedicated_disconnect_closes_one_decoder_and_falls_back_once(self):
        camera = Camera(1, "Door", "rtsp://light", "host", "manual", True,
                        recognition_url="rtsp://main")
        media = SimpleNamespace(recognition_url=lambda _camera: SimpleNamespace(url="rtsp://router/main"))
        DisconnectingCapture.starts = DisconnectingCapture.active = 0
        DisconnectingCapture.maximum_active = DisconnectingCapture.stops = 0
        manager, cameras, sink = self.make_manager(camera, media, DisconnectingCapture)
        manager.request(1, 10)
        wait_for(lambda: sink.terminal_count == 1)
        self.assertEqual([mode for mode, _ in sink.active_modes],
                         ["dedicated", "lightweight_fallback"])
        self.assertEqual(DisconnectingCapture.starts, 1)
        self.assertEqual(DisconnectingCapture.maximum_active, 1)
        self.assertEqual(DisconnectingCapture.active, 0)
        self.assertEqual(DisconnectingCapture.stops, 1)
        self.assertGreater(cameras.capture.latest_calls, 0)

    def test_cleanup_timeout_still_finalizes_and_releases_session(self):
        camera = Camera(1, "Door", "rtsp://light", "host", "manual", True,
                        recognition_url="rtsp://main")
        media = SimpleNamespace(recognition_url=lambda _camera: SimpleNamespace(url="rtsp://router/main"))
        manager, _cameras, sink = self.make_manager(camera, media, CleanupTimeoutCapture)
        manager.request(1, 10)
        wait_for(lambda: sink.terminal_count == 1)
        self.assertIn("synthetic decoder cleanup timeout", sink.terminal_errors[0])
        wait_for(lambda: manager.status(1).state.value == "idle")
        manager.request(1, 11)
        wait_for(lambda: sink.terminal_count == 2)

    def test_hard_cap_stays_monotonic_across_wall_clock_jump(self):
        camera = Camera(1, "Door", "rtsp://light", "host", "manual", True)
        mono, utc = [100.0], [datetime(2026, 1, 1, tzinfo=UTC)]
        manager = SimpleNamespace(monotonic=lambda: mono[0], utc_now=lambda: utc[0],
                                  wall_clock=lambda: 1_000.0)
        spec = {"interval_start": utc[0] - timedelta(seconds=5), "trigger_at": utc[0],
                "interval_end": utc[0] + timedelta(seconds=10),
                "maximum_end": utc[0] + timedelta(seconds=20)}
        runtime = _RecognitionRuntime(manager, camera, 1, spec, FakePreRoll())
        self.assertEqual(runtime.maximum_mono, 120.0)
        utc[0] -= timedelta(hours=1)
        runtime.extend(dict(spec, interval_end=spec["maximum_end"]))
        self.assertEqual(runtime.deadline_mono, 120.0)
        self.assertEqual(runtime.maximum_mono, 120.0)


if __name__ == "__main__": unittest.main()
