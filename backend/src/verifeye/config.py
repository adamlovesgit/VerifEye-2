"""Validated process configuration; no web-framework dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database: Path
    upload_dir: Path
    model_path: Path
    camera_key: str
    recognition_fps: float = 2.0
    pre_roll_fps: float = 2.0
    preview_fps: float = 20.0
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
    onvif_motion_cooldown_seconds: float = 20.0
    motion_no_face_retention_days: int = 7
    motion_unrecognized_retention_days: int = 30
    sqlite_busy_timeout_ms: int = 5000
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""
    smtp_tls_mode: str = "starttls"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    public_base_url: str = "http://127.0.0.1:8000"
    mediamtx_api_url: str = "http://127.0.0.1:9997"
    mediamtx_rtsp_url: str = "rtsp://127.0.0.1:8554"
    mediamtx_whep_url: str = "http://127.0.0.1:8889"
    public_whep_url: str = "http://127.0.0.1:8889"
    mediamtx_webrtc_udp_address: str = "127.0.0.1:8189"
    mediamtx_webrtc_additional_host: str = "127.0.0.1"
    mediamtx_runtime_dir: Path | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        backend = Path(__file__).resolve().parents[2]
        public_base_url = os.getenv("VERIFEYE_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        whep_listener_url = os.getenv("VERIFEYE_MEDIAMTX_WHEP_URL", "http://127.0.0.1:8889").rstrip("/")
        public_host = urlsplit(public_base_url).hostname or ""
        return cls(
            database=Path(os.getenv("VERIFEYE_DATABASE", backend / "data" / "verifeye.db")),
            upload_dir=Path(os.getenv("VERIFEYE_UPLOAD_DIR", backend / "data" / "enrollments")),
            model_path=Path(os.getenv("VERIFEYE_MODEL_PATH", Path.home() / ".insightface/models/buffalo_l/w600k_r50.onnx")),
            camera_key=os.getenv("VERIFEYE_CAMERA_KEY", ""),
            recognition_fps=float(os.getenv("VERIFEYE_RECOGNITION_FPS", "2")),
            pre_roll_fps=float(os.getenv("VERIFEYE_PRE_ROLL_FPS", os.getenv("VERIFEYE_RECOGNITION_FPS", "2"))),
            preview_fps=float(os.getenv("VERIFEYE_PREVIEW_FPS", "20")),
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
            onvif_motion_cooldown_seconds=float(os.getenv("VERIFEYE_ONVIF_MOTION_COOLDOWN_SECONDS", "20")),
            motion_no_face_retention_days=int(os.getenv("VERIFEYE_MOTION_NO_FACE_RETENTION_DAYS", "7")),
            motion_unrecognized_retention_days=int(os.getenv("VERIFEYE_MOTION_UNRECOGNIZED_RETENTION_DAYS", "30")),
            sqlite_busy_timeout_ms=int(os.getenv("VERIFEYE_SQLITE_BUSY_TIMEOUT_MS", "5000")),
            smtp_host=os.getenv("VERIFEYE_SMTP_HOST", ""),
            smtp_port=int(os.getenv("VERIFEYE_SMTP_PORT", "587")),
            smtp_username=os.getenv("VERIFEYE_SMTP_USERNAME", ""),
            smtp_password=os.getenv("VERIFEYE_SMTP_PASSWORD", ""),
            smtp_sender=os.getenv("VERIFEYE_SMTP_SENDER", ""),
            smtp_tls_mode=os.getenv("VERIFEYE_SMTP_TLS_MODE", "starttls").lower(),
            twilio_account_sid=os.getenv("VERIFEYE_TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("VERIFEYE_TWILIO_AUTH_TOKEN", ""),
            twilio_from_number=os.getenv("VERIFEYE_TWILIO_FROM_NUMBER", ""),
            public_base_url=public_base_url,
            mediamtx_api_url=os.getenv("VERIFEYE_MEDIAMTX_API_URL", "http://127.0.0.1:9997").rstrip("/"),
            mediamtx_rtsp_url=os.getenv("VERIFEYE_MEDIAMTX_RTSP_URL", "rtsp://127.0.0.1:8554").rstrip("/"),
            mediamtx_whep_url=whep_listener_url,
            public_whep_url=os.getenv("VERIFEYE_PUBLIC_WHEP_URL", whep_listener_url).rstrip("/"),
            mediamtx_webrtc_udp_address=os.getenv("VERIFEYE_MEDIAMTX_WEBRTC_UDP_ADDRESS", "127.0.0.1:8189"),
            mediamtx_webrtc_additional_host=os.getenv("VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST", public_host),
            mediamtx_runtime_dir=Path(os.getenv("VERIFEYE_MEDIAMTX_RUNTIME_DIR", backend / "data" / "media-router")),
        )

    def validate(self) -> None:
        if self.recognition_fps <= 0 or self.pre_roll_fps <= 0 or self.preview_fps <= 0 or self.max_active_cameras < 1:
            raise ValueError("Recognition FPS, preview FPS, and active-camera limit must be positive.")
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
        if self.onvif_motion_cooldown_seconds < 0:
            raise ValueError("ONVIF motion cooldown must not be negative.")
        if self.motion_no_face_retention_days < 1 or self.motion_unrecognized_retention_days < 1:
            raise ValueError("Motion-event retention must be at least one day.")
        if self.sqlite_busy_timeout_ms < 0:
            raise ValueError("SQLite busy timeout must not be negative.")
        if not 1 <= self.smtp_port <= 65535 or self.smtp_tls_mode not in {"starttls", "ssl", "none"}:
            raise ValueError("SMTP port or TLS mode is invalid.")
        public = urlsplit(self.public_base_url)
        whep = urlsplit(self.public_whep_url)
        if (public.scheme not in {"http", "https"} or not public.hostname or public.username or public.password
                or public.query or public.fragment or public.path not in {"", "/"}):
            raise ValueError("VERIFEYE_PUBLIC_BASE_URL must be an absolute HTTP(S) origin without credentials, path, query, or fragment.")
        if (whep.scheme not in {"http", "https"} or not whep.hostname or whep.username or whep.password
                or whep.query or whep.fragment):
            raise ValueError("VERIFEYE_PUBLIC_WHEP_URL must be an absolute HTTP(S) URL without credentials, query, or fragment.")
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if public.hostname not in loopback_hosts and public.scheme != "https":
            raise ValueError("VERIFEYE_PUBLIC_BASE_URL must use HTTPS outside loopback development.")
        if whep.hostname not in loopback_hosts and whep.scheme != "https":
            raise ValueError("VERIFEYE_PUBLIC_WHEP_URL must use HTTPS outside loopback development.")
        if whep.hostname.lower() != public.hostname.lower():
            raise ValueError("VERIFEYE_PUBLIC_WHEP_URL must use the same public host as VERIFEYE_PUBLIC_BASE_URL.")
        if self.mediamtx_webrtc_additional_host.lower() != public.hostname.lower():
            raise ValueError("VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST must exactly match the public URL host.")
        udp = urlsplit("udp://" + self.mediamtx_webrtc_udp_address)
        if not udp.hostname or udp.port is None or udp.path not in {"", "/"}:
            raise ValueError("VERIFEYE_MEDIAMTX_WEBRTC_UDP_ADDRESS must be an address and port, for example 0.0.0.0:8189.")

    @property
    def webrtc_allowed_origins(self) -> tuple[str, ...]:
        """Permit both conventional loopback names only for local development."""
        public = urlsplit(self.public_base_url)
        if public.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return (f"{public.scheme}://{public.netloc}",)
        port = f":{public.port}" if public.port is not None else ""
        origins = [f"{public.scheme}://127.0.0.1{port}", f"{public.scheme}://localhost{port}"]
        exact = f"{public.scheme}://{public.netloc}"
        if exact not in origins:
            origins.append(exact)
        return tuple(origins)
