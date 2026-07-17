"""VerifEye composition root and thin HTTP adapter."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import threading

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import AuthError, AuthStore, User
from .cameras import CameraManager, CameraRepository, CameraService, CredentialCipher
from .cameras.models import ActiveCameraLimitReached, CameraError, CameraNotFound, DuplicateCamera, InvalidCameraConfiguration
from .cameras.service import OnvifGateway
from .config import Settings
from .enrollment import EnrollmentError, EnrollmentService
from .recognition import IdentityMatcher, RecognitionEngine
from .storage import EmbeddingStore


PROJECT_DIR = Path(__file__).resolve().parents[3]
FRONTEND_DIR = PROJECT_DIR / "frontend"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class CameraCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True
    sourceType: str = "manual"


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    enabled: bool | None = None


class OnvifCredentials(BaseModel):
    endpoint: str
    username: str
    password: str

class OnvifImport(OnvifCredentials):
    token: str
    name: str = Field(min_length=1, max_length=100)


def database_store(request=None) -> EmbeddingStore:
    settings = request.app.state.settings if request else Settings.from_environment()
    return EmbeddingStore(settings.database)


def current_user(authorization: str | None = Header(default=None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue.")
    settings = app.state.settings if hasattr(app.state, "settings") else Settings.from_environment()
    with EmbeddingStore(settings.database) as store:
        user = AuthStore(store._connection).user_for_session(authorization[7:])
    if user is None: raise HTTPException(401, "Your session has expired. Please sign in again.")
    return user


def user_json(user): return {"id": user.id, "email": user.email, "displayName": user.display_name}
def session_response(user, token): return {"token": token, "user": user_json(user)}


def camera_json(camera, status):
    return {"id": camera.id, "name": camera.name, "host": camera.sanitized_host, "sourceType": camera.source_type,
            "enabled": camera.enabled, "running": status.running, "connectionState": status.connection_state.value,
            "lastFrameAt": status.last_frame_at, "lastError": status.last_error, "retryAttempt": status.retry_attempt,
            "nextRetryAt": status.next_retry_at}


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_environment(); settings.validate()
    cipher = CredentialCipher(settings.camera_key)  # deliberately fails startup for missing/invalid key
    with EmbeddingStore(settings.database): pass
    repository = CameraRepository(settings.database, cipher)
    engine = RecognitionEngine(settings.model_path)
    matcher = IdentityMatcher(settings.database, settings.similarity_threshold)
    manager = CameraManager(repository, engine, matcher, settings.recognition_fps, settings.rtsp_timeout_seconds,
                            settings.cleanup_timeout_seconds, settings.max_active_cameras)
    application.state.settings, application.state.engine = settings, engine
    application.state.manager = manager; application.state.cameras = CameraService(repository, manager)
    application.state.enrollment = EnrollmentService(settings.database, settings.upload_dir, engine)
    application.state.onvif = OnvifGateway()
    try:
        manager.start_enabled()
        yield
    finally:
        manager.shutdown(); engine.close()


app = FastAPI(title="VerifEye", version="0.2.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.exception_handler(CameraError)
async def camera_error(_request, exc):
    from fastapi.responses import JSONResponse
    status = 404 if isinstance(exc, CameraNotFound) else 409 if isinstance(exc, (DuplicateCamera, ActiveCameraLimitReached)) else 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def index(): return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/auth/register", status_code=201)
def register(payload: dict):
    try:
        with EmbeddingStore(app.state.settings.database) as store:
            auth = AuthStore(store._connection); user = auth.create_user(str(payload.get("email", "")), str(payload.get("displayName", "")), str(payload.get("password", ""))); token = auth.create_session(user.id)
    except AuthError as exc: raise HTTPException(400, str(exc)) from exc
    return session_response(user, token)


@app.post("/api/auth/login")
def login(payload: dict):
    try:
        with EmbeddingStore(app.state.settings.database) as store:
            auth = AuthStore(store._connection); user = auth.authenticate(str(payload.get("email", "")), str(payload.get("password", ""))); token = auth.create_session(user.id)
    except AuthError as exc: raise HTTPException(401, str(exc)) from exc
    return session_response(user, token)


@app.get("/api/auth/me")
def me(user=Depends(current_user)): return user_json(user)


@app.post("/api/auth/logout", status_code=204, response_class=Response)
def logout(authorization: str = Header(), _user=Depends(current_user)):
    with EmbeddingStore(app.state.settings.database) as store: AuthStore(store._connection).delete_session(authorization[7:])
    return Response(status_code=204)


@app.post("/api/enroll", status_code=201)
async def enroll(image: UploadFile = File(), user=Depends(current_user)):
    suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if image.content_type not in suffixes: raise HTTPException(415, "Upload a JPEG, PNG, or WebP image.")
    contents = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES: raise HTTPException(413, "Image must be 10 MB or smaller.")
    try: return app.state.enrollment.enroll(user, contents, suffixes[image.content_type], image.filename)
    except EnrollmentError as exc: raise HTTPException(422, str(exc)) from exc


@app.get("/api/cameras")
def list_cameras(_user=Depends(current_user)): return [camera_json(*item) for item in app.state.cameras.list()]


@app.post("/api/cameras", status_code=201)
def create_camera(payload: CameraCreate, _user=Depends(current_user)):
    return camera_json(*app.state.cameras.create(payload.name, payload.url, payload.enabled, payload.sourceType))


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
    except Exception as exc: raise HTTPException(502, "ONVIF authentication or profile lookup failed.") from exc

@app.post("/api/onvif/import", status_code=201)
def onvif_import(payload: OnvifImport, _user=Depends(current_user)):
    from urllib.parse import quote, urlsplit, urlunsplit
    try:
        profiles = app.state.onvif.profiles(payload.endpoint, payload.username, payload.password)
        profile = next(item for item in profiles if item["token"] == payload.token)
        parsed = urlsplit(profile["uri"])
        host = parsed.hostname or ""; netloc = f"{quote(payload.username, safe='')}:{quote(payload.password, safe='')}@{host}"
        if parsed.port: netloc += f":{parsed.port}"
        url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        return camera_json(*app.state.cameras.create(payload.name, url, True, "onvif"))
    except StopIteration as exc: raise HTTPException(400, "The selected ONVIF profile no longer exists.") from exc
    except CameraError: raise
    except Exception as exc: raise HTTPException(502, "ONVIF camera import failed.") from exc
