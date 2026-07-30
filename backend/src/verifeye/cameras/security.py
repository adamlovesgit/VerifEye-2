"""Encryption and sanitization for camera connection strings."""

from urllib.parse import urlsplit, urlunsplit


class CredentialCipher:
    def __init__(self, key: str) -> None:
        try:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            raise ValueError("VERIFEYE_CAMERA_KEY must be a valid Fernet key.") from exc

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except Exception as exc:
            raise ValueError("Saved camera credentials cannot be decrypted with VERIFEYE_CAMERA_KEY.") from exc


def validate_rtsp_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Enter a valid RTSP URL with a host and optional port.") from exc
    return value.strip()


def sanitized_host(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    return f"{host}:{parsed.port}" if parsed.port else host


def normalized_rtsp_url(value: str) -> str:
    """Canonical comparison form without changing the connection string."""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    default_port = 554 if parsed.scheme.lower() == "rtsp" else 322
    port = "" if parsed.port in (None, default_port) else f":{parsed.port}"
    credentials = ""
    if parsed.username is not None:
        credentials = parsed.username
        if parsed.password is not None: credentials += f":{parsed.password}"
        credentials += "@"
    return urlunsplit((parsed.scheme.lower(), f"{credentials}{host}{port}", parsed.path or "/", parsed.query, parsed.fragment))
