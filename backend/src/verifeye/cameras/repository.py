"""SQLite camera repository; encrypted secrets never leave as persisted plaintext."""

from pathlib import Path
import hashlib
import sqlite3
from contextlib import contextmanager

from .models import Camera, CameraNotFound, DuplicateCamera
from .security import CredentialCipher, sanitized_host


class CameraRepository:
    def __init__(self, database: str | Path, cipher: CredentialCipher) -> None:
        self.database, self.cipher = Path(database), cipher

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.database))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(self) -> list[Camera]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM cameras ORDER BY name COLLATE NOCASE").fetchall()
        return [self._camera(row) for row in rows]

    def get(self, camera_id: int) -> Camera:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        if row is None:
            raise CameraNotFound(f"Camera {camera_id} was not found.")
        return self._camera(row)

    def create(self, name: str, url: str, enabled: bool, source_type: str = "manual") -> Camera:
        encrypted = self.cipher.encrypt(url)
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO cameras(name, encrypted_url, url_fingerprint, sanitized_host, source_type, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                    (name.strip(), encrypted, hashlib.sha256(url.encode()).hexdigest(), sanitized_host(url), source_type, int(enabled)),
                )
                camera_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DuplicateCamera("A camera with that name or connection already exists.") from exc
        return self.get(camera_id)

    def update(self, camera_id: int, *, name=None, url=None, enabled=None) -> Camera:
        camera = self.get(camera_id)
        final_url = url if url is not None else camera.url
        values = (name.strip() if name is not None else camera.name, self.cipher.encrypt(final_url), hashlib.sha256(final_url.encode()).hexdigest(),
                  sanitized_host(final_url), int(camera.enabled if enabled is None else enabled), camera_id)
        try:
            with self._connect() as db:
                db.execute("UPDATE cameras SET name=?, encrypted_url=?, url_fingerprint=?, sanitized_host=?, enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        except sqlite3.IntegrityError as exc:
            raise DuplicateCamera("A camera with that name or connection already exists.") from exc
        return self.get(camera_id)

    def delete(self, camera_id: int) -> None:
        self.get(camera_id)
        with self._connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))

    def _camera(self, row) -> Camera:
        return Camera(row["id"], row["name"], self.cipher.decrypt(row["encrypted_url"]), row["sanitized_host"], row["source_type"], bool(row["enabled"]))
