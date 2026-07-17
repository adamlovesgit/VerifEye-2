"""Encryption and sanitization for camera connection strings."""

from urllib.parse import urlsplit


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
