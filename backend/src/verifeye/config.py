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
        )

    def validate(self) -> None:
        if self.recognition_fps <= 0 or self.max_active_cameras < 1:
            raise ValueError("Recognition FPS and active-camera limit must be positive.")
        if not -1 <= self.similarity_threshold <= 1:
            raise ValueError("Similarity threshold must be between -1 and 1.")
