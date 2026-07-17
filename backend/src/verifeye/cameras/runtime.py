"""Concrete camera manager, supervised workers, and non-destructive fan-out."""

from __future__ import annotations

import threading
import time
from typing import Callable

import cv2
import mediapipe as mp

from .models import ActiveCameraLimitReached, CameraStatus, ConnectionState
from ..recognition import annotate
from ..vision import process_frame


class LatestFrame:
    """Single-slot decoded-frame buffer."""
    def __init__(self): self._condition, self._value, self._sequence = threading.Condition(), None, 0
    def put(self, value):
        with self._condition:
            self._value, self._sequence = value, self._sequence + 1
            self._condition.notify_all()
    def get_after(self, sequence, timeout=None):
        with self._condition:
            self._condition.wait_for(lambda: self._sequence > sequence, timeout)
            return self._sequence, self._value
    def clear(self):
        with self._condition: self._value = None


class FramePublisher:
    """Immutable latest-JPEG fan-out; subscriber reads never consume data."""
    def __init__(self): self._condition, self._jpeg, self._sequence, self._closed = threading.Condition(), None, 0, False
    def publish(self, jpeg: bytes):
        with self._condition:
            self._jpeg, self._sequence = bytes(jpeg), self._sequence + 1
            self._condition.notify_all()
    def wait_after(self, sequence: int, timeout=15.0):
        with self._condition:
            self._condition.wait_for(lambda: self._sequence > sequence or self._closed, timeout)
            return self._sequence, self._jpeg
    def close(self):
        with self._condition:
            self._closed, self._jpeg = True, None
            self._condition.notify_all()


class PyAvFrameSource:
    def __init__(self, url: str, timeout: float):
        import av
        self._container = av.open(url, options={"rtsp_transport": "tcp", "stimeout": str(int(timeout * 1_000_000))}, timeout=timeout)
    def frames(self):
        for frame in self._container.decode(video=0): yield frame.to_ndarray(format="bgr24")
    def close(self): self._container.close()


class CameraWorker:
    def __init__(self, camera, engine, matcher, fps, timeout, source_factory=PyAvFrameSource):
        self.camera, self.engine, self.matcher = camera, engine, matcher
        self.period, self.timeout, self.source_factory = 1 / fps, timeout, source_factory
        self.publisher, self.latest = FramePublisher(), LatestFrame()
        self._stop, self._thread, self._source = threading.Event(), None, None
        self._lock = threading.Lock()
        self._status = CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)

    @property
    def status(self):
        with self._lock: return self._status
    def _set(self, **changes):
        with self._lock:
            values = self._status.__dict__ | changes
            self._status = CameraStatus(**values)
    def start(self):
        self._set(running=True, connection_state=ConnectionState.CONNECTING)
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.camera.id}", daemon=True); self._thread.start()
    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self._set(connection_state=ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RETRYING,
                          retry_attempt=attempt, next_retry_at=None, last_error=None)
                self._source = self.source_factory(self.camera.url, self.timeout)
                attempt = 0
                decoder_done, decoder_errors = threading.Event(), []
                def decode_latest():
                    try:
                        for frame in self._source.frames():
                            if self._stop.is_set(): break
                            self.latest.put(frame)
                    except Exception as error:
                        decoder_errors.append(error)
                    finally:
                        decoder_done.set()
                decoder = threading.Thread(target=decode_latest, name=f"camera-{self.camera.id}-decoder", daemon=True)
                decoder.start()
                detector = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=.5)
                try:
                    sequence = 0
                    while not self._stop.is_set():
                        next_sequence, newest = self.latest.get_after(sequence, self.period)
                        if next_sequence == sequence or newest is None:
                            if decoder_done.is_set(): break
                            continue
                        sequence = next_sequence
                        faces = process_frame(newest, detector, self.engine, {"detector": {"min_conf": .5, "pad_ratio": .15}})
                        labeled = [(face, self.matcher.match(face.embedding)) for face in faces]
                        ok, jpeg = cv2.imencode(".jpg", annotate(newest, labeled), [cv2.IMWRITE_JPEG_QUALITY, 82])
                        if ok: self.publisher.publish(jpeg.tobytes())
                        self._set(connection_state=ConnectionState.LIVE, last_frame_at=time.time())
                        self._stop.wait(self.period)
                finally:
                    detector.close()
                    if self._source:
                        try: self._source.close()
                        except Exception: pass
                    decoder.join(self.timeout)
                if decoder_errors: raise decoder_errors[0]
                if not self._stop.is_set(): raise OSError("RTSP stream ended")
            except Exception as exc:
                if self._stop.is_set(): break
                message = str(exc).lower()
                state = ConnectionState.AUTHENTICATION_FAILED if any(x in message for x in ("401", "unauthorized", "authentication")) else ConnectionState.OFFLINE
                self._set(connection_state=state, last_error="authentication" if state == ConnectionState.AUTHENTICATION_FAILED else "unavailable")
                attempt += 1; delay = min(30.0, 2 ** min(attempt, 5))
                if state != ConnectionState.AUTHENTICATION_FAILED:
                    self._set(connection_state=ConnectionState.RETRYING, retry_attempt=attempt, next_retry_at=time.time() + delay)
                else:
                    self._set(retry_attempt=attempt, next_retry_at=time.time() + delay)
                self._stop.wait(delay)
            finally:
                if self._source:
                    try: self._source.close()
                    except Exception: pass
                    self._source = None
        self.latest.clear(); self.publisher.close(); self._set(running=False, connection_state=ConnectionState.STOPPED, next_retry_at=None)
    def stop(self, timeout):
        self._stop.set()
        if self._source:
            try: self._source.close()
            except Exception: pass
        if self._thread: self._thread.join(timeout)
        if self._thread and self._thread.is_alive(): raise TimeoutError(f"Camera {self.camera.id} did not stop within the cleanup deadline.")


class CameraManager:
    def __init__(self, repository, engine, matcher, fps=2.0, timeout=8.0, cleanup_timeout=10.0, max_active=4, worker_factory=CameraWorker):
        self.repository, self.engine, self.matcher = repository, engine, matcher
        self.fps, self.timeout, self.cleanup_timeout, self.max_active, self.worker_factory = fps, timeout, cleanup_timeout, max_active, worker_factory
        self._workers, self._lock, self._shutting_down = {}, threading.RLock(), False
    def start(self, camera_id):
        with self._lock:
            if self._shutting_down: raise RuntimeError("Camera manager is shutting down.")
            if camera_id in self._workers: return self._workers[camera_id].status
            if len(self._workers) >= self.max_active: raise ActiveCameraLimitReached("The active-camera limit has been reached.")
            camera = self.repository.get(camera_id)
            worker = self.worker_factory(camera, self.engine, self.matcher, self.fps, self.timeout)
            self._workers[camera_id] = worker; worker.start(); return worker.status
    def stop(self, camera_id):
        with self._lock: worker = self._workers.get(camera_id)
        if worker:
            worker.stop(self.cleanup_timeout)
            with self._lock: self._workers.pop(camera_id, None)
        camera = self.repository.get(camera_id)
        return CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)
    def restart(self, camera_id): self.stop(camera_id); return self.start(camera_id)
    def delete(self, camera_id): self.stop(camera_id); self.repository.delete(camera_id)
    def status(self, camera_id):
        camera = self.repository.get(camera_id)
        with self._lock: worker = self._workers.get(camera_id)
        return worker.status if worker else CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)
    def publisher(self, camera_id):
        with self._lock: worker = self._workers.get(camera_id)
        return worker.publisher if worker else None
    def start_enabled(self):
        for camera in self.repository.list():
            if camera.enabled: self.start(camera.id)
    def shutdown(self):
        with self._lock: self._shutting_down, ids = True, list(self._workers)
        for camera_id in ids:
            try: self.stop(camera_id)
            except Exception: pass
