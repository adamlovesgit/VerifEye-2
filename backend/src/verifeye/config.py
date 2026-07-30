"""Validated process configuration; no web-framework dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database: Path
    upload_dir: Path
    model_path: Path
    camera_key: str
    recognition_fps: float = 2.0
    similarity_threshold: float = 0.40
    frame_freshness_seconds: float = 5.0
    rtsp_timeout_seconds: float = 8.0
    cleanup_timeout_seconds: float = 10.0
    max_active_cameras: int = 4
    pre_roll_seconds: float = 5.0
    recognition_window_seconds: float = 10.0
    max_recognition_session_seconds: float = 60.0
    pre_roll_max_frames: int = 150
    event_screenshot_dir: Path | None = None
    event_max_age_seconds: float = 86400.0
    event_future_skew_seconds: float = 300.0
    event_dispatch_lease_seconds: float = 30.0
    event_dispatch_max_attempts: int = 5
    sqlite_busy_timeout_ms: int = 5000

    @classmethod
    def from_environment(cls) -> "Settings":
        backend = Path(__file__).resolve().parents[2]
        return cls(
            database=Path(os.getenv("VERIFEYE_DATABASE", backend / "data" / "verifeye.db")),
            upload_dir=Path(os.getenv("VERIFEYE_UPLOAD_DIR", backend / "data" / "enrollments")),
            model_path=Path(os.getenv("VERIFEYE_MODEL_PATH", Path.home() / ".insightface/models/buffalo_l/w600k_r50.onnx")),
            camera_key=os.getenv("VERIFEYE_CAMERA_KEY", ""),
            recognition_fps=float(os.getenv("VERIFEYE_RECOGNITION_FPS", "2")),
            similarity_threshold=float(os.getenv("VERIFEYE_SIMILARITY_THRESHOLD", "0.40")),
            frame_freshness_seconds=float(os.getenv("VERIFEYE_FRAME_FRESHNESS_SECONDS", "5")),
            rtsp_timeout_seconds=float(os.getenv("VERIFEYE_RTSP_TIMEOUT_SECONDS", "8")),
            cleanup_timeout_seconds=float(os.getenv("VERIFEYE_CLEANUP_TIMEOUT_SECONDS", "10")),
            max_active_cameras=int(os.getenv("VERIFEYE_MAX_ACTIVE_CAMERAS", "4")),
            pre_roll_seconds=float(os.getenv("VERIFEYE_PRE_ROLL_SECONDS", "5")),
            recognition_window_seconds=float(os.getenv("VERIFEYE_RECOGNITION_WINDOW_SECONDS", "10")),
            max_recognition_session_seconds=float(os.getenv("VERIFEYE_MAX_RECOGNITION_SESSION_SECONDS", "60")),
            pre_roll_max_frames=int(os.getenv("VERIFEYE_PRE_ROLL_MAX_FRAMES", "150")),
            event_screenshot_dir=Path(os.getenv("VERIFEYE_EVENT_SCREENSHOT_DIR", backend / "data" / "events")),
            event_max_age_seconds=float(os.getenv("VERIFEYE_EVENT_MAX_AGE_SECONDS", "86400")),
            event_future_skew_seconds=float(os.getenv("VERIFEYE_EVENT_FUTURE_SKEW_SECONDS", "300")),
            event_dispatch_lease_seconds=float(os.getenv("VERIFEYE_EVENT_DISPATCH_LEASE_SECONDS", "30")),
            event_dispatch_max_attempts=int(os.getenv("VERIFEYE_EVENT_DISPATCH_MAX_ATTEMPTS", "5")),
            sqlite_busy_timeout_ms=int(os.getenv("VERIFEYE_SQLITE_BUSY_TIMEOUT_MS", "5000")),
        )

    def validate(self) -> None:
        if self.recognition_fps <= 0 or self.max_active_cameras < 1:
            raise ValueError("Recognition FPS and active-camera limit must be positive.")
        if self.pre_roll_seconds < 0 or self.recognition_window_seconds <= 0 or self.max_recognition_session_seconds <= 0:
            raise ValueError("Recognition session durations must be positive.")
        if self.recognition_window_seconds > self.max_recognition_session_seconds or self.pre_roll_max_frames < 1:
            raise ValueError("Recognition window must fit the session maximum and the pre-roll cap must be positive.")
        if not -1 <= self.similarity_threshold <= 1:
            raise ValueError("Similarity threshold must be between -1 and 1.")
        if self.event_max_age_seconds <= 0 or self.event_future_skew_seconds < 0:
            raise ValueError("Event timestamp bounds are invalid.")
        if self.event_dispatch_lease_seconds <= 0 or self.event_dispatch_max_attempts < 1:
            raise ValueError("Event dispatcher settings are invalid.")
        if self.sqlite_busy_timeout_ms < 0:
            raise ValueError("SQLite busy timeout must not be negative.")
