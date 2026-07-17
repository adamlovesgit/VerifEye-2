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


@dataclass(frozen=True)
class Camera:
    id: int
    name: str
    url: str
    sanitized_host: str
    source_type: str
    enabled: bool


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


class CameraError(Exception): pass
class CameraNotFound(CameraError): pass
class InvalidCameraConfiguration(CameraError): pass
class DuplicateCamera(CameraError): pass
class ActiveCameraLimitReached(CameraError): pass
class CameraAuthenticationFailed(CameraError): pass
