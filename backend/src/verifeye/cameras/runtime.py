"""Independent pre-roll capture and event-driven recognition runtimes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
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
UTC = timezone.utc


@dataclass(frozen=True)
class FrameRecord:
    camera_id: int
    sequence: int
    source_generation: int
    captured_at: float
    captured_monotonic: float
    source_role: str
    frame: np.ndarray


class LatestFrame:
    """Single-slot handoff; a reader never creates an accumulating queue."""
    def __init__(self):
        self._condition, self._value, self._sequence, self._closed = threading.Condition(), None, 0, False
    @property
    def sequence(self):
        with self._condition: return self._sequence
    def put(self, value):
        with self._condition:
            self._value = value
            self._sequence = getattr(value, "sequence", self._sequence + 1)
            self._condition.notify_all()
    def get_after(self, sequence, timeout=None):
        with self._condition:
            self._condition.wait_for(
                lambda: (self._sequence > sequence and self._value is not None) or self._closed, timeout
            )
            return self._sequence, self._value
    def clear(self):
        with self._condition:
            self._value = None; self._condition.notify_all()
    def close(self):
        with self._condition:
            self._closed, self._value = True, None; self._condition.notify_all()


class FramePublisher:
    """Temporary MJPEG compatibility publisher retained until hardware acceptance."""
    def __init__(self):
        self._condition, self._jpeg, self._sequence, self._closed = threading.Condition(), None, 0, False
    def publish(self, jpeg):
        with self._condition:
            self._jpeg, self._sequence = bytes(jpeg), self._sequence + 1; self._condition.notify_all()
    def wait_after(self, sequence, timeout=15.0):
        with self._condition:
            self._condition.wait_for(lambda: self._sequence > sequence or self._closed, timeout)
            return self._sequence, self._jpeg
    def close(self):
        with self._condition:
            self._closed, self._jpeg = True, None; self._condition.notify_all()


class PreRollBuffer:
    """Bounded decoded-frame history, evicted by monotonic age and queried by UTC."""
    def __init__(self, duration_seconds, max_frames):
        self.duration_seconds, self.max_frames = duration_seconds, max_frames
        self._frames, self._lock = deque(), threading.Lock()
    def put(self, record):
        cutoff = record.captured_monotonic - self.duration_seconds
        with self._lock:
            self._frames.append(record)
            while self._frames and (len(self._frames) > self.max_frames or self._frames[0].captured_monotonic < cutoff):
                self._frames.popleft()
    def snapshot_between(self, start_utc, end_utc):
        start = start_utc.timestamp() if isinstance(start_utc, datetime) else float(start_utc)
        end = end_utc.timestamp() if isinstance(end_utc, datetime) else float(end_utc)
        with self._lock: return tuple(frame for frame in self._frames if start <= frame.captured_at <= end)
    def clear(self):
        with self._lock: self._frames.clear()


class PyAvFrameSource:
    """Infrastructure decoder used only by capture components."""
    def __init__(self, url, timeout):
        import av
        self._container = av.open(url, options={"rtsp_transport": "tcp", "stimeout": str(int(timeout * 1_000_000))}, timeout=timeout)
    def frames(self):
        for frame in self._container.decode(video=0): yield frame.to_ndarray(format="bgr24")
    def close(self): self._container.close()


def _owned_frame(frame):
    value = np.ascontiguousarray(frame).copy(); value.setflags(write=False); return value


@dataclass(frozen=True)
class PreRollStatus:
    running: bool = False
    ready: bool = False
    last_frame_at: float | None = None
    error: str | None = None
    retry_attempt: int = 0
    next_retry_at: float | None = None


class PreRollCapture:
    """Sole continuous decoder of a camera's shared MediaMTX preview path.

    ``snapshot_between`` is the pre-roll API. ``latest_after`` exists only for
    shared-lightweight and lightweight-fallback recognition sessions.
    """
    def __init__(self, camera, url, fps, timeout, duration_seconds=5.0, max_frames=150,
                 preview_fps=20.0,
                 source_factory=PyAvFrameSource, wall_clock=time.time, monotonic=time.monotonic,
                 reconnect_delay=lambda attempt: min(30.0, 2 ** min(attempt, 5))):
        self.camera, self.url, self.period, self.timeout = camera, url, 1 / fps, timeout
        self.buffer, self.latest, self.publisher = PreRollBuffer(duration_seconds, max_frames), LatestFrame(), FramePublisher()
        self.preview_period = 1 / preview_fps
        self.source_factory, self.wall_clock, self.monotonic = source_factory, wall_clock, monotonic
        self.reconnect_delay = reconnect_delay
        self._stop, self._thread, self._source, self._lock = threading.Event(), None, None, threading.RLock()
        self._sequence, self._generation, self._status = 0, 0, PreRollStatus()
    @property
    def status(self):
        with self._lock: return self._status
    @property
    def sequence(self): return self.latest.sequence
    def _set(self, **changes):
        with self._lock: self._status = PreRollStatus(**(self._status.__dict__ | changes))
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive(): return
            self._status = PreRollStatus(running=True)
            self._thread = threading.Thread(target=self._run, daemon=True, name=f"camera-{self.camera.id}-preroll")
            self._thread.start()
    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            source = None
            try:
                self._set(ready=False, error=None, retry_attempt=attempt, next_retry_at=None)
                source = self.source_factory(self.url, self.timeout)
                with self._lock:
                    self._source = source; self._generation += 1; generation = self._generation
                attempt, last_sample, last_preview = 0, float("-inf"), float("-inf")
                for frame in source.frames():
                    if self._stop.is_set(): break
                    now_mono = self.monotonic()
                    if now_mono - last_preview >= self.preview_period:
                        last_preview = now_mono
                        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
                        if ok: self.publisher.publish(jpeg.tobytes())
                    if now_mono - last_sample < self.period: continue
                    last_sample = now_mono
                    with self._lock: self._sequence += 1; sequence = self._sequence
                    now_utc = self.wall_clock()
                    record = FrameRecord(self.camera.id, sequence, generation, now_utc, now_mono, "lightweight", _owned_frame(frame))
                    self.buffer.put(record); self.latest.put(record)
                    self._set(ready=True, last_frame_at=now_utc, retry_attempt=0)
                if not self._stop.is_set(): raise OSError("RTSP stream ended")
            except Exception as exc:
                if self._stop.is_set(): break
                attempt += 1; delay = self.reconnect_delay(attempt)
                self._set(ready=False, error=str(exc) or "unavailable", retry_attempt=attempt,
                          next_retry_at=self.wall_clock() + delay)
                self._stop.wait(delay)
            finally:
                with self._lock:
                    if self._source is source: self._source = None
                if source:
                    try: source.close()
                    except Exception: pass
                self.latest.clear()
        self.latest.close(); self.publisher.close(); self.buffer.clear(); self._set(running=False, ready=False, next_retry_at=None)
    def snapshot_between(self, start_utc, end_utc): return self.buffer.snapshot_between(start_utc, end_utc)
    def latest_after(self, sequence, timeout=None): return self.latest.get_after(sequence, timeout)
    def request_stop(self):
        self._stop.set()
        # The decoder thread owns the PyAV container.  Closing it here can race
        # with ``decode()`` in FFmpeg native code and take down the whole API
        # process.  MediaMTX path removal and the decoder timeout wake the
        # thread; ``_run`` then closes the source from that same thread.
        self.latest.close(); self.publisher.close()
    def finish_stop(self, timeout):
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive(): raise TimeoutError(f"Camera {self.camera.id} pre-roll decoder did not stop.")
    def stop(self, timeout): self.request_stop(); self.finish_stop(timeout)


class RecognitionCapture:
    """One disposable, one-attempt decoder for a distinct recognition path."""
    def __init__(self, camera_id, url, fps, timeout, source_factory=PyAvFrameSource,
                 wall_clock=time.time, monotonic=time.monotonic):
        self.camera_id, self.url, self.period, self.timeout = camera_id, url, 1 / fps, timeout
        self.source_factory, self.wall_clock, self.monotonic = source_factory, wall_clock, monotonic
        self.latest, self.done, self.failed, self.error = LatestFrame(), threading.Event(), False, None
        self._source, self._thread, self._stop = None, None, threading.Event()
    def start(self):
        try: self._source = self.source_factory(self.url, self.timeout)
        except Exception as exc:
            self.failed, self.error = True, str(exc) or "dedicated stream unavailable"
            self.done.set(); self.latest.close(); return False
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"camera-{self.camera_id}-recognition-capture")
        self._thread.start(); return True
    def _run(self):
        sequence, last_sample = 0, float("-inf")
        try:
            for frame in self._source.frames():
                if self._stop.is_set(): break
                now_mono = self.monotonic()
                if now_mono - last_sample < self.period: continue
                last_sample = now_mono; sequence += 1
                self.latest.put(FrameRecord(self.camera_id, sequence, 1, self.wall_clock(), now_mono, "dedicated", _owned_frame(frame)))
            if not self._stop.is_set(): raise OSError("dedicated stream ended")
        except Exception as exc:
            if not self._stop.is_set(): self.failed, self.error = True, str(exc) or "dedicated stream disconnected"
        finally:
            if self._source:
                try: self._source.close()
                except Exception: pass
            self.done.set(); self.latest.close()
    def latest_after(self, sequence, timeout=None): return self.latest.get_after(sequence, timeout)
    def stop(self, timeout):
        self._stop.set(); self.latest.close()
        # ``frames()`` and ``close()`` both enter PyAV/FFmpeg native code.  Closing
        # the container here while the capture thread is decoding can deadlock.
        # The capture thread exclusively owns source cleanup in ``_run``; this
        # caller only signals it and waits for the decoder's configured timeout.
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError(
                    f"Camera {self.camera_id} recognition decoder did not stop within {timeout:g} seconds."
                )


class InferenceSession:
    def __init__(self, engine, matcher, detector, frame_processor):
        self.engine, self.matcher, self.detector, self.frame_processor = engine, matcher, detector, frame_processor
    def process(self, record):
        faces = self.frame_processor(record.frame.copy(), self.detector, self.engine,
                                     {"detector": {"min_conf": .5, "pad_ratio": .15}})
        captured, results = datetime.fromtimestamp(record.captured_at, UTC).isoformat(), []
        for face in faces:
            label = self.matcher.match(face.embedding)
            crop_ok, crop = cv2.imencode(".jpg", face.aligned_rgb[..., ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
            results.append({"capture_timestamp": captured, "frame_sequence": record.sequence,
                "source_role": record.source_role,
                "outcome": "recognized" if label.identity_id is not None else "unrecognized_face",
                "identity_id": label.identity_id, "similarity": label.similarity,
                "detection_confidence": float(face.score), "displayed_label": label.display_name,
                "image_bytes": crop.tobytes() if crop_ok else None})
        return results
    def close(self):
        try: self.detector.close()
        except Exception: pass


class InferenceExecutor:
    """Own face detection and access to the process-wide ArcFace engine."""
    def __init__(self, engine, matcher, detector_factory=create_face_detector, frame_processor=process_frame):
        self.engine, self.matcher, self.detector_factory, self.frame_processor = engine, matcher, detector_factory, frame_processor
    def open_session(self):
        return InferenceSession(self.engine, self.matcher, self.detector_factory(.5), self.frame_processor)


class _RecognitionRuntime:
    def __init__(self, manager, camera, session_id, spec, preroll):
        self.manager, self.camera, self.session_id, self.preroll = manager, camera, session_id, preroll
        self.lock, self.state, self.mode = threading.RLock(), RecognitionSessionState.STARTING, RecognitionStreamMode.NONE
        self.started_utc, self.error, self.capture, self.thread = manager.wall_clock(), None, None, None
        self._apply_spec(spec)
    def _apply_spec(self, spec):
        now_utc, now_mono = self.manager.utc_now(), self.manager.monotonic()
        remaining = max(0.0, (spec["interval_end"] - now_utc).total_seconds())
        maximum = max(0.0, (spec["maximum_end"] - now_utc).total_seconds())
        with self.lock:
            self.spec, self.deadline_mono, self.maximum_mono = spec, now_mono + min(remaining, maximum), now_mono + maximum
    def extend(self, spec):
        now_utc, now_mono = self.manager.utc_now(), self.manager.monotonic()
        desired = now_mono + max(0.0, (spec["interval_end"] - now_utc).total_seconds())
        with self.lock:
            self.spec = spec
            # The hard cap was converted once when the runtime began. Recomputing
            # it after a wall-clock jump would move a monotonic safety boundary.
            self.deadline_mono = min(max(self.deadline_mono, desired), self.maximum_mono)
    def remaining(self):
        with self.lock: return max(0.0, self.deadline_mono - self.manager.monotonic())
    def specification(self):
        with self.lock: return dict(self.spec)
    def status(self, extended=False):
        with self.lock:
            now_utc, now_mono = self.manager.wall_clock(), self.manager.monotonic()
            return RecognitionSessionStatus(self.camera.id, self.state, self.mode, self.started_utc,
                now_utc + max(0.0, self.deadline_mono - now_mono),
                now_utc + max(0.0, self.maximum_mono - now_mono), extended)


class RecognitionSessionManager:
    """Own session reuse, monotonic timing, mode selection, and inference lifecycle."""
    def __init__(self, repository, cameras, media_source, inference, sink, recognition_fps=2.0,
                 timeout=8.0, capture_factory=RecognitionCapture, wall_clock=time.time,
                 monotonic=time.monotonic, utc_now=lambda: datetime.now(UTC)):
        self.repository, self.cameras, self.media_source, self.inference, self.sink = repository, cameras, media_source, inference, sink
        self.recognition_fps, self.timeout, self.capture_factory = recognition_fps, timeout, capture_factory
        self.wall_clock, self.monotonic, self.utc_now = wall_clock, monotonic, utc_now
        self._runtimes, self._lock, self._shutting_down = {}, threading.RLock(), False
    def request(self, camera_id, session_id):
        camera, spec = self.repository.get(camera_id), self.sink.session_spec(session_id)
        with self._lock:
            if self._shutting_down: raise RuntimeError("Recognition session manager is shutting down.")
            current = self._runtimes.get(camera_id)
            if current:
                if current.session_id != session_id: raise RuntimeError("A different recognition session is already active for this camera.")
                if current.state not in {RecognitionSessionState.STARTING, RecognitionSessionState.ACTIVE}:
                    raise RuntimeError("The recognition session is already stopping.")
                current.extend(spec); return current.status(True)
            preroll = self.cameras.preroll(camera_id)
            if preroll is None: raise RuntimeError(f"Camera {camera_id} is not running.")
            runtime = _RecognitionRuntime(self, camera, session_id, spec, preroll)
            self._runtimes[camera_id] = runtime
            runtime.thread = threading.Thread(target=self._run, args=(runtime,), daemon=True,
                                              name=f"camera-{camera_id}-recognition-session")
            runtime.thread.start(); return runtime.status(False)
    def _run(self, runtime):
        inference_session, face_count, failed = None, 0, None
        try:
            inference_session = self.inference.open_session()
            endpoint = self.media_source.recognition_url(runtime.camera)
            if endpoint is None:
                runtime.mode = RecognitionStreamMode.SHARED_LIGHTWEIGHT
            else:
                runtime.capture = self.capture_factory(runtime.camera.id, endpoint.url, self.recognition_fps, self.timeout)
                if runtime.capture.start(): runtime.mode = RecognitionStreamMode.DEDICATED
                else:
                    runtime.mode, runtime.error = RecognitionStreamMode.LIGHTWEIGHT_FALLBACK, runtime.capture.error or "dedicated stream unavailable"
                    runtime.capture.stop(self.timeout); runtime.capture = None
            runtime.state = RecognitionSessionState.ACTIVE
            self.sink.active(runtime.session_id, runtime.mode.value, runtime.error)
            spec, boundary = runtime.specification(), runtime.preroll.sequence
            for record in runtime.preroll.snapshot_between(spec["interval_start"], spec["trigger_at"]):
                if runtime.remaining() <= 0: break
                results = inference_session.process(record); face_count += len(results)
                if results: self.sink.results(runtime.session_id, results)
            dedicated_sequence, lightweight_sequence = 0, boundary
            while (remaining := runtime.remaining()) > 0:
                if runtime.mode == RecognitionStreamMode.DEDICATED:
                    sequence, record = runtime.capture.latest_after(dedicated_sequence, min(1 / self.recognition_fps, remaining))
                    if runtime.capture.failed or (runtime.capture.done.is_set() and sequence == dedicated_sequence):
                        runtime.error = runtime.capture.error or "dedicated stream disconnected"
                        runtime.capture.stop(self.timeout); runtime.capture = None
                        runtime.mode = RecognitionStreamMode.LIGHTWEIGHT_FALLBACK
                        self.sink.active(runtime.session_id, runtime.mode.value, runtime.error)
                        lightweight_sequence = runtime.preroll.sequence
                        continue
                    if sequence == dedicated_sequence or record is None: continue
                    dedicated_sequence = sequence
                else:
                    sequence, record = runtime.preroll.latest_after(lightweight_sequence, min(1 / self.recognition_fps, remaining))
                    if sequence == lightweight_sequence or record is None: continue
                    lightweight_sequence = sequence
                    if runtime.mode == RecognitionStreamMode.LIGHTWEIGHT_FALLBACK:
                        record = replace(record, source_role="lightweight_fallback")
                results = inference_session.process(record); face_count += len(results)
                if results: self.sink.results(runtime.session_id, results)
        except Exception as exc:
            failed = str(exc) or "processing error"
            self.sink.results(runtime.session_id, [{"capture_timestamp": datetime.now(UTC).isoformat(),
                "outcome": "processing_error", "error_code": "processing_error", "error_message": failed}])
        finally:
            runtime.state = RecognitionSessionState.STOPPING
            cleanup_error = None
            try:
                if runtime.capture:
                    runtime.capture.stop(self.timeout)
            except Exception as exc:
                cleanup_error = str(exc) or "recognition capture cleanup failed"
                logger.exception(
                    "Recognition capture cleanup failed for camera %s session %s.",
                    runtime.camera.id, runtime.session_id,
                )
            try:
                if not failed and face_count == 0:
                    self.sink.results(runtime.session_id, [{"capture_timestamp": datetime.now(UTC).isoformat(), "outcome": "no_face"}])
                # Durable finalization must happen before optional inference cleanup:
                # a native cleanup stall must never leave the session active.
                self.sink.terminal(runtime.session_id, failed or cleanup_error)
            finally:
                try:
                    if inference_session: inference_session.close()
                except Exception:
                    logger.exception(
                        "Recognition inference cleanup failed for camera %s session %s.",
                        runtime.camera.id, runtime.session_id,
                    )
                runtime.state, runtime.mode = RecognitionSessionState.IDLE, RecognitionStreamMode.NONE
                with self._lock:
                    if self._runtimes.get(runtime.camera.id) is runtime: del self._runtimes[runtime.camera.id]
    def status(self, camera_id):
        with self._lock: runtime = self._runtimes.get(camera_id)
        return runtime.status() if runtime else RecognitionSessionStatus(camera_id, RecognitionSessionState.IDLE,
            RecognitionStreamMode.NONE, None, None, None, False)
    def error(self, camera_id):
        with self._lock: runtime = self._runtimes.get(camera_id)
        return runtime.error if runtime else None
    def stop(self, camera_id, timeout=10):
        with self._lock: runtime = self._runtimes.get(camera_id)
        if runtime:
            with runtime.lock: runtime.deadline_mono = self.monotonic()
            if runtime.thread: runtime.thread.join(timeout)
    def shutdown(self, timeout=10):
        with self._lock: self._shutting_down = True; camera_ids = list(self._runtimes)
        for camera_id in camera_ids: self.stop(camera_id, timeout)


class CameraManager:
    """Own media provisioning and exactly one pre-roll decoder per enabled camera."""
    def __init__(self, repository, media_source, pre_roll_fps=2.0, timeout=8.0,
                 cleanup_timeout=10.0, max_active=4, capture_factory=PreRollCapture,
                 pre_roll_seconds=5.0, pre_roll_max_frames=150, preview_fps=20.0):
        self.repository, self.media_source, self.pre_roll_fps, self.timeout = repository, media_source, pre_roll_fps, timeout
        self.cleanup_timeout, self.max_active, self.capture_factory = cleanup_timeout, max_active, capture_factory
        self.pre_roll_seconds, self.pre_roll_max_frames, self.preview_fps, self.sessions = pre_roll_seconds, pre_roll_max_frames, preview_fps, None
        self._captures, self._cleanup_threads, self._lock, self._shutting_down = {}, set(), threading.RLock(), False
    def bind_sessions(self, sessions): self.sessions = sessions
    def start(self, camera_id):
        with self._lock:
            if self._shutting_down: raise RuntimeError("Camera manager is shutting down.")
            if camera_id in self._captures: return self.status(camera_id)
            if len(self._captures) >= self.max_active: raise ActiveCameraLimitReached("The active-camera limit has been reached.")
            camera = self.repository.get(camera_id); self.media_source.provision(camera)
            capture = self.capture_factory(camera, self.media_source.preroll_url(camera_id).url,
                self.pre_roll_fps, self.timeout, duration_seconds=self.pre_roll_seconds,
                max_frames=self.pre_roll_max_frames, preview_fps=self.preview_fps)
            self._captures[camera_id] = capture; capture.start()
        return self.status(camera_id)
    def preroll(self, camera_id):
        with self._lock: return self._captures.get(camera_id)
    def publisher(self, camera_id):
        with self._lock: capture = self._captures.get(camera_id)
        return capture.publisher if capture else None
    def stop(self, camera_id):
        if self.sessions: self.sessions.stop(camera_id, self.cleanup_timeout)
        with self._lock: capture = self._captures.pop(camera_id, None)
        if capture:
            capture.request_stop()
            cleanup = threading.Thread(target=self._finish_stop, args=(capture,), daemon=True, name=f"camera-{camera_id}-cleanup")
            with self._lock: self._cleanup_threads.add(cleanup)
            cleanup.start()
        self.media_source.remove(camera_id)
        camera = self.repository.get(camera_id)
        return CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)
    def _finish_stop(self, capture):
        current = threading.current_thread()
        try: capture.finish_stop(self.cleanup_timeout)
        except Exception: logger.exception("Camera %s pre-roll cleanup failed.", capture.camera.id)
        finally:
            with self._lock: self._cleanup_threads.discard(current)
    def restart(self, camera_id):
        if self.sessions: self.sessions.stop(camera_id, self.cleanup_timeout)
        with self._lock: capture = self._captures.pop(camera_id, None)
        if capture:
            capture.request_stop(); capture.finish_stop(self.cleanup_timeout)
        self.media_source.remove(camera_id)
        return self.start(camera_id)
    def delete(self, camera_id):
        if self.sessions: self.sessions.stop(camera_id, self.cleanup_timeout)
        with self._lock: capture = self._captures.pop(camera_id, None)
        if capture:
            capture.request_stop(); capture.finish_stop(self.cleanup_timeout)
        self.media_source.remove(camera_id)
        self.repository.delete(camera_id)
    def status(self, camera_id):
        camera = self.repository.get(camera_id)
        with self._lock: capture = self._captures.get(camera_id)
        if capture is None: return CameraStatus(camera.id, camera.enabled, False, ConnectionState.STOPPED)
        state, media_ready = capture.status, self.media_source.media_ready(camera_id)
        connection = (ConnectionState.LIVE if state.ready and media_ready else
                      ConnectionState.RETRYING if not media_ready or state.retry_attempt else
                      ConnectionState.CONNECTING)
        session = self.sessions.status(camera_id) if self.sessions else None
        return CameraStatus(camera.id, camera.enabled, state.running, connection,
            last_frame_at=state.last_frame_at, last_error=state.error, retry_attempt=state.retry_attempt,
            next_retry_at=state.next_retry_at,
            recognition_session_state=session.state if session else RecognitionSessionState.IDLE,
            recognition_stream_mode=session.stream_mode if session else RecognitionStreamMode.NONE,
            recognition_session_started_at=session.started_at if session else None,
            recognition_deadline=session.deadline if session else None,
            recognition_maximum_deadline=session.maximum_deadline if session else None,
            recognition_error=self.sessions.error(camera_id) if self.sessions else None,
            media_ready=media_ready, pre_roll_ready=state.ready,
            pre_roll_last_frame_at=state.last_frame_at, pre_roll_error=state.error)
    def start_enabled(self):
        for camera in self.repository.list():
            if camera.enabled: self.start(camera.id)
    def shutdown(self):
        if self.sessions: self.sessions.shutdown(self.cleanup_timeout)
        with self._lock: self._shutting_down, captures = True, list(self._captures.values()); self._captures.clear()
        for capture in captures: capture.request_stop()
        deadline = time.monotonic() + self.cleanup_timeout
        for capture in captures:
            try: capture.finish_stop(max(0.0, deadline - time.monotonic()))
            except Exception: logger.exception("Camera %s did not stop during shutdown.", capture.camera.id)
        with self._lock: cleanup = list(self._cleanup_threads)
        for thread in cleanup: thread.join(max(0.0, deadline - time.monotonic()))
