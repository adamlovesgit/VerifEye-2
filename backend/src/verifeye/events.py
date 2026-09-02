"""Durable camera-event ingestion, dispatch, and inspection."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any
import uuid


UTC = timezone.utc
logger = logging.getLogger(__name__)
TERMINAL_SESSION_STATES = {"completed", "failed", "interrupted", "cancelled"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone.")
    return parsed.astimezone(UTC)


def configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int = 5000) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA journal_mode = WAL")


class EventError(ValueError):
    pass


class InvalidEvent(EventError):
    pass


class InvalidEventToken(EventError):
    pass


@dataclass(frozen=True)
class AcceptedEvent:
    id: int
    state: str
    accepted_at: str
    created: bool


class EventRepository:
    """Short-transaction SQLite operations for the event pipeline."""

    def __init__(self, database: str | Path, busy_timeout_ms: int = 5000):
        self.database = Path(database)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.database), timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        configure_connection(connection, self.busy_timeout_ms)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def issue_token(self, camera_id: int) -> tuple[int, str]:
        token = secrets.token_urlsafe(32)
        now = iso(utcnow())
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM cameras WHERE id = ?", (camera_id,)).fetchone() is None:
                raise KeyError(camera_id)
            cursor = connection.execute(
                """INSERT INTO camera_event_tokens(camera_id, token_hash, token_prefix, created_at)
                   VALUES (?, ?, ?, ?)""",
                (camera_id, hashlib.sha256(token.encode()).digest(), token[:8], now),
            )
        return int(cursor.lastrowid), token

    def revoke_tokens(self, camera_id: int) -> int:
        now = iso(utcnow())
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE camera_event_tokens SET revoked_at = ?
                   WHERE camera_id = ? AND revoked_at IS NULL""",
                (now, camera_id),
            )
        return cursor.rowcount

    def revoke_token(self, camera_id: int, token_id: int) -> bool:
        now = iso(utcnow())
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE camera_event_tokens SET revoked_at = ?
                   WHERE id = ? AND camera_id = ? AND revoked_at IS NULL""",
                (now, token_id, camera_id),
            )
        return cursor.rowcount == 1

    def authenticate_camera_token(self, camera_id: int, token: str) -> bool:
        digest = hashlib.sha256(token.encode()).digest()
        now = iso(utcnow())
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, token_hash FROM camera_event_tokens
                   WHERE camera_id = ? AND revoked_at IS NULL""",
                (camera_id,),
            ).fetchall()
            match = next((row for row in rows if hmac.compare_digest(row["token_hash"], digest)), None)
            if match is None:
                return False
            connection.execute(
                "UPDATE camera_event_tokens SET last_used_at = ? WHERE id = ?", (now, match["id"])
            )
        return True

    def accept_event(
        self, camera_id: int, source_event_id: str, event_type: str, occurred_at: datetime,
        metadata: dict[str, Any], screenshot: dict[str, Any] | None = None, *, dispatch: bool = True,
    ) -> AcceptedEvent:
        now = iso(utcnow())
        with self.connect() as connection:
            existing = connection.execute(
                """SELECT id, state, accepted_at FROM camera_events
                   WHERE camera_id = ? AND source_event_id = ?""",
                (camera_id, source_event_id),
            ).fetchone()
            if existing:
                return AcceptedEvent(existing["id"], existing["state"], existing["accepted_at"], False)
            columns = """camera_id, source_event_id, event_type, occurred_at, accepted_at,
                metadata_json, state, created_at, updated_at"""
            values = (camera_id, source_event_id, event_type, iso(occurred_at), now,
                      json.dumps(metadata, separators=(",", ":")), "accepted", now, now)
            placeholders = ", ".join("?" for _ in values)
            cursor = connection.execute(
                f"INSERT INTO camera_events({columns}) VALUES ({placeholders})", values
            )
            event_id = int(cursor.lastrowid)
            if screenshot:
                connection.execute(
                    """INSERT INTO screenshots(
                           event_id, role, relative_path, media_type, byte_size, sha256, created_at
                       ) VALUES (?, 'source_upload', ?, ?, ?, ?, ?)""",
                    (event_id, screenshot["relative_path"], screenshot["media_type"],
                     screenshot["byte_size"], screenshot["sha256"], now),
                )
            if dispatch:
                connection.execute(
                    """INSERT INTO event_dispatch(event_id, state, available_at, created_at, updated_at)
                       VALUES (?, 'pending', ?, ?, ?)""",
                    (event_id, now, now, now),
                )
        return AcceptedEvent(event_id, "accepted", now, True)

    def existing_event(self, camera_id: int, source_event_id: str) -> AcceptedEvent | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, state, accepted_at FROM camera_events
                   WHERE camera_id = ? AND source_event_id = ?""",
                (camera_id, source_event_id),
            ).fetchone()
        return AcceptedEvent(row["id"], row["state"], row["accepted_at"], False) if row else None

    def claim_dispatch(self, owner: str, lease_seconds: float) -> sqlite3.Row | None:
        now_dt, now = utcnow(), iso(utcnow())
        expiry = iso(now_dt + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE event_dispatch
                   SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                   WHERE state = 'claimed' AND lease_expires_at < ?""", (now, now)
            )
            row = connection.execute(
                """SELECT id FROM event_dispatch
                   WHERE state = 'pending' AND available_at <= ?
                   ORDER BY available_at, id LIMIT 1""", (now,)
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """UPDATE event_dispatch SET state = 'claimed', attempts = attempts + 1,
                       lease_owner = ?, lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'pending'""",
                (owner, expiry, now, row["id"]),
            )
            claimed = connection.execute(
                """SELECT d.*, e.camera_id, e.occurred_at, e.state AS event_state
                   FROM event_dispatch d JOIN camera_events e ON e.id = d.event_id
                   WHERE d.id = ?""", (row["id"],)
            ).fetchone()
            connection.commit()
            return claimed

    def attach_event(
        self, event_id: int, pre_roll: float, window: float, maximum: float
    ) -> tuple[int, bool]:
        now_dt, now = utcnow(), iso(utcnow())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            linked = connection.execute(
                "SELECT session_id FROM recognition_session_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if linked:
                connection.commit()
                return int(linked["session_id"]), False
            event = connection.execute("SELECT * FROM camera_events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            occurred = parse_utc(event["occurred_at"])
            requested_start = occurred - timedelta(seconds=pre_roll)
            requested_end = occurred + timedelta(seconds=window)
            candidates = connection.execute(
                """SELECT * FROM recognition_sessions
                   WHERE camera_id = ? AND state IN ('pending', 'active')
                   ORDER BY id DESC""", (event["camera_id"],)
            ).fetchall()
            session = next((
                row for row in candidates
                if now_dt < parse_utc(row["maximum_end"])
                and requested_start <= parse_utc(row["interval_end"])
                and requested_end >= parse_utc(row["interval_start"])
            ), None)
            created = session is None
            if created:
                maximum_end = max(now_dt, requested_end) + timedelta(seconds=max(0, maximum - window))
                cursor = connection.execute(
                    """INSERT INTO recognition_sessions(
                           camera_id, state, interval_start, interval_end, maximum_end, created_at, updated_at
                       ) VALUES (?, 'pending', ?, ?, ?, ?, ?)""",
                    (event["camera_id"], iso(requested_start), iso(requested_end),
                     iso(maximum_end), now, now),
                )
                session_id, effective_end = int(cursor.lastrowid), requested_end
            else:
                session_id = int(session["id"])
                effective_end = min(max(parse_utc(session["interval_end"]), requested_end),
                                    parse_utc(session["maximum_end"]))
                connection.execute(
                    """UPDATE recognition_sessions SET interval_end = ?, updated_at = ?
                       WHERE id = ? AND state IN ('pending', 'active')""",
                    (iso(effective_end), now, session_id),
                )
            connection.execute(
                """INSERT INTO recognition_session_events(
                       session_id, event_id, attribution_start, attribution_end, attached_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (session_id, event_id, iso(requested_start), iso(min(requested_end, effective_end)), now),
            )
            connection.execute(
                """UPDATE camera_events SET state = 'dispatched', updated_at = ?
                   WHERE id = ? AND state = 'accepted'""", (now, event_id)
            )
            connection.commit()
            return session_id, created

    def session_state(self, session_id: int) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM recognition_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row["state"]

    def session_spec(self, session_id: int) -> dict[str, Any]:
        """Return durable UTC boundaries used to derive monotonic runtime timing."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.interval_start, s.interval_end, s.maximum_end,
                          MIN(e.occurred_at) AS trigger_at
                   FROM recognition_sessions s
                   JOIN recognition_session_events l ON l.session_id=s.id
                   JOIN camera_events e ON e.id=l.event_id
                   WHERE s.id=? GROUP BY s.id""", (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return {name: parse_utc(row[name]) for name in (
            "interval_start", "interval_end", "maximum_end", "trigger_at"
        )}

    def transition_session(
        self, session_id: int, state: str, *, stream_mode: str | None = None,
        error_code: str | None = None, error_message: str | None = None,
    ) -> None:
        if state not in {"pending", "active", "completed", "failed", "interrupted", "cancelled"}:
            raise ValueError(state)
        now = iso(utcnow())
        terminal = state in TERMINAL_SESSION_STATES
        with self.connect() as connection:
            current = connection.execute(
                "SELECT state FROM recognition_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if current is None or current["state"] in TERMINAL_SESSION_STATES:
                return
            connection.execute(
                """UPDATE recognition_sessions SET state = ?, stream_mode = COALESCE(?, stream_mode),
                       started_at = CASE WHEN ? = 'active' THEN COALESCE(started_at, ?) ELSE started_at END,
                       completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                       error_code = ?, error_message = ?, updated_at = ? WHERE id = ?""",
                (state, stream_mode, state, now, int(terminal), now,
                 error_code, error_message, now, session_id),
            )
            if terminal:
                event_state = "completed" if state == "completed" else "failed"
                connection.execute(
                    """UPDATE camera_events SET state = ?, error_code = ?, error_message = ?, updated_at = ?
                       WHERE id IN (SELECT event_id FROM recognition_session_events WHERE session_id = ?)
                         AND state IN ('accepted', 'dispatched')""",
                    (event_state, error_code, error_message, now, session_id),
                )

    def add_results(self, session_id: int, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        now = iso(utcnow())
        with self.connect() as connection:
            for item in results:
                cursor = connection.execute(
                    """INSERT INTO recognition_results(
                       session_id, capture_timestamp, frame_sequence, source_role, outcome,
                       identity_id, similarity, detection_confidence, displayed_label,
                       error_code, error_message, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, item["capture_timestamp"], item.get("frame_sequence"),
                     item.get("source_role"), item["outcome"], item.get("identity_id"),
                     item.get("similarity"), item.get("detection_confidence"),
                     item.get("displayed_label"), item.get("error_code"),
                     item.get("error_message"), now),
                )
                screenshot = item.get("screenshot")
                if screenshot:
                    connection.execute(
                        """INSERT INTO screenshots(
                               result_id, role, relative_path, media_type, byte_size, sha256, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (int(cursor.lastrowid), screenshot.get("role", "face_crop"),
                         screenshot["relative_path"], screenshot["media_type"],
                         screenshot["byte_size"], screenshot["sha256"], now),
                    )

    def complete_dispatch(self, dispatch_id: int, owner: str) -> None:
        now = iso(utcnow())
        with self.connect() as connection:
            connection.execute(
                """UPDATE event_dispatch SET state = 'completed', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE id = ? AND state = 'claimed' AND lease_owner = ?""",
                (now, dispatch_id, owner),
            )

    def renew_dispatch(self, dispatch_id: int, owner: str, lease_seconds: float) -> bool:
        now_dt, now = utcnow(), iso(utcnow())
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE event_dispatch SET lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'claimed' AND lease_owner = ?
                     AND lease_expires_at >= ?""",
                (iso(now_dt + timedelta(seconds=lease_seconds)), now,
                 dispatch_id, owner, now),
            )
        return cursor.rowcount == 1

    def retry_dispatch(
        self, row: sqlite3.Row, owner: str, code: str, message: str, max_attempts: int
    ) -> None:
        now_dt, now = utcnow(), iso(utcnow())
        exhausted = int(row["attempts"]) >= max_attempts
        with self.connect() as connection:
            if exhausted:
                connection.execute(
                    """UPDATE event_dispatch SET state = 'failed', lease_owner = NULL,
                           lease_expires_at = NULL, last_error_code = ?, last_error_message = ?,
                           updated_at = ? WHERE id = ? AND lease_owner = ?""",
                    (code, message, now, row["id"], owner),
                )
                linked = connection.execute(
                    "SELECT session_id FROM recognition_session_events WHERE event_id = ?", (row["event_id"],)
                ).fetchone()
            else:
                delay = min(16, 2 ** max(0, int(row["attempts"]) - 1))
                connection.execute(
                    """UPDATE event_dispatch SET state = 'pending', available_at = ?,
                           lease_owner = NULL, lease_expires_at = NULL, last_error_code = ?,
                           last_error_message = ?, updated_at = ?
                       WHERE id = ? AND lease_owner = ?""",
                    (iso(now_dt + timedelta(seconds=delay)), code, message, now, row["id"], owner),
                )
                linked = None
        if exhausted and linked:
            self.transition_session(linked["session_id"], "failed",
                                    error_code="dispatch_exhausted", error_message=message)

    def fail_dispatch_permanently(self, row: sqlite3.Row, owner: str, code: str, message: str) -> None:
        self.retry_dispatch(row, owner, code, message, 0)

    def reconcile(self) -> None:
        now = iso(utcnow())
        with self.connect() as connection:
            connection.execute(
                """UPDATE event_dispatch SET state = 'pending', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ? WHERE state = 'claimed'""", (now,)
            )
            interrupted = [row["id"] for row in connection.execute(
                "SELECT id FROM recognition_sessions WHERE state IN ('pending', 'active')"
            )]
        for session_id in interrupted:
            self.transition_session(session_id, "interrupted",
                                    error_code="interrupted_by_restart",
                                    error_message="Recognition was interrupted by process restart.")
        now = iso(utcnow())
        with self.connect() as connection:
            connection.execute(
                """UPDATE event_dispatch SET state = 'completed', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE event_id IN (SELECT id FROM camera_events WHERE state = 'completed')
                     AND state NOT IN ('completed', 'failed')""", (now,)
            )
            connection.execute(
                """UPDATE event_dispatch SET state = 'failed', lease_owner = NULL,
                       lease_expires_at = NULL, last_error_code = COALESCE(last_error_code, 'event_failed'),
                       last_error_message = COALESCE(last_error_message, 'The event is terminal.'),
                       updated_at = ?
                   WHERE event_id IN (SELECT id FROM camera_events WHERE state = 'failed')
                     AND state NOT IN ('completed', 'failed')""", (now,)
            )

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            event = connection.execute(
                """SELECT e.*, c.name AS camera_name FROM camera_events e
                   JOIN cameras c ON c.id = e.camera_id WHERE e.id = ?""", (event_id,)
            ).fetchone()
            if event is None:
                return None
            dispatch = connection.execute(
                "SELECT * FROM event_dispatch WHERE event_id = ?", (event_id,)
            ).fetchone()
            link = connection.execute(
                """SELECT l.*, s.state AS session_state, s.stream_mode, s.error_code AS session_error_code,
                          s.error_message AS session_error_message, s.started_at, s.completed_at
                   FROM recognition_session_events l JOIN recognition_sessions s ON s.id = l.session_id
                   WHERE l.event_id = ?""", (event_id,)
            ).fetchone()
            results = []
            if link:
                results = connection.execute(
                    """SELECT * FROM recognition_results WHERE session_id = ?
                       AND (capture_timestamp BETWEEN ? AND ?
                            OR outcome IN ('no_face', 'processing_error'))
                       ORDER BY capture_timestamp, id""",
                    (link["session_id"], link["attribution_start"], link["attribution_end"]),
                ).fetchall()
            screenshots = connection.execute(
                """SELECT s.id, s.role, s.media_type, s.byte_size, s.created_at,
                          s.result_id, s.session_id
                   FROM screenshots s WHERE s.event_id = ?
                      OR s.session_id = ?
                      OR s.result_id IN (
                          SELECT r.id FROM recognition_results r
                          WHERE r.session_id = ? AND (
                              r.capture_timestamp BETWEEN ? AND ?
                              OR r.outcome IN ('no_face', 'processing_error')
                          )
                      )""",
                (event_id, link["session_id"] if link else -1,
                 link["session_id"] if link else -1,
                 link["attribution_start"] if link else "",
                 link["attribution_end"] if link else ""),
            ).fetchall()
        value = dict(event)
        value["metadata"] = json.loads(value.pop("metadata_json"))
        value["dispatch"] = dict(dispatch) if dispatch else None
        value["session"] = dict(link) if link else None
        value["results"] = [dict(row) for row in results]
        value["screenshots"] = [dict(row) for row in screenshots]
        return value

    def list_events(
        self, *, limit: int = 50, offset: int = 0, camera_id: int | None = None,
        state: str | None = None, outcome: str | None = None,
        accepted_after: str | None = None, accepted_before: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, parameters = [], []
        if camera_id is not None:
            clauses.append("e.camera_id = ?"); parameters.append(camera_id)
        if state:
            clauses.append("e.state = ?"); parameters.append(state)
        if accepted_after:
            clauses.append("e.accepted_at >= ?"); parameters.append(iso(parse_utc(accepted_after)))
        if accepted_before:
            clauses.append("e.accepted_at <= ?"); parameters.append(iso(parse_utc(accepted_before)))
        if outcome:
            clauses.append("""EXISTS (
                SELECT 1 FROM recognition_session_events l JOIN recognition_results r
                  ON r.session_id = l.session_id
                WHERE l.event_id = e.id AND r.outcome = ?
                  AND (r.capture_timestamp BETWEEN l.attribution_start AND l.attribution_end
                       OR r.outcome IN ('no_face', 'processing_error')))""")
            parameters.append(outcome)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT e.*, c.name AS camera_name FROM camera_events e
                    JOIN cameras c ON c.id = e.camera_id{where}
                    ORDER BY e.accepted_at DESC, e.id DESC LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metadata"] = json.loads(value.pop("metadata_json"))
            values.append(value)
        return values

    def screenshot(self, screenshot_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM screenshots WHERE id = ?", (screenshot_id,)).fetchone()

    def referenced_paths(self) -> set[str]:
        with self.connect() as connection:
            return {row["relative_path"] for row in connection.execute("SELECT relative_path FROM screenshots")}

    def prune_motion_events(self, no_face_days: int, unrecognized_days: int) -> int:
        """Delete terminal ONVIF motion events according to their strongest outcome."""
        no_face_cutoff = iso(utcnow() - timedelta(days=no_face_days))
        unrecognized_cutoff = iso(utcnow() - timedelta(days=unrecognized_days))
        with self.connect() as connection:
            cursor = connection.execute(
                """DELETE FROM camera_events AS e
                   WHERE e.event_type = 'onvif_motion'
                     AND e.state IN ('completed', 'failed')
                     AND NOT EXISTS (
                         SELECT 1 FROM recognition_session_events l
                         JOIN recognition_results r ON r.session_id = l.session_id
                         WHERE l.event_id = e.id AND r.outcome = 'recognized'
                     )
                     AND (
                         (e.accepted_at < ? AND EXISTS (
                             SELECT 1 FROM recognition_session_events l
                             JOIN recognition_results r ON r.session_id = l.session_id
                             WHERE l.event_id = e.id AND r.outcome = 'unrecognized_face'
                         ))
                         OR
                         (e.accepted_at < ? AND NOT EXISTS (
                             SELECT 1 FROM recognition_session_events l
                             JOIN recognition_results r ON r.session_id = l.session_id
                             WHERE l.event_id = e.id AND r.outcome = 'unrecognized_face'
                         ))
                     )""",
                (unrecognized_cutoff, no_face_cutoff),
            )
            connection.execute(
                """DELETE FROM recognition_sessions
                   WHERE state IN ('completed', 'failed', 'interrupted', 'cancelled')
                     AND NOT EXISTS (
                         SELECT 1 FROM recognition_session_events l
                         WHERE l.session_id = recognition_sessions.id
                     )"""
            )
        return cursor.rowcount


class ScreenshotStorage:
    EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.temporary = self.root / ".tmp"
        self.temporary.mkdir(parents=True, exist_ok=True)

    def stage(self, contents: bytes, media_type: str, generated: bool = False) -> tuple[Path, dict[str, Any]]:
        if media_type not in self.EXTENSIONS:
            raise InvalidEvent("Screenshot must be JPEG, PNG, or WebP.")
        if not contents:
            raise InvalidEvent("Screenshot is empty.")
        digest = hashlib.sha256(contents).hexdigest()
        temporary = self.temporary / f"{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(contents)
        relative = Path("generated" if generated else "source") / f"{uuid.uuid4().hex}{self.EXTENSIONS[media_type]}"
        final = self.root / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(final)
        return final, {
            "relative_path": relative.as_posix(), "media_type": media_type,
            "byte_size": len(contents), "sha256": digest,
        }

    def resolve(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError("Invalid screenshot path.")
        return target

    def cleanup_orphans(self, referenced: set[str]) -> None:
        for path in self.temporary.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        for directory in ("source", "generated"):
            location = self.root / directory
            if not location.exists():
                continue
            for path in location.rglob("*"):
                if path.is_file() and path.relative_to(self.root).as_posix() not in referenced:
                    path.unlink(missing_ok=True)


class RecognitionPersistenceSink:
    """Explicit persistence boundary for recognition runtime output."""

    def __init__(self, repository: EventRepository, screenshot_storage: ScreenshotStorage | None = None):
        self.repository, self.screenshot_storage = repository, screenshot_storage
        self.notification_callback = None
        self._capture_errors, self._lock = {}, threading.Lock()

    def session_spec(self, session_id):
        return self.repository.session_spec(session_id)

    def active(self, session_id: int, stream_mode: str, error: str | None = None) -> None:
        if error:
            with self._lock: self._capture_errors[session_id] = error
        self.repository.transition_session(
            session_id, "active", stream_mode=stream_mode,
            error_code="capture_fallback" if error else None, error_message=error,
        )

    def results(self, session_id: int, results: list[dict[str, Any]]) -> None:
        finalized = []
        try:
            if self.screenshot_storage:
                for item in results:
                    image = item.pop("image_bytes", None)
                    image_role = item.pop("image_role", "face_crop")
                    if image:
                        path, record = self.screenshot_storage.stage(image, "image/jpeg", generated=True)
                        record["role"] = image_role
                        finalized.append(path); item["screenshot"] = record
            self.repository.add_results(session_id, results)
        except Exception:
            for path in finalized: path.unlink(missing_ok=True)
            raise

    def terminal(self, session_id: int, error: str | None) -> None:
        state = "failed" if error else "completed"
        with self._lock: capture_error = self._capture_errors.pop(session_id, None)
        self.repository.transition_session(
            session_id, state,
            error_code="recognition_failed" if error else ("capture_fallback" if capture_error else None),
            error_message=error or capture_error,
        )
        notifier = self.notification_callback
        if notifier:
            try: notifier(session_id)
            except Exception: logger.exception("Notification planning failed for recognition session %s.", session_id)


class EventDispatcher:
    def __init__(
        self, repository: EventRepository, manager, pre_roll: float, window: float, maximum: float,
        lease_seconds: float = 30, max_attempts: int = 5, poll_seconds: float = .25,
        screenshot_storage: ScreenshotStorage | None = None, no_face_retention_days: int = 7,
        unrecognized_retention_days: int = 30, retention_interval_seconds: float = 3600,
    ):
        self.repository, self.manager = repository, manager
        self.pre_roll, self.window, self.maximum = pre_roll, window, maximum
        self.lease_seconds, self.max_attempts, self.poll_seconds = lease_seconds, max_attempts, poll_seconds
        self.screenshot_storage = screenshot_storage
        self.no_face_retention_days, self.unrecognized_retention_days = no_face_retention_days, unrecognized_retention_days
        self.retention_interval_seconds = retention_interval_seconds
        self._next_retention_at = 0.0
        self.owner = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="event-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() >= self._next_retention_at:
                try:
                    self.repository.prune_motion_events(
                        self.no_face_retention_days, self.unrecognized_retention_days
                    )
                    if self.screenshot_storage:
                        self.screenshot_storage.cleanup_orphans(self.repository.referenced_paths())
                except Exception:
                    logger.exception("Motion-event retention cleanup failed.")
                finally:
                    self._next_retention_at = time.monotonic() + self.retention_interval_seconds
            row = self.repository.claim_dispatch(self.owner, self.lease_seconds)
            if row is None:
                self._stop.wait(self.poll_seconds)
                continue
            try:
                session_id, _created = self.repository.attach_event(
                    row["event_id"], self.pre_roll, self.window, self.maximum
                )
                state = self.repository.session_state(session_id)
                if state in {"pending", "active"}:
                    renewal_stop = threading.Event()
                    def renew():
                        while not renewal_stop.wait(self.lease_seconds / 2):
                            if not self.repository.renew_dispatch(
                                row["id"], self.owner, self.lease_seconds
                            ):
                                return
                    renewal = threading.Thread(target=renew, daemon=True)
                    renewal.start()
                    try:
                        self.manager.request(row["camera_id"], session_id)
                    finally:
                        renewal_stop.set()
                        renewal.join(min(1, self.lease_seconds / 2))
                elif state != "completed":
                    raise RuntimeError(f"Session {session_id} is {state}.")
                self.repository.complete_dispatch(row["id"], self.owner)
            except KeyError as exc:
                self.repository.fail_dispatch_permanently(
                    row, self.owner, "missing_camera", str(exc)
                )
            except Exception as exc:
                self.repository.retry_dispatch(
                    row, self.owner, "dispatch_failed", str(exc), self.max_attempts
                )
