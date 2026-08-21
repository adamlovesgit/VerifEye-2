"""Framework-independent camera models and errors."""

from dataclasses import dataclass
from enum import Enum


class ConnectionState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    LIVE = "live"
    OFFLINE = "offline"
    AUTHENTICATION_FAILED = "authentication_failed"
    RETRYING = "retrying"


class RecognitionSessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"


class RecognitionStreamMode(str, Enum):
    NONE = "none"
    SHARED_LIGHTWEIGHT = "shared_lightweight"
    DEDICATED = "dedicated"
    LIGHTWEIGHT_FALLBACK = "lightweight_fallback"


@dataclass(frozen=True)
class Camera:
    id: int
    name: str
    url: str
    sanitized_host: str
    source_type: str
    enabled: bool
    recognition_url: str | None = None
    onvif_endpoint: str | None = None
    onvif_username: str | None = None
    onvif_password: str | None = None


@dataclass(frozen=True)
class CameraStatus:
    camera_id: int
    enabled: bool
    running: bool
    connection_state: ConnectionState
    last_frame_at: float | None = None
    last_error: str | None = None
    retry_attempt: int = 0
    next_retry_at: float | None = None
    recognition_session_state: RecognitionSessionState = RecognitionSessionState.IDLE
    recognition_stream_mode: RecognitionStreamMode = RecognitionStreamMode.NONE
    recognition_session_started_at: float | None = None
    recognition_deadline: float | None = None
    recognition_maximum_deadline: float | None = None
    recognition_error: str | None = None
    media_ready: bool = False
    pre_roll_ready: bool = False
    pre_roll_last_frame_at: float | None = None
    pre_roll_error: str | None = None


@dataclass(frozen=True)
class RecognitionSessionStatus:
    camera_id: int
    state: RecognitionSessionState
    stream_mode: RecognitionStreamMode
    started_at: float | None
    deadline: float | None
    maximum_deadline: float | None
    extended: bool


class CameraError(Exception): pass
class CameraNotFound(CameraError): pass
class InvalidCameraConfiguration(CameraError): pass
class DuplicateCamera(CameraError): pass
class ActiveCameraLimitReached(CameraError): pass
class CameraAuthenticationFailed(CameraError): pass
