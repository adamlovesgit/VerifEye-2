"""Durable notification rules, planning, and SMTP/Twilio delivery."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid
import html
import json
import logging
import re
import smtplib
import sqlite3
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .events import configure_connection, iso, parse_utc, utcnow


logger = logging.getLogger(__name__)


RULE_OUTCOMES = {
    "identity": "recognized",
    "unknown_face": "unrecognized_face",
    "no_face": "no_face",
    "system_error": "processing_error",
}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class NotificationError(ValueError):
    pass


class PermanentDeliveryError(RuntimeError):
    pass


def validate_email(value: str | None) -> str | None:
    value = value.strip() if value else None
    if value and not EMAIL_RE.fullmatch(value):
        raise NotificationError("Enter a valid email address.")
    return value


def validate_phone(value: str | None) -> str | None:
    value = re.sub(r"[\s()-]", "", value or "") or None
    if value and not PHONE_RE.fullmatch(value):
        raise NotificationError("Enter a phone number in E.164 format, such as +15551234567.")
    return value


@dataclass
class ProviderSettings:
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

    @property
    def email_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_sender and self.smtp_tls_mode in {"starttls", "ssl", "none"})

    @property
    def sms_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)


class NotificationProviderStore:
    """Persist SMTP overrides while keeping the password encrypted at rest."""

    def __init__(self, database: str | Path, cipher) -> None:
        self.database, self.cipher = Path(database), cipher

    def load(self, defaults: ProviderSettings) -> ProviderSettings:
        connection = sqlite3.connect(str(self.database))
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM notification_provider_settings WHERE id=1").fetchone()
        finally:
            connection.close()
        if row is None:
            return defaults
        password = self.cipher.decrypt(row["smtp_password_encrypted"]) if row["smtp_password_encrypted"] else ""
        defaults.smtp_host, defaults.smtp_port = row["smtp_host"], int(row["smtp_port"])
        defaults.smtp_username, defaults.smtp_password = row["smtp_username"], password
        defaults.smtp_sender, defaults.smtp_tls_mode = row["smtp_sender"], row["smtp_tls_mode"]
        return defaults

    def save_smtp(self, payload: dict[str, Any], current: ProviderSettings) -> ProviderSettings:
        host = str(payload.get("host") or "").strip()
        username = str(payload.get("username") or "").strip()
        sender = str(payload.get("sender") or "").strip()
        tls_mode = str(payload.get("tlsMode") or "").lower()
        port = int(payload.get("port") or 0)
        if len(host) > 253 or len(username) > 320 or len(sender) > 320:
            raise NotificationError("SMTP settings are too long.")
        if not 1 <= port <= 65535 or tls_mode not in {"starttls", "ssl", "none"}:
            raise NotificationError("Choose a valid SMTP port and TLS mode.")
        if sender and not EMAIL_RE.fullmatch(sender):
            raise NotificationError("Enter a valid SMTP sender email address.")
        password = "" if payload.get("clearPassword") else payload.get("password")
        if password is None or password == "":
            password = "" if payload.get("clearPassword") else current.smtp_password
        if len(password) > 1024:
            raise NotificationError("SMTP password is too long.")
        encrypted = self.cipher.encrypt(password) if password else None
        now = iso(utcnow())
        connection = sqlite3.connect(str(self.database))
        try:
            configure_connection(connection)
            with connection:
                connection.execute(
                    """INSERT INTO notification_provider_settings(
                           id,smtp_host,smtp_port,smtp_username,smtp_password_encrypted,
                           smtp_sender,smtp_tls_mode,updated_at
                       ) VALUES(1,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET smtp_host=excluded.smtp_host,
                           smtp_port=excluded.smtp_port,smtp_username=excluded.smtp_username,
                           smtp_password_encrypted=excluded.smtp_password_encrypted,
                           smtp_sender=excluded.smtp_sender,smtp_tls_mode=excluded.smtp_tls_mode,
                           updated_at=excluded.updated_at""",
                    (host, port, username, encrypted, sender, tls_mode, now),
                )
        finally:
            connection.close()
        current.smtp_host, current.smtp_port = host, port
        current.smtp_username, current.smtp_password = username, password
        current.smtp_sender, current.smtp_tls_mode = sender, tls_mode
        return current


class NotificationRepository:
    def __init__(self, database: str | Path, busy_timeout_ms: int = 5000):
        self.database, self.busy_timeout_ms = Path(database), busy_timeout_ms

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

    def settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            identities = [dict(row) for row in connection.execute(
                "SELECT id, display_name AS displayName FROM identities ORDER BY display_name COLLATE NOCASE")]
            cameras = [dict(row) for row in connection.execute(
                "SELECT id, name FROM cameras ORDER BY name COLLATE NOCASE")]
            rows = connection.execute(
                """SELECT r.*, i.display_name AS identity_name FROM notification_rules r
                   LEFT JOIN identities i ON i.id = r.identity_id
                   ORDER BY CASE r.rule_type
                     WHEN 'unknown_face' THEN 0 WHEN 'no_face' THEN 1
                     WHEN 'system_error' THEN 2 ELSE 3 END,
                     i.display_name COLLATE NOCASE""").fetchall()
            camera_rows = connection.execute(
                """SELECT rc.rule_id,rc.camera_id FROM notification_rule_cameras rc
                   JOIN notification_rules r ON r.id=rc.rule_id""").fetchall()
        by_rule: dict[int, list[int]] = {}
        for row in camera_rows:
            by_rule.setdefault(row["rule_id"], []).append(row["camera_id"])
        return {"rules": [self._rule_json(row, by_rule.get(row["id"], [])) for row in rows],
                "identities": identities, "cameras": cameras}

    @staticmethod
    def _rule_json(row, cameras):
        return {"id": row["id"], "identityId": row["identity_id"], "identityName": row["identity_name"],
                "ruleType": row["rule_type"], "emailAddress": row["email_address"] or "",
                "phoneNumber": row["phone_number"] or "", "emailEnabled": bool(row["email_enabled"]),
                "smsEnabled": bool(row["sms_enabled"]), "cameraIds": sorted(cameras),
                "version": row["version"]}

    def save_rule(self, payload: dict[str, Any], rule_id: int | None = None) -> int:
        identity_id = payload.get("identityId")
        rule_type = payload.get("ruleType")
        if rule_type not in RULE_OUTCOMES: raise NotificationError("Choose a valid rule type.")
        if (rule_type == "identity") != (identity_id is not None):
            raise NotificationError("Choose either a session-level rule or one identity.")
        email, phone = validate_email(payload.get("emailAddress")), validate_phone(payload.get("phoneNumber"))
        email_enabled, sms_enabled = bool(payload.get("emailEnabled")), bool(payload.get("smsEnabled"))
        if email_enabled and not email: raise NotificationError("An email address is required when email is enabled.")
        if sms_enabled and not phone: raise NotificationError("A phone number is required when SMS is enabled.")
        camera_ids = {int(value) for value in payload.get("cameraIds") or []}
        now = iso(utcnow())
        try:
            with self.connect() as connection:
                if identity_id is not None and connection.execute(
                        "SELECT 1 FROM identities WHERE id=?", (identity_id,)).fetchone() is None:
                    raise NotificationError("Identity not found.")
                if camera_ids:
                    found = {r[0] for r in connection.execute(
                        f"SELECT id FROM cameras WHERE id IN ({','.join('?' for _ in camera_ids)})", tuple(camera_ids))}
                    if found != camera_ids: raise NotificationError("One or more cameras were not found.")
                if rule_id is None:
                    columns = """identity_id,rule_type,email_address,phone_number,email_enabled,
                        sms_enabled,created_at,updated_at"""
                    values = (identity_id, rule_type, email, phone, int(email_enabled), int(sms_enabled), now, now)
                    cursor = connection.execute(
                        f"INSERT INTO notification_rules({columns}) VALUES({','.join('?' for _ in values)})", values)
                    rule_id = int(cursor.lastrowid)
                else:
                    version = int(payload.get("version") or 0)
                    cursor = connection.execute(
                        """UPDATE notification_rules SET identity_id=?,rule_type=?,email_address=?,phone_number=?,
                           email_enabled=?,sms_enabled=?,version=version+1,updated_at=?
                           WHERE id=? AND version=?""",
                        (identity_id, rule_type, email, phone, int(email_enabled), int(sms_enabled),
                         now, rule_id, version))
                    if cursor.rowcount != 1: raise NotificationError("This rule changed elsewhere. Refresh and try again.")
                    connection.execute("DELETE FROM notification_rule_cameras WHERE rule_id=?", (rule_id,))
                connection.executemany("INSERT INTO notification_rule_cameras(rule_id,camera_id) VALUES(?,?)",
                                       [(rule_id, camera_id) for camera_id in camera_ids])
        except sqlite3.IntegrityError as exc:
            raise NotificationError("A rule already exists for this identity or session outcome.") from exc
        return rule_id

    def delete_rule(self, rule_id: int) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM notification_rules WHERE id=?", (rule_id,)).rowcount == 1

    def enqueue_session(self, session_id: int, base_url: str) -> int:
        count, now = 0, iso(utcnow())
        with self.connect() as connection:
            events = connection.execute(
                """SELECT e.id,e.camera_id,e.occurred_at,c.name camera_name FROM camera_events e
                   JOIN cameras c ON c.id=e.camera_id JOIN recognition_session_events l ON l.event_id=e.id
                   WHERE l.session_id=? AND e.state IN ('completed','failed')
                   ORDER BY e.occurred_at,e.id""", (session_id,)).fetchall()
            session_results = connection.execute(
                """SELECT r.*,i.display_name identity_name FROM recognition_results r
                   LEFT JOIN identities i ON i.id=r.identity_id WHERE r.session_id=?""",
                (session_id,),
            ).fetchall()
            session = connection.execute(
                "SELECT state FROM recognition_sessions WHERE id=?", (session_id,)
            ).fetchone()
            has_face = any(r["outcome"] in {"recognized", "unrecognized_face"} for r in session_results)
            session_rule_types = []
            if any(r["outcome"] == "unrecognized_face" for r in session_results):
                session_rule_types.append("unknown_face")
            if session and session["state"] == "completed" and not has_face \
                    and any(r["outcome"] == "no_face" for r in session_results):
                session_rule_types.append("no_face")
            if (session and session["state"] == "failed") \
                    or any(r["outcome"] == "processing_error" for r in session_results):
                session_rule_types.append("system_error")
            for event_index, event in enumerate(events):
                results = connection.execute(
                    """SELECT r.*,i.display_name identity_name FROM recognition_results r
                       LEFT JOIN identities i ON i.id=r.identity_id JOIN recognition_session_events l ON l.session_id=r.session_id
                       WHERE l.event_id=? AND r.capture_timestamp BETWEEN l.attribution_start AND l.attribution_end""",
                    (event["id"],),
                ).fetchall()
                identities = {r["identity_id"]: r["identity_name"] for r in results if r["outcome"] == "recognized" and r["identity_id"]}
                matches = [(identity_id, RULE_OUTCOMES["identity"], name, "identity")
                           for identity_id, name in identities.items()]
                if event_index == 0:
                    matches.extend((None, RULE_OUTCOMES[rule_type], None, rule_type)
                                   for rule_type in session_rule_types)
                for identity_id, outcome, identity_name, rule_type in matches:
                    rules = connection.execute(
                        """SELECT r.* FROM notification_rules r WHERE
                           r.rule_type=? AND (? IS NULL OR r.identity_id=?)
                           AND (NOT EXISTS(SELECT 1 FROM notification_rule_cameras rc WHERE rc.rule_id=r.id)
                                 OR EXISTS(SELECT 1 FROM notification_rule_cameras rc WHERE rc.rule_id=r.id AND rc.camera_id=?))""",
                        (rule_type, identity_id, identity_id, event["camera_id"])).fetchall()
                    screenshot = connection.execute(
                        """SELECT s.id FROM screenshots s LEFT JOIN recognition_results r ON r.id=s.result_id
                           WHERE s.event_id=? OR (r.session_id=? AND r.identity_id IS ?)
                           AND (?='recognized' OR r.outcome=?)
                           ORDER BY CASE s.role WHEN 'face_crop' THEN 0 ELSE 1 END,s.id LIMIT 1""",
                        (event["id"], session_id, identity_id, outcome, outcome)).fetchone()
                    for rule in rules:
                        for channel, enabled, destination in (("email", rule["email_enabled"], rule["email_address"]),
                                                              ("sms", rule["sms_enabled"], rule["phone_number"])):
                            if not enabled or not destination: continue
                            columns = """event_id,session_id,rule_id,channel,destination,available_at,outcome,camera_name,
                                identity_name,occurred_at,event_link,screenshot_id,created_at,updated_at"""
                            values = (event["id"], session_id, rule["id"], channel, destination, now, outcome,
                                      event["camera_name"], identity_name, event["occurred_at"],
                                      f"{base_url.rstrip('/')}/?event={event['id']}",
                                      screenshot["id"] if screenshot else None, now, now)
                            cursor = connection.execute(
                                f"INSERT OR IGNORE INTO notification_deliveries({columns}) "
                                f"VALUES({','.join('?' for _ in values)})", values)
                            count += cursor.rowcount
        return count

    def reconcile(self, base_url: str) -> None:
        with self.connect() as connection:
            sessions = [row[0] for row in connection.execute(
                "SELECT id FROM recognition_sessions WHERE state IN ('completed','failed')")]
        for session_id in sessions: self.enqueue_session(session_id, base_url)

    def enqueue_test(self, rule_id: int, channel: str, base_url: str) -> int:
        if channel not in {"email", "sms"}: raise NotificationError("Channel must be email or SMS.")
        now = iso(utcnow())
        with self.connect() as connection:
            rule = connection.execute("SELECT * FROM notification_rules WHERE id=?", (rule_id,)).fetchone()
            if not rule: raise NotificationError("Notification rule not found.")
            destination = rule["email_address" if channel == "email" else "phone_number"]
            if not destination: raise NotificationError(f"Configure a destination before testing {channel}.")
            columns = """rule_id,channel,destination,available_at,outcome,camera_name,identity_name,
                occurred_at,event_link,is_test,created_at,updated_at"""
            values = (rule_id, channel, destination, now, "test", "VerifEye test", None, now,
                      base_url.rstrip("/"), 1, now, now)
            cursor = connection.execute(
                f"INSERT INTO notification_deliveries({columns}) VALUES({','.join('?' for _ in values)})", values)
        return int(cursor.lastrowid)

    def claim(self, owner: str, lease_seconds: float):
        now_dt, now = utcnow(), iso(utcnow())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE notification_deliveries SET status='retrying',lease_owner=NULL,
                lease_expires_at=NULL,available_at=?,updated_at=? WHERE status='claimed' AND lease_expires_at<?""", (now, now, now))
            row = connection.execute("""SELECT id FROM notification_deliveries WHERE status IN ('queued','retrying')
                AND available_at<=? ORDER BY available_at,id LIMIT 1""", (now,)).fetchone()
            if not row: connection.commit(); return None
            connection.execute("""UPDATE notification_deliveries SET status='claimed',attempts=attempts+1,
                lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?""",
                (owner, iso(now_dt + timedelta(seconds=lease_seconds)), now, row["id"]))
            claimed = connection.execute("SELECT * FROM notification_deliveries WHERE id=?", (row["id"],)).fetchone()
            connection.commit(); return claimed

    def sent(self, delivery_id: int, message_id: str | None):
        with self.connect() as connection: connection.execute(
            """UPDATE notification_deliveries SET status='sent',provider_message_id=?,lease_owner=NULL,
               lease_expires_at=NULL,last_error=NULL,updated_at=? WHERE id=?""", (message_id, iso(utcnow()), delivery_id))

    def failed(self, row, error: str, permanent: bool, max_attempts: int):
        terminal = permanent or row["attempts"] >= max_attempts
        delay = min(300, 2 ** max(1, row["attempts"]))
        with self.connect() as connection: connection.execute(
            """UPDATE notification_deliveries SET status=?,available_at=?,lease_owner=NULL,lease_expires_at=NULL,
               last_error=?,updated_at=? WHERE id=?""",
            ("failed" if terminal else "retrying", iso(utcnow() + timedelta(seconds=delay)), str(error)[:500], iso(utcnow()), row["id"]))

    def deliveries(self, limit=50, offset=0, status=None, channel=None):
        clauses, args = [], []
        if status: clauses.append("status=?"); args.append(status)
        if channel: clauses.append("channel=?"); args.append(channel)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM notification_deliveries{where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return [{"id":r["id"],"channel":r["channel"],"destination":r["destination"],"status":r["status"],
                 "attempts":r["attempts"],"outcome":r["outcome"],"cameraName":r["camera_name"],
                 "identityName":r["identity_name"],"occurredAt":r["occurred_at"],"isTest":bool(r["is_test"]),
                 "providerMessageId":r["provider_message_id"],"lastError":r["last_error"],
                 "createdAt":r["created_at"],"updatedAt":r["updated_at"]} for r in rows]

    def screenshot(self, screenshot_id: int | None):
        if not screenshot_id: return None
        with self.connect() as connection:
            return connection.execute("SELECT relative_path,media_type FROM screenshots WHERE id=?", (screenshot_id,)).fetchone()


class NotificationWorker:
    def __init__(self, repository: NotificationRepository, providers: ProviderSettings, screenshot_root: Path,
                 lease_seconds=30, max_attempts=5, poll_seconds=.5):
        self.repository, self.providers, self.screenshot_root = repository, providers, screenshot_root
        self.lease_seconds, self.max_attempts, self.poll_seconds = lease_seconds, max_attempts, poll_seconds
        self.owner, self._stop, self._thread = uuid.uuid4().hex, threading.Event(), None

    def start(self):
        self.repository.reconcile(self.providers.public_base_url)
        self._thread = threading.Thread(target=self._run, name="notification-worker", daemon=True); self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(5)

    def _run(self):
        while not self._stop.is_set():
            row = self.repository.claim(self.owner, self.lease_seconds)
            if not row: self._stop.wait(self.poll_seconds); continue
            try:
                message_id = self._send(row); self.repository.sent(row["id"], message_id)
                logger.info(
                    "Notification accepted by provider delivery_id=%s channel=%s message_id=%s",
                    row["id"], row["channel"], message_id or "unavailable",
                )
            except Exception as exc:
                self.repository.failed(row, exc, isinstance(exc, PermanentDeliveryError), self.max_attempts)
                logger.warning(
                    "Notification delivery failed delivery_id=%s channel=%s attempt=%s error=%s",
                    row["id"], row["channel"], row["attempts"], exc,
                )

    def _body(self, row):
        title = "VerifEye test notification" if row["is_test"] else f"VerifEye: {row['outcome'].replace('_',' ')}"
        details = [title, f"Camera: {row['camera_name'] or '—'}", f"Time: {row['occurred_at'] or '—'}"]
        if row["identity_name"]: details.append(f"Identity: {row['identity_name']}")
        if row["event_link"]: details.append(f"View event: {row['event_link']}")
        return title, "\n".join(details)

    def _send(self, row):
        title, body = self._body(row)
        if row["channel"] == "email":
            if not self.providers.email_ready: raise PermanentDeliveryError("SMTP provider is not configured.")
            message = EmailMessage(); message["Subject"] = title; message["From"] = self.providers.smtp_sender
            message["To"] = row["destination"]
            message["Message-ID"] = make_msgid(domain=self.providers.smtp_sender.rsplit("@", 1)[-1])
            message.set_content(body)
            cid, image_html = None, ""
            shot = self.repository.screenshot(row["screenshot_id"])
            image = self.screenshot_root / shot["relative_path"] if shot else None
            if image and image.is_file(): cid = make_msgid(); image_html = f'<p><img src="cid:{cid[1:-1]}" alt="Event capture" style="max-width:100%"></p>'
            message.add_alternative(f"<html><body><p>{html.escape(body).replace(chr(10), '<br>')}</p>{image_html}</body></html>", subtype="html")
            if cid:
                subtype = shot["media_type"].split("/",1)[1]; message.get_payload()[1].add_related(image.read_bytes(), maintype="image", subtype=subtype, cid=cid)
            context = ssl.create_default_context()
            if self.providers.smtp_tls_mode == "ssl": server = smtplib.SMTP_SSL(self.providers.smtp_host, self.providers.smtp_port, timeout=15, context=context)
            else: server = smtplib.SMTP(self.providers.smtp_host, self.providers.smtp_port, timeout=15)
            with server:
                server.ehlo()
                if self.providers.smtp_tls_mode == "starttls":
                    server.starttls(context=context); server.ehlo()
                if self.providers.smtp_username: server.login(self.providers.smtp_username, self.providers.smtp_password)
                refused = server.send_message(message)
                if refused:
                    detail = ", ".join(f"{address}: {response!r}" for address, response in refused.items())
                    raise PermanentDeliveryError(f"SMTP rejected recipient(s): {detail}")
            return message["Message-ID"]
        if not self.providers.sms_ready: raise PermanentDeliveryError("Twilio provider is not configured.")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(self.providers.twilio_account_sid)}/Messages.json"
        data = urllib.parse.urlencode({"To":row["destination"],"From":self.providers.twilio_from_number,"Body":body}).encode()
        request = urllib.request.Request(url, data=data); token = __import__("base64").b64encode(f"{self.providers.twilio_account_sid}:{self.providers.twilio_auth_token}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response: return json.loads(response.read()).get("sid")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if 400 <= exc.code < 500 and exc.code != 429: raise PermanentDeliveryError(f"Twilio rejected the message ({exc.code}): {detail}") from exc
            raise RuntimeError(f"Twilio request failed ({exc.code}).") from exc
