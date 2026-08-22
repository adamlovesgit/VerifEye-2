"""VerifEye composition root and thin HTTP adapter."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import AuthError, AuthStore, SetupComplete, User
from .cameras import (
    CameraManager, CameraRepository, CameraService, CredentialCipher, InferenceExecutor,
    MediaMTXClient, MediaMTXProcess, MediaMTXSource, RecognitionSessionManager,
)
from .cameras.models import ActiveCameraLimitReached, CameraError, CameraNotFound, DuplicateCamera, InvalidCameraConfiguration
from .cameras.service import OnvifGateway
from .config import Settings
from .enrollment import EnrollmentError, EnrollmentService
from .events import (
    EventDispatcher, EventRepository, InvalidEvent, RecognitionPersistenceSink,
    ScreenshotStorage, parse_utc, utcnow,
)
from .onvif_events import OnvifEventManager
from .recognition import IdentityMatcher, RecognitionEngine
from .notifications import NotificationError, NotificationProviderStore, NotificationRepository, NotificationWorker, ProviderSettings
from .request_diagnostics import RequestDiagnostics
from .storage import EmbeddingStore


PROJECT_DIR = Path(__file__).resolve().parents[3]
FRONTEND_DIR = PROJECT_DIR / "frontend"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)
request_diagnostics = RequestDiagnostics()


class LoginRateLimiter:
    def __init__(self, maximum_attempts: int = 5, window_seconds: int = 60) -> None:
        self.maximum_attempts, self.window_seconds = maximum_attempts, window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [value for value in self._failures.get(key, []) if now - value < self.window_seconds]
            self._failures[key] = recent
            if len(recent) >= self.maximum_attempts:
                retry_after = max(1, int(self.window_seconds - (now - recent[0])))
                raise HTTPException(429, "Too many sign-in attempts. Try again shortly.", headers={"Retry-After": str(retry_after)})

    def failed(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


login_rate_limiter = LoginRateLimiter()


class CameraCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True
    sourceType: str = "manual"
    recognition_url: str | None = Field(default=None, alias="recognitionUrl", max_length=2048)


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    enabled: bool | None = None
    recognition_url: str | None = Field(default=None, alias="recognitionUrl", max_length=2048)


class OnvifCredentials(BaseModel):
    endpoint: str
    username: str
    password: str

class OnvifImport(OnvifCredentials):
    token: str | None = None
    preview_token: str | None = Field(default=None, alias="previewToken")
    recognition_token: str | None = Field(default=None, alias="recognitionToken")
    name: str = Field(min_length=1, max_length=100)


class NotificationRulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: int | None = Field(default=None, alias="identityId")
    rule_type: Literal["identity", "unknown_face", "no_face", "system_error"] = Field(alias="ruleType")
    email_address: str | None = Field(default=None, alias="emailAddress", max_length=320)
    phone_number: str | None = Field(default=None, alias="phoneNumber", max_length=32)
    email_enabled: bool = Field(default=False, alias="emailEnabled")
    sms_enabled: bool = Field(default=False, alias="smsEnabled")
    camera_ids: list[int] = Field(default_factory=list, alias="cameraIds")
    version: int | None = None


class NotificationTestPayload(BaseModel):
    rule_id: int = Field(alias="ruleId")
    channel: str


class SmtpSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="", max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=1024)
    sender: str = Field(default="", max_length=320)
    tls_mode: Literal["starttls", "ssl", "none"] = Field(default="starttls", alias="tlsMode")
    clear_password: bool = Field(default=False, alias="clearPassword")


class MediaAuthRequest(BaseModel):
    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str
    path: str = ""
    protocol: str = ""


class GuestCredentials(BaseModel):
    email: str = Field(max_length=254)
    display_name: str = Field(alias="displayName", min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=1024)


def sanitized_onvif_endpoint(value: str) -> str:
    """Return a diagnostic endpoint without credentials, query, or fragment."""
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    host = parsed.hostname or "unknown-host"
    if ":" in host: host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def database_store(request=None) -> EmbeddingStore:
    settings = request.app.state.settings if request else Settings.from_environment()
    return EmbeddingStore(settings.database)


def authenticated_user(authorization: str | None = Header(default=None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue.")
    settings = app.state.settings if hasattr(app.state, "settings") else Settings.from_environment()
    with EmbeddingStore(settings.database) as store:
        user = AuthStore(store._connection).user_for_session(authorization[7:])
    if user is None: raise HTTPException(401, "Your session has expired. Please sign in again.")
    return user


def current_user(user: User = Depends(authenticated_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator access is required.")
    return user


def user_json(user): return {"id": user.id, "email": user.email, "displayName": user.display_name, "role": user.role}
def session_response(user, token): return {"token": token, "user": user_json(user)}


def camera_json(camera, status):
    media = getattr(app.state, "media", None)
    return {"id": camera.id, "name": camera.name, "host": camera.sanitized_host, "sourceType": camera.source_type,
            "enabled": camera.enabled, "running": status.running, "connectionState": status.connection_state.value,
            "lastFrameAt": status.last_frame_at, "lastError": status.last_error, "retryAttempt": status.retry_attempt,
            "nextRetryAt": status.next_retry_at, "hasRecognitionStream": camera.recognition_url is not None,
            "recognitionSessionState": status.recognition_session_state.value,
            "recognitionStreamMode": status.recognition_stream_mode.value,
            "recognitionSessionStartedAt": status.recognition_session_started_at,
            "recognitionDeadline": status.recognition_deadline,
            "recognitionMaximumDeadline": status.recognition_maximum_deadline,
            "recognitionError": status.recognition_error,
            "previewUrl": media.preview_url(camera.id).url if media else None,
            "mediaReady": getattr(status, "media_ready", False),
            "preRollReady": getattr(status, "pre_roll_ready", False),
            "preRollLastFrameAt": getattr(status, "pre_roll_last_frame_at", None),
            "preRollError": getattr(status, "pre_roll_error", None)}


def guest_camera_json(camera, status):
    return {
        "id": camera.id,
        "name": camera.name,
        "connectionState": status.connection_state.value,
        "previewAvailable": bool(camera.enabled and status.running),
    }


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_environment(); settings.validate()
    logger.info("VerifEye database: %s", settings.database.resolve())
    cipher = CredentialCipher(settings.camera_key)  # deliberately fails startup for missing/invalid key
    with EmbeddingStore(settings.database): pass
    repository = CameraRepository(settings.database, cipher)
    media_client = MediaMTXClient(settings.mediamtx_api_url)
    media_process = MediaMTXProcess(
        PROJECT_DIR / "backend" / "vendor" / "mediamtx" / "1.19.3",
        settings.mediamtx_runtime_dir, media_client,
        settings.mediamtx_rtsp_url, settings.mediamtx_whep_url,
    )
    media = MediaMTXSource(media_client, media_process, settings.mediamtx_rtsp_url, settings.mediamtx_whep_url)
    engine = RecognitionEngine(settings.model_path)
    matcher = IdentityMatcher(settings.database, settings.similarity_threshold)
    manager = CameraManager(
        repository, media, settings.pre_roll_fps, settings.rtsp_timeout_seconds,
        settings.cleanup_timeout_seconds, settings.max_active_cameras,
        pre_roll_seconds=settings.pre_roll_seconds, pre_roll_max_frames=settings.pre_roll_max_frames,
        preview_fps=settings.preview_fps,
    )
    event_repository = EventRepository(settings.database, settings.sqlite_busy_timeout_ms)
    screenshots = ScreenshotStorage(settings.event_screenshot_dir)
    event_repository.reconcile()
    screenshots.cleanup_orphans(event_repository.referenced_paths())
    sink = RecognitionPersistenceSink(event_repository, screenshots)
    sessions = RecognitionSessionManager(
        repository, manager, media, InferenceExecutor(engine, matcher), sink,
        settings.recognition_fps, settings.rtsp_timeout_seconds,
    )
    manager.bind_sessions(sessions)
    dispatcher = EventDispatcher(
        event_repository, sessions, settings.pre_roll_seconds, settings.recognition_window_seconds,
        settings.max_recognition_session_seconds, settings.event_dispatch_lease_seconds,
        settings.event_dispatch_max_attempts, screenshot_storage=screenshots,
        no_face_retention_days=settings.motion_no_face_retention_days,
        unrecognized_retention_days=settings.motion_unrecognized_retention_days,
    )
    application.state.settings, application.state.engine = settings, engine
    application.state.manager, application.state.media = manager, media
    application.state.media_process, application.state.sessions = media_process, sessions
    application.state.onvif = OnvifGateway()
    onvif_events = OnvifEventManager(
        repository, event_repository, application.state.onvif,
        cooldown_seconds=settings.onvif_motion_cooldown_seconds,
    )
    application.state.onvif_events = onvif_events
    application.state.cameras = CameraService(repository, manager, onvif_events)
    application.state.enrollment = EnrollmentService(settings.database, settings.upload_dir, engine)
    application.state.events, application.state.screenshots = event_repository, screenshots
    application.state.dispatcher = dispatcher
    providers = ProviderSettings(
        settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password,
        settings.smtp_sender, settings.smtp_tls_mode, settings.twilio_account_sid,
        settings.twilio_auth_token, settings.twilio_from_number, settings.public_base_url,
    )
    provider_store = NotificationProviderStore(settings.database, cipher)
    providers = provider_store.load(providers)
    notifications = NotificationRepository(settings.database, settings.sqlite_busy_timeout_ms)
    notification_worker = NotificationWorker(notifications, providers, settings.event_screenshot_dir)
    sink.notification_callback = lambda session_id: notifications.enqueue_session(session_id, providers.public_base_url)
    application.state.notifications, application.state.notification_providers = notifications, providers
    application.state.notification_provider_store = provider_store
    application.state.notification_worker = notification_worker
    try:
        media_process.start()
        media.reconcile(repository.list())
        media_process.on_ready = lambda: media.reconcile(repository.list())
        manager.start_enabled()
        onvif_events.start_enabled()
        dispatcher.start()
        notification_worker.start()
        yield
    finally:
        onvif_events.shutdown(); dispatcher.stop(); notification_worker.stop(); manager.shutdown()
        media_process.stop(); engine.close()


app = FastAPI(title="VerifEye", version="0.2.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.middleware("http")
async def diagnose_requests_and_disable_frontend_cache(request, call_next):
    diagnostic, timer = request_diagnostics.begin(request.method, request.url.path)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = diagnostic.request_id
        if request.url.path in {"/", "/guest/cameras"} or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        request_diagnostics.finish(diagnostic, timer, response.status_code)
        return response
    except BaseException:
        request_diagnostics.finish(diagnostic, timer, None, failed=True)
        raise



@app.exception_handler(CameraError)
async def camera_error(_request, exc):
    from fastapi.responses import JSONResponse
    status = 404 if isinstance(exc, CameraNotFound) else 409 if isinstance(exc, (DuplicateCamera, ActiveCameraLimitReached)) else 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def index(): return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/guest/cameras", include_in_schema=False)
def guest_index(): return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/internal/media-auth", include_in_schema=False)
def media_auth(payload: MediaAuthRequest):
    """Delegate MediaMTX reads to existing VerifEye authorization state."""
    match = re.fullmatch(r"verifeye-camera-(\d+)-(preview|recognition)", payload.path)
    if payload.action != "read" or match is None:
        raise HTTPException(401, "Media read is not authorized.")
    camera_id, role = int(match.group(1)), match.group(2)
    try:
        camera = app.state.cameras.repository.get(camera_id)
    except CameraNotFound as exc:
        raise HTTPException(401, "Media read is not authorized.") from exc
    if not camera.enabled or (role == "recognition" and not camera.recognition_url):
        raise HTTPException(401, "Media read is not authorized.")
    loopback = payload.ip == "::1" or payload.ip.startswith("127.")
    if payload.protocol == "rtsp" and loopback:
        return Response(status_code=204)
    if payload.protocol != "webrtc" or role != "preview" or not payload.token:
        raise HTTPException(401, "Media read is not authorized.")
    with EmbeddingStore(app.state.settings.database) as store:
        auth = AuthStore(store._connection)
        user = auth.user_for_session(payload.token)
        authorized = bool(user and user.role == "admin") or bool(
            auth.user_for_preview_grant(payload.token, camera_id)
        )
    if not authorized:
        raise HTTPException(401, "Media read is not authorized.")
    return Response(status_code=204)


@app.get("/api/auth/setup")
def setup_status():
    with EmbeddingStore(app.state.settings.database) as store:
        return {"setupRequired": AuthStore(store._connection).setup_required()}


@app.post("/api/auth/register", status_code=201)
def register(payload: dict):
    try:
        with EmbeddingStore(app.state.settings.database) as store:
            auth = AuthStore(store._connection); user = auth.create_initial_admin(str(payload.get("email", "")), str(payload.get("displayName", "")), str(payload.get("password", ""))); token = auth.create_session(user.id)
    except SetupComplete as exc: raise HTTPException(409, str(exc)) from exc
    except AuthError as exc: raise HTTPException(400, str(exc)) from exc
    return session_response(user, token)


@app.post("/api/auth/login")
def login(payload: dict, request: Request):
    client_key = request.client.host if request.client else "unknown"
    login_rate_limiter.check(client_key)
    try:
        with EmbeddingStore(app.state.settings.database) as store:
            auth = AuthStore(store._connection); user = auth.authenticate(str(payload.get("email", "")), str(payload.get("password", ""))); token = auth.create_session(user.id)
    except AuthError as exc:
        login_rate_limiter.failed(client_key)
        raise HTTPException(401, str(exc)) from exc
    login_rate_limiter.succeeded(client_key)
    return session_response(user, token)


@app.get("/api/auth/me")
def me(user=Depends(authenticated_user)): return user_json(user)


@app.post("/api/auth/logout", status_code=204, response_class=Response)
def logout(authorization: str = Header(), _user=Depends(authenticated_user)):
    with EmbeddingStore(app.state.settings.database) as store: AuthStore(store._connection).delete_session(authorization[7:])
    return Response(status_code=204)


@app.get("/api/admin/guest")
def get_guest(_user=Depends(current_user)):
    with EmbeddingStore(app.state.settings.database) as store:
        guest = AuthStore(store._connection).guest()
    return {"configured": guest is not None, "guest": user_json(guest) if guest else None}


@app.put("/api/admin/guest")
def put_guest(payload: GuestCredentials, _user=Depends(current_user)):
    try:
        with EmbeddingStore(app.state.settings.database) as store:
            guest = AuthStore(store._connection).replace_guest(
                payload.email, payload.display_name, payload.password
            )
    except AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"configured": True, "guest": user_json(guest)}


@app.delete("/api/admin/guest", status_code=204)
def delete_guest(_user=Depends(current_user)):
    with EmbeddingStore(app.state.settings.database) as store:
        AuthStore(store._connection).revoke_guest()
    return Response(status_code=204)


@app.get("/api/guest/cameras")
def list_guest_cameras(_user=Depends(authenticated_user)):
    return [guest_camera_json(*item) for item in app.state.cameras.list()]


@app.post("/api/guest/cameras/{camera_id}/preview-authorization")
def authorize_guest_preview(camera_id: int, user=Depends(authenticated_user)):
    camera = app.state.cameras.repository.get(camera_id)
    status = app.state.manager.status(camera_id)
    if not camera.enabled or not status.running:
        raise HTTPException(409, "Camera preview is unavailable.")
    with EmbeddingStore(app.state.settings.database) as store:
        token = AuthStore(store._connection).create_preview_grant(user.id, camera_id)
    return {"url": app.state.media.preview_url(camera_id).url, "token": token, "expiresIn": 90}


@app.post("/api/enroll", status_code=201)
async def enroll(name: str = Form(), image: UploadFile = File(), _user=Depends(current_user)):
    suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if image.content_type not in suffixes: raise HTTPException(415, "Upload a JPEG, PNG, or WebP image.")
    contents = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES: raise HTTPException(413, "Image must be 10 MB or smaller.")
    try: return app.state.enrollment.enroll(name, contents, suffixes[image.content_type], image.filename)
    except EnrollmentError as exc: raise HTTPException(422, str(exc)) from exc


def embedding_json(record):
    return {"id": record.id, "modelName": record.model_name, "dimensions": int(record.embedding.size),
            "sourcePath": record.source_path, "detectionScore": record.detection_score,
            "metadata": record.metadata, "createdAt": record.created_at}


@app.get("/api/identities")
def list_identities(_user=Depends(current_user)):
    with EmbeddingStore(app.state.settings.database) as store:
        return [{"id": identity.id, "externalId": identity.external_id, "displayName": identity.display_name,
                 "createdAt": identity.created_at, "updatedAt": identity.updated_at,
                 "embeddings": [embedding_json(item) for item in identity.embeddings]}
                for identity in store.list_identities()]


@app.get("/api/embeddings/{embedding_id}/reference-image")
def get_embedding_reference_image(embedding_id: int, _user=Depends(current_user)):
    try:
        contents = app.state.enrollment.reference_image(embedding_id)
    except KeyError as exc:
        raise HTTPException(404, "Face record not found.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "The enrollment reference image is unavailable.") from exc
    except EnrollmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(contents, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.delete("/api/identities/{identity_id}", status_code=204)
def delete_identity(identity_id: int, _user=Depends(current_user)):
    with EmbeddingStore(app.state.settings.database) as store:
        try: source_paths = store.delete_identity(identity_id)
        except KeyError as exc: raise HTTPException(404, "Identity not found.") from exc
    upload_dir = Path(app.state.settings.upload_dir).resolve()
    for source_path in source_paths:
        target = (upload_dir / source_path).resolve()
        if target.is_relative_to(upload_dir): target.unlink(missing_ok=True)
    return Response(status_code=204)


@app.get("/api/notification-settings")
def notification_settings(_user=Depends(current_user)):
    value = app.state.notifications.settings()
    providers = app.state.notification_providers
    value["providers"] = {"email": {"ready": providers.email_ready, "host": providers.smtp_host,
        "port": providers.smtp_port, "username": providers.smtp_username, "sender": providers.smtp_sender,
        "tlsMode": providers.smtp_tls_mode, "passwordConfigured": bool(providers.smtp_password)},
        "sms": {"ready": providers.sms_ready}}
    return value


@app.put("/api/notification-settings/smtp")
def update_smtp_settings(payload: SmtpSettingsPayload, _user=Depends(current_user)):
    try:
        providers = app.state.notification_provider_store.save_smtp(
            payload.model_dump(by_alias=True), app.state.notification_providers
        )
    except NotificationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ready": providers.email_ready, "host": providers.smtp_host, "port": providers.smtp_port,
            "username": providers.smtp_username, "sender": providers.smtp_sender,
            "tlsMode": providers.smtp_tls_mode, "passwordConfigured": bool(providers.smtp_password)}


def notification_payload(payload: NotificationRulePayload) -> dict:
    return payload.model_dump(by_alias=True)


@app.post("/api/notification-rules", status_code=201)
def create_notification_rule(payload: NotificationRulePayload, _user=Depends(current_user)):
    try: rule_id = app.state.notifications.save_rule(notification_payload(payload))
    except NotificationError as exc: raise HTTPException(422, str(exc)) from exc
    return {"id": rule_id}


@app.put("/api/notification-rules/{rule_id}")
def update_notification_rule(rule_id: int, payload: NotificationRulePayload, _user=Depends(current_user)):
    try: app.state.notifications.save_rule(notification_payload(payload), rule_id)
    except NotificationError as exc: raise HTTPException(409 if "changed elsewhere" in str(exc) else 422, str(exc)) from exc
    return {"id": rule_id}


@app.delete("/api/notification-rules/{rule_id}", status_code=204)
def delete_notification_rule(rule_id: int, _user=Depends(current_user)):
    if not app.state.notifications.delete_rule(rule_id): raise HTTPException(404, "Notification rule not found.")
    return Response(status_code=204)


@app.post("/api/notification-tests", status_code=202)
def test_notification(payload: NotificationTestPayload, _user=Depends(current_user)):
    providers = app.state.notification_providers
    if payload.channel == "email" and not providers.email_ready: raise HTTPException(409, "SMTP provider is not configured.")
    if payload.channel == "sms" and not providers.sms_ready: raise HTTPException(409, "Twilio provider is not configured.")
    try: delivery_id = app.state.notifications.enqueue_test(
        payload.rule_id, payload.channel, providers.public_base_url
    )
    except NotificationError as exc: raise HTTPException(422, str(exc)) from exc
    return {"id": delivery_id, "status": "queued"}


@app.get("/api/notification-deliveries")
def notification_deliveries(limit: int = 50, offset: int = 0, status: str | None = None,
                            channel: str | None = None, _user=Depends(current_user)):
    if not 1 <= limit <= 200 or offset < 0: raise HTTPException(422, "Invalid pagination.")
    if status and status not in {"queued","claimed","retrying","sent","failed"}: raise HTTPException(422, "Invalid status filter.")
    if channel and channel not in {"email","sms"}: raise HTTPException(422, "Invalid channel filter.")
    return app.state.notifications.deliveries(limit, offset, status, channel)


@app.get("/api/cameras")
def list_cameras(_user=Depends(current_user)): return [camera_json(*item) for item in app.state.cameras.list()]


@app.post("/api/cameras", status_code=201)
def create_camera(payload: CameraCreate, _user=Depends(current_user)):
    return camera_json(*app.state.cameras.create(
        payload.name, payload.url, payload.enabled, payload.sourceType,
        payload.recognition_url,
    ))


@app.patch("/api/cameras/{camera_id}")
def update_camera(camera_id: int, payload: CameraUpdate, _user=Depends(current_user)):
    return camera_json(*app.state.cameras.update(camera_id, **payload.model_dump(exclude_unset=True)))


@app.delete("/api/cameras/{camera_id}", status_code=204)
def delete_camera(camera_id: int, _user=Depends(current_user)): app.state.cameras.delete(camera_id); return Response(status_code=204)


@app.post("/api/cameras/{camera_id}/start")
def start_camera(camera_id: int, _user=Depends(current_user)):
    camera = app.state.cameras.repository.get(camera_id); return camera_json(camera, app.state.cameras.start(camera_id))


@app.post("/api/cameras/{camera_id}/stop")
def stop_camera(camera_id: int, _user=Depends(current_user)):
    camera = app.state.cameras.repository.get(camera_id); return camera_json(camera, app.state.cameras.stop(camera_id))


def event_summary(event):
    return {"id": event.id, "state": event.state, "acceptedAt": event.accepted_at,
            "inspectionPath": f"/api/camera-events/{event.id}"}


def valid_image_signature(contents: bytes, media_type: str) -> bool:
    return ((media_type == "image/jpeg" and contents.startswith(b"\xff\xd8\xff"))
            or (media_type == "image/png" and contents.startswith(b"\x89PNG\r\n\x1a\n"))
            or (media_type == "image/webp" and len(contents) >= 12
                and contents[:4] == b"RIFF" and contents[8:12] == b"WEBP"))


@app.post("/api/cameras/{camera_id}/event-token", status_code=201)
def issue_camera_event_token(camera_id: int, _user=Depends(current_user)):
    app.state.cameras.repository.get(camera_id)
    token_id, token = app.state.events.issue_token(camera_id)
    return {"id": token_id, "cameraId": camera_id, "token": token}


@app.post("/api/cameras/{camera_id}/event-token/rotate", status_code=201)
def rotate_camera_event_token(camera_id: int, _user=Depends(current_user)):
    app.state.cameras.repository.get(camera_id)
    app.state.events.revoke_tokens(camera_id)
    token_id, token = app.state.events.issue_token(camera_id)
    return {"id": token_id, "cameraId": camera_id, "token": token}


@app.delete("/api/cameras/{camera_id}/event-token", status_code=204)
def revoke_camera_event_token(camera_id: int, _user=Depends(current_user)):
    app.state.cameras.repository.get(camera_id)
    app.state.events.revoke_tokens(camera_id)
    return Response(status_code=204)


@app.delete("/api/cameras/{camera_id}/event-tokens/{token_id}", status_code=204)
def revoke_specific_camera_event_token(camera_id: int, token_id: int, _user=Depends(current_user)):
    app.state.cameras.repository.get(camera_id)
    if not app.state.events.revoke_token(camera_id, token_id):
        raise HTTPException(404, "Camera event token not found.")
    return Response(status_code=204)


@app.post("/api/cameras/{camera_id}/events")
async def ingest_camera_event(
    camera_id: int,
    source_event_id: str = Form(alias="sourceEventId", min_length=1, max_length=200),
    event_type: str = Form(alias="eventType", min_length=1, max_length=100),
    occurred_at: str = Form(alias="occurredAt"),
    metadata: str = Form(default="{}"),
    screenshot: UploadFile | None = File(default=None),
    event_token: str | None = Header(default=None, alias="X-Camera-Event-Token"),
):
    if not event_token or not app.state.events.authenticate_camera_token(camera_id, event_token):
        raise HTTPException(401, "Invalid or revoked camera event token.")
    source_event_id = source_event_id.strip()
    existing = app.state.events.existing_event(camera_id, source_event_id)
    if existing:
        return JSONResponse(event_summary(existing), status_code=200)
    try:
        occurred = parse_utc(occurred_at)
        now, settings = utcnow(), app.state.settings
        if occurred < now - timedelta(seconds=settings.event_max_age_seconds):
            raise InvalidEvent("Event timestamp is too old.")
        if occurred > now + timedelta(seconds=settings.event_future_skew_seconds):
            raise InvalidEvent("Event timestamp is too far in the future.")
        metadata_value = json.loads(metadata)
        if not isinstance(metadata_value, dict):
            raise InvalidEvent("Metadata must be a JSON object.")
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finalized = None
    try:
        screenshot_record = None
        if screenshot is not None:
            contents = await screenshot.read(MAX_UPLOAD_BYTES + 1)
            if len(contents) > MAX_UPLOAD_BYTES:
                raise InvalidEvent("Screenshot must be 10 MB or smaller.")
            media_type = screenshot.content_type or ""
            if not valid_image_signature(contents, media_type):
                raise InvalidEvent("Screenshot content does not match a supported image type.")
            finalized, screenshot_record = app.state.screenshots.stage(contents, media_type)
        event = app.state.events.accept_event(
            camera_id, source_event_id, event_type.strip(), occurred, metadata_value, screenshot_record
        )
        if not event.created and finalized:
            finalized.unlink(missing_ok=True)
        return JSONResponse(event_summary(event), status_code=202 if event.created else 200)
    except (InvalidEvent, sqlite3.IntegrityError) as exc:
        if finalized:
            finalized.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        if finalized:
            finalized.unlink(missing_ok=True)
        raise


@app.get("/api/camera-events")
def list_camera_events(
    camera_id: int | None = None, state: str | None = None, outcome: str | None = None,
    accepted_after: str | None = None, accepted_before: str | None = None,
    limit: int = 50, offset: int = 0, _user=Depends(current_user),
):
    if not 1 <= limit <= 200 or offset < 0:
        raise HTTPException(422, "Invalid pagination.")
    try:
        return app.state.events.list_events(
            limit=limit, offset=offset, camera_id=camera_id, state=state, outcome=outcome,
            accepted_after=accepted_after, accepted_before=accepted_before,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/camera-events/{event_id}")
def get_camera_event(event_id: int, _user=Depends(current_user)):
    event = app.state.events.get_event(event_id)
    if event is None:
        raise HTTPException(404, "Camera event not found.")
    for shot in event["screenshots"]:
        shot["contentPath"] = f"/api/screenshots/{shot['id']}"
    return event


@app.get("/api/screenshots/{screenshot_id}")
def get_screenshot(screenshot_id: int, _user=Depends(current_user)):
    record = app.state.events.screenshot(screenshot_id)
    if record is None:
        raise HTTPException(404, "Screenshot not found.")
    path = app.state.screenshots.resolve(record["relative_path"])
    if not path.is_file():
        raise HTTPException(404, "Screenshot file is unavailable.")
    return FileResponse(path, media_type=record["media_type"])


@app.get("/api/cameras/{camera_id}/stream")
def stream_camera(camera_id: int, _user=Depends(current_user)):
    app.state.cameras.repository.get(camera_id)
    publisher = app.state.manager.publisher(camera_id)
    if publisher is None: raise HTTPException(409, "Camera is not running.")
    def frames():
        sequence = 0
        while True:
            sequence, jpeg = publisher.wait_after(sequence)
            if jpeg is None: return
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/api/onvif/discover")
def discover_onvif(_user=Depends(current_user)): return app.state.onvif.discover()


@app.post("/api/onvif/profiles")
def onvif_profiles(payload: OnvifCredentials, _user=Depends(current_user)):
    try: return [{"token": item["token"], "name": item["name"]} for item in app.state.onvif.profiles(payload.endpoint, payload.username, payload.password)]
    except Exception as exc:
        logger.exception(
            "ONVIF profile lookup failed for %s (%s)",
            sanitized_onvif_endpoint(payload.endpoint), type(exc).__name__,
        )
        raise HTTPException(502, "ONVIF authentication or profile lookup failed.") from exc

@app.post("/api/onvif/import", status_code=201)
def onvif_import(payload: OnvifImport, _user=Depends(current_user)):
    from urllib.parse import quote, urlsplit, urlunsplit
    def authenticated_url(profile):
        parsed = urlsplit(profile["uri"])
        host = parsed.hostname or ""; netloc = f"{quote(payload.username, safe='')}:{quote(payload.password, safe='')}@{host}"
        if parsed.port: netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    try:
        profiles = app.state.onvif.profiles(payload.endpoint, payload.username, payload.password)
        preview_token = payload.preview_token or payload.token
        if not preview_token: raise StopIteration
        preview = next(item for item in profiles if item["token"] == preview_token)
        recognition = next((item for item in profiles if item["token"] == payload.recognition_token), None)
        created = app.state.cameras.create(
            payload.name, authenticated_url(preview), True, "onvif",
            authenticated_url(recognition) if recognition else None,
            payload.endpoint, payload.username, payload.password,
        )
        return camera_json(*created)
    except StopIteration as exc: raise HTTPException(400, "The selected ONVIF profile no longer exists.") from exc
    except CameraError: raise
    except Exception as exc:
        logger.exception(
            "ONVIF camera import failed for %s (%s)",
            sanitized_onvif_endpoint(payload.endpoint), type(exc).__name__,
        )
        raise HTTPException(502, "ONVIF camera import failed.") from exc
