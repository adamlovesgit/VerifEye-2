"""Lightweight camera capture and bounded, triggered recognition sessions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time

import cv2
import numpy as np

from .models import (
    ActiveCameraLimitReached, CameraStatus, ConnectionState,
    RecognitionSessionState, RecognitionSessionStatus, RecognitionStreamMode,
)
from ..vision import create_face_detector, process_frame

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameRecord:
    camera_id: int
    sequence: int
    source_generation: int
    captured_at: float
    source_role: str
    frame: np.ndarray


class LatestFrame:
    """Single-slot handoff; new frames replace unprocessed frames."""
    def __init__(self):
        self._condition, self._value, self._sequence = threading.Condition(), None, 0
    def put(self, value):
        with self._condition:
            self._value = value
            self._sequence = getattr(value, "sequence", self._sequence + 1)
            self._condition.notify_all()
    def get_after(self, sequence, timeout=None):
        with self._condition:
            self._condition.wait_for(
                lambda: self._sequence > sequence and self._value is not None, timeout
            )
            return self._sequence, self._value
    def clear(self):
        with self._condition:
            self._value = None
            self._condition.notify_all()


class PreRollBuffer:
    def __init__(self, duration_seconds, max_frames):
        self.duration_seconds, self.max_frames = duration_seconds, max_frames
        self._frames, self._lock = deque(), threading.Lock()
    def put(self, record):
        cutoff = record.captured_at - self.duration_seconds
        with self._lock:
            self._frames.append(record)
            while self._frames and (len(self._frames) > self.max_frames or self._frames[0].captured_at < cutoff):
                self._frames.popleft()
    def snapshot(self, now=None):
        cutoff = (time.time() if now is None else now) - self.duration_seconds
        with self._lock: return tuple(frame for frame in self._frames if frame.captured_at >= cutoff)
    def clear(self):
        with self._lock: self._frames.clear()


class FramePublisher:
    """Immutable latest-JPEG fan-out; subscriber reads never consume data."""
    def __init__(self): self._condition, self._jpeg, self._sequence, self._closed = threading.Condition(), None, 0, False
    def publish(self, jpeg):
        with self._condition:
            self._jpeg, self._sequence = bytes(jpeg), self._sequence + 1
            self._condition.notify_all()
    def wait_after(self, sequence, timeout=15.0):
        with self._condition:
            self._condition.wait_for(lambda: self._sequence > sequence or self._closed, timeout)
            return self._sequence, self._jpeg
    def close(self):
        with self._condition:
            self._closed, self._jpeg = True, None
            self._condition.notify_all()


class PyAvFrameSource:
    def __init__(self, url, timeout):
        import av
        self._container = av.open(url, options={"rtsp_transport": "tcp", "stimeout": str(int(timeout * 1_000_000))}, timeout=timeout)
    def frames(self):
        for frame in self._container.decode(video=0): yield frame.to_ndarray(format="bgr24")
    def close(self): self._container.close()


def _owned_frame(frame):
    value = np.ascontiguousarray(frame).copy()
    value.setflags(write=False)
    return value


class CameraWorker:
    def __init__(self, camera, engine, matcher, fps, timeout, source_factory=PyAvFrameSource,
                 pre_roll_seconds=5.0, recognition_window_seconds=10.0,
                 max_session_seconds=60.0, pre_roll_max_frames=150,
                 detector_factory=create_face_detector, frame_processor=process_frame, clock=time.time,
                 preview_fps=20.0):
        self.camera, self.engine, self.matcher = camera, engine, matcher
        self.period, self.preview_period = 1 / fps, 1 / preview_fps
        self.timeout, self.source_factory = timeout, source_factory
        self.pre_roll = PreRollBuffer(pre_roll_seconds, pre_roll_max_frames)
        self.window_seconds, self.max_session_seconds = recognition_window_seconds, max_session_seconds
        self.detector_factory, self.frame_processor, self.clock = detector_factory, frame_processor, clock
        self.publisher, self.latest = FramePublisher(), LatestFrame()
        self._stop, self._capture_thread, self._session_thread = threading.Event(), None, None
        self._source, self._dedicated_source = None, None
        self._lock = threading.RLock()
        self._sequence = self._source_generation = self._session_generation = 0
        self._session_state, self._stream_mode = RecognitionSessionState.IDLE, RecognitionStreamMode.NONE
        self._session_started_at = self._deadline = self._maximum_deadline = None
        self._recognition_error = None
        self._durable_session_id = None
        self._lifecycle_callback = self._result_callback = None
        self._session_face_count = 0
        self._status = CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)

    @property
    def status(self):
        with self._lock:
            return CameraStatus(**(self._status.__dict__ | {
                "recognition_session_state": self._session_state,
                "recognition_stream_mode": self._stream_mode,
                "recognition_session_started_at": self._session_started_at,
                "recognition_deadline": self._deadline,
                "recognition_maximum_deadline": self._maximum_deadline,
                "recognition_error": self._recognition_error,
            }))
    def _set(self, **changes):
        with self._lock: self._status = CameraStatus(**(self._status.__dict__ | changes))
    def _session_status(self, extended):
        return RecognitionSessionStatus(self.camera.id, self._session_state, self._stream_mode,
                                        self._session_started_at, self._deadline,
                                        self._maximum_deadline, extended)

    def start(self):
        self._set(running=True, connection_state=ConnectionState.CONNECTING)
        self._capture_thread = threading.Thread(target=self._capture_loop, name=f"camera-{self.camera.id}-lightweight", daemon=True)
        self._capture_thread.start()

    def _capture_loop(self):
        attempt = 0
        while not self._stop.is_set():
            source = None
            try:
                self._set(connection_state=ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RETRYING,
                          retry_attempt=attempt, next_retry_at=None, last_error=None)
                source = self.source_factory(self.camera.url, self.timeout)
                with self._lock:
                    self._source, self._source_generation = source, self._source_generation + 1
                    generation = self._source_generation
                attempt, last_sample, last_preview = 0, 0.0, 0.0
                for frame in source.frames():
                    if self._stop.is_set(): break
                    now = self.clock()
                    if now - last_preview >= self.preview_period:
                        last_preview = now
                        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
                        if ok: self.publisher.publish(jpeg.tobytes())
                    if now - last_sample >= self.period:
                        last_sample = now
                        with self._lock:
                            self._sequence += 1
                            sequence = self._sequence
                        record = FrameRecord(self.camera.id, sequence, generation, now, "lightweight", _owned_frame(frame))
                        self.pre_roll.put(record); self.latest.put(record)
                    self._set(connection_state=ConnectionState.LIVE, last_frame_at=now)
                if not self._stop.is_set(): raise OSError("RTSP stream ended")
            except Exception as exc:
                if self._stop.is_set(): break
                message = str(exc).lower()
                state = ConnectionState.AUTHENTICATION_FAILED if any(x in message for x in ("401", "unauthorized", "authentication")) else ConnectionState.OFFLINE
                attempt += 1; delay = min(30.0, 2 ** min(attempt, 5))
                self._set(connection_state=state, last_error="authentication" if state == ConnectionState.AUTHENTICATION_FAILED else "unavailable",
                          retry_attempt=attempt, next_retry_at=self.clock() + delay)
                self._stop.wait(delay)
            finally:
                with self._lock: self._source = None
                if source:
                    try: source.close()
                    except Exception: pass
                self.latest.clear(); self.pre_roll.clear()
        self.publisher.close()
        self._set(running=False, connection_state=ConnectionState.STOPPED, next_retry_at=None)

    def trigger_recognition(self, session_id=None, lifecycle_callback=None, result_callback=None):
        now = self.clock()
        with self._lock:
            if self._stop.is_set() or not self._status.running or self._session_state == RecognitionSessionState.STOPPING:
                raise RuntimeError(f"Camera {self.camera.id} is not running.")
            if self._session_state in (RecognitionSessionState.STARTING, RecognitionSessionState.ACTIVE):
                if session_id is not None and self._durable_session_id not in (None, session_id):
                    raise RuntimeError("The active runtime session has a different durable session.")
                self._deadline = min(now + self.window_seconds, self._maximum_deadline)
                return self._session_status(True)
            self._session_generation += 1
            generation = self._session_generation
            self._session_state, self._stream_mode = RecognitionSessionState.STARTING, RecognitionStreamMode.NONE
            self._session_started_at = now
            self._deadline = min(now + self.window_seconds, now + self.max_session_seconds)
            self._maximum_deadline = now + self.max_session_seconds
            self._recognition_error = None
            self._durable_session_id = session_id
            self._lifecycle_callback, self._result_callback = lifecycle_callback, result_callback
            self._session_face_count = 0
            snapshot = self.pre_roll.snapshot(now)
            lightweight_boundary = self._sequence
            self._session_thread = threading.Thread(
                target=self._recognition_loop, args=(generation, snapshot, lightweight_boundary), daemon=True
            )
            self._session_thread.start()
            return self._session_status(False)

    def _valid_session(self, generation):
        with self._lock: return generation == self._session_generation and not self._stop.is_set()

    def _connect_dedicated(self, generation, result, ready):
        source = None
        try:
            source = self.source_factory(self.camera.recognition_url, self.timeout)
            with result["lock"], self._lock:
                valid = generation == self._session_generation and self._session_state in (
                    RecognitionSessionState.STARTING, RecognitionSessionState.ACTIVE
                )
                if result["accepting"] and valid:
                    result["source"], source = source, None
        except Exception as exc:
            with result["lock"]: result["error"] = exc
        finally:
            if source:
                try: source.close()
                except Exception: pass
            ready.set()

    def _recognize(self, record, detector):
        frame = record.frame.copy()
        faces = self.frame_processor(frame, detector, self.engine, {"detector": {"min_conf": .5, "pad_ratio": .15}})
        labeled = [(face, self.matcher.match(face.embedding)) for face in faces]
        self._session_face_count += len(labeled)
        if labeled and self._result_callback and self._durable_session_id is not None:
            captured = datetime.fromtimestamp(record.captured_at, timezone.utc).isoformat()
            results = []
            for face, label in labeled:
                crop_ok, crop = cv2.imencode(".jpg", face.aligned_rgb[..., ::-1],
                                             [cv2.IMWRITE_JPEG_QUALITY, 90])
                results.append({
                    "capture_timestamp": captured,
                    "frame_sequence": record.sequence,
                    "source_role": record.source_role,
                    "outcome": "recognized" if label.identity_id is not None else "unrecognized_face",
                    "identity_id": label.identity_id,
                    "similarity": label.similarity,
                    "detection_confidence": float(face.score),
                    "displayed_label": label.display_name,
                    "image_bytes": crop.tobytes() if crop_ok else None,
                })
            self._result_callback(self._durable_session_id, results)
    def _recognition_loop(self, generation, snapshot, lightweight_boundary):
        detector = dedicated_decoder = None
        with self._lock:
            durable_session_id = self._durable_session_id
            lifecycle_callback = self._lifecycle_callback
            result_callback = self._result_callback
        dedicated_done, connector_ready = threading.Event(), threading.Event()
        connector_result = {"lock": threading.Lock(), "accepting": True}
        connection_deadline = self._session_started_at + self.timeout
        failed = None
        try:
            detector = self.detector_factory(.5)
            if self.camera.recognition_url:
                threading.Thread(target=self._connect_dedicated, args=(generation, connector_result, connector_ready), daemon=True).start()
            else:
                with self._lock: self._stream_mode = RecognitionStreamMode.SHARED_LIGHTWEIGHT
            last_lightweight = lightweight_boundary
            for record in snapshot:
                if not self._valid_session(generation) or self.clock() >= self._deadline: return
                self._recognize(record, detector)
            dedicated_latest = LatestFrame()
            if self.camera.recognition_url:
                connector_ready.wait(max(0.0, connection_deadline - self.clock()))
                with connector_result["lock"]:
                    connector_result["accepting"] = False
                    source = connector_result.pop("source", None)
                if source is None:
                    with self._lock:
                        self._stream_mode = RecognitionStreamMode.LIGHTWEIGHT_FALLBACK
                        self._recognition_error = "dedicated stream unavailable"
                else:
                    with self._lock:
                        if generation != self._session_generation: return
                        self._dedicated_source, self._stream_mode = source, RecognitionStreamMode.DEDICATED
                    def decode():
                        sequence = 0
                        try:
                            for frame in source.frames():
                                if not self._valid_session(generation): break
                                sequence += 1
                                dedicated_latest.put(FrameRecord(self.camera.id, sequence, 1, self.clock(), "dedicated", _owned_frame(frame)))
                        except Exception: pass
                        finally:
                            try: source.close()
                            except Exception: pass
                            dedicated_done.set()
                    dedicated_decoder = threading.Thread(target=decode, daemon=True); dedicated_decoder.start()
            with self._lock:
                if generation == self._session_generation: self._session_state = RecognitionSessionState.ACTIVE
            if lifecycle_callback and durable_session_id is not None:
                lifecycle_callback(
                    durable_session_id, "active", self._stream_mode.value, None
                )
            last_dedicated = 0
            while self._valid_session(generation):
                with self._lock: deadline, mode = self._deadline, self._stream_mode
                remaining = deadline - self.clock()
                if remaining <= 0: break
                if mode == RecognitionStreamMode.DEDICATED:
                    sequence, record = dedicated_latest.get_after(last_dedicated, min(self.period, remaining))
                    if dedicated_done.is_set() and sequence == last_dedicated:
                        with self._lock:
                            self._stream_mode = RecognitionStreamMode.LIGHTWEIGHT_FALLBACK
                            self._recognition_error = "dedicated stream disconnected"
                        continue
                    if sequence == last_dedicated or record is None: continue
                    last_dedicated = sequence
                else:
                    sequence, record = self.latest.get_after(last_lightweight, min(self.period, remaining))
                    if sequence == last_lightweight or record is None: continue
                    last_lightweight = sequence
                self._recognize(record, detector)
        except Exception as exc:
            failed = str(exc)
        finally:
            with connector_result["lock"]:
                connector_result["accepting"] = False
                pending_source = connector_result.pop("source", None)
            if pending_source:
                try: pending_source.close()
                except Exception: pass
            with self._lock:
                if generation == self._session_generation: self._session_state = RecognitionSessionState.STOPPING
                self._dedicated_source = None
            if dedicated_decoder: dedicated_decoder.join(self.timeout)
            if detector:
                try: detector.close()
                except Exception: pass
            with self._lock:
                session_face_count = self._session_face_count
                if generation == self._session_generation:
                    self._session_state, self._stream_mode = RecognitionSessionState.IDLE, RecognitionStreamMode.NONE
                    self._session_started_at = self._deadline = self._maximum_deadline = None
            if durable_session_id is not None and lifecycle_callback:
                if failed:
                    if result_callback:
                        result_callback(durable_session_id, [{
                            "capture_timestamp": datetime.now(timezone.utc).isoformat(),
                            "outcome": "processing_error", "error_code": "processing_error",
                            "error_message": failed,
                        }])
                    lifecycle_callback(durable_session_id, "failed", None, failed)
                else:
                    if session_face_count == 0 and result_callback:
                        result_callback(durable_session_id, [{
                            "capture_timestamp": datetime.now(timezone.utc).isoformat(),
                            "outcome": "no_face",
                        }])
                    lifecycle_callback(durable_session_id, "completed", None, None)

    def request_stop(self):
        self._stop.set()
        self.publisher.close()
        with self._lock:
            self._session_generation += 1

    def finish_stop(self, timeout):
        deadline = time.monotonic() + timeout
        for thread in (self._session_thread, self._capture_thread):
            if thread: thread.join(max(0.0, deadline - time.monotonic()))
            if thread and thread.is_alive(): raise TimeoutError(f"Camera {self.camera.id} did not stop within the cleanup deadline.")

    def stop(self, timeout):
        self.request_stop()
        self.finish_stop(timeout)


class CameraManager:
    def __init__(self, repository, engine, matcher, fps=2.0, timeout=8.0, cleanup_timeout=10.0, max_active=4,
                 worker_factory=CameraWorker, pre_roll_seconds=5.0, recognition_window_seconds=10.0,
                 max_session_seconds=60.0, pre_roll_max_frames=150, preview_fps=20.0):
        self.repository, self.engine, self.matcher = repository, engine, matcher
        self.fps, self.timeout, self.cleanup_timeout, self.max_active, self.worker_factory = fps, timeout, cleanup_timeout, max_active, worker_factory
        self.pre_roll_seconds, self.recognition_window_seconds = pre_roll_seconds, recognition_window_seconds
        self.max_session_seconds, self.pre_roll_max_frames = max_session_seconds, pre_roll_max_frames
        self.preview_fps = preview_fps
        self._workers, self._cleanup_threads, self._lock, self._shutting_down = {}, set(), threading.RLock(), False
    def start(self, camera_id):
        with self._lock:
            if self._shutting_down: raise RuntimeError("Camera manager is shutting down.")
            if camera_id in self._workers: return self._workers[camera_id].status
            if len(self._workers) >= self.max_active: raise ActiveCameraLimitReached("The active-camera limit has been reached.")
            camera = self.repository.get(camera_id)
            try:
                worker = self.worker_factory(camera, self.engine, self.matcher, self.fps, self.timeout,
                    pre_roll_seconds=self.pre_roll_seconds, recognition_window_seconds=self.recognition_window_seconds,
                    max_session_seconds=self.max_session_seconds, pre_roll_max_frames=self.pre_roll_max_frames,
                    preview_fps=self.preview_fps)
            except TypeError:
                worker = self.worker_factory(camera, self.engine, self.matcher, self.fps, self.timeout)
            self._workers[camera_id] = worker; worker.start(); return worker.status
    def trigger_recognition(self, camera_id, **kwargs):
        self.repository.get(camera_id)
        with self._lock: worker = self._workers.get(camera_id)
        if worker is None: raise RuntimeError(f"Camera {camera_id} is not running.")
        return worker.trigger_recognition(**kwargs)
    def stop(self, camera_id):
        with self._lock: worker = self._workers.pop(camera_id, None)
        if worker:
            request_stop = getattr(worker, "request_stop", None)
            if request_stop:
                request_stop()
                cleanup = threading.Thread(
                    target=self._finish_stop, args=(worker,), name=f"camera-{camera_id}-cleanup", daemon=True
                )
                with self._lock: self._cleanup_threads.add(cleanup)
                cleanup.start()
            else:
                worker.stop(self.cleanup_timeout)
        camera = self.repository.get(camera_id)
        return CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)
    def _finish_stop(self, worker):
        current = threading.current_thread()
        try:
            worker.finish_stop(self.cleanup_timeout)
        except Exception:
            logger.exception("Camera %s cleanup did not complete cleanly.", worker.camera.id)
        finally:
            with self._lock: self._cleanup_threads.discard(current)
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
        with self._lock:
            self._shutting_down, workers = True, list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            try: worker.request_stop()
            except Exception: logger.exception("Could not signal camera %s to stop.", worker.camera.id)
        deadline = time.monotonic() + self.cleanup_timeout
        for worker in workers:
            try: worker.finish_stop(max(0.0, deadline - time.monotonic()))
            except Exception: logger.exception("Camera %s did not stop during shutdown.", worker.camera.id)
        with self._lock: cleanup_threads = list(self._cleanup_threads)
        for thread in cleanup_threads: thread.join(max(0.0, deadline - time.monotonic()))
