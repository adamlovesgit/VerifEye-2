VerifEye 2.0 Charter

VerifEye 2.0 is designed to be a self hosted security system with minimal cloud coupling. The reason for this ethos is for the consumer to own the full lifecycle of data, not the vendor. 

The architecture for this application is inintally designed to be local first modular monolith. This means: one backend application, one database, and one local web interface. 

The recognition pipeline is migrated from VerifEye 1.0, a school project where the core logic for this project was first designed and implemented. 

## Development setup

Create and activate a virtual environment, then install the complete application
and testing dependency set from the project root:

```powershell
python -m venv .venv
# PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Linux or macOS, activate the environment with:

```bash
source .venv/bin/activate
```

The root requirements file includes `backend/requirements-dev.txt`, which in
turn includes the web and vision dependency sets. Those smaller files remain
available when only part of the application is needed.

## Recognition pipeline integration test

Install the recognition dependencies into the active Python environment:

```powershell
python -m pip install -r backend/requirements-vision.txt
```

Run the pipeline against a clear image containing at least one face:

```powershell
python backend/tests/integration/recognize_image.py C:\path\to\face.jpg
```

The test uses the ArcFace model at
`~/.insightface/models/buffalo_l/w600k_r50.onnx` by default. Use
`--model-path C:\path\to\w600k_r50.onnx` when the model is elsewhere.

Successful runs write two files under `backend/test-output`:

- `<name>_annotated.jpg` shows the raw bounding box, padded bounding box,
  landmarks, and detection score.
- `<name>_recognition.json` contains those values plus the complete embedding,
  its dtype, shape, and L2 norm.

The command exits unsuccessfully if it cannot detect a face or if the aligned
image and embedding violate the pipeline contract.

## Local embedding database

Embeddings are stored locally in SQLite as normalized `float32` blobs. The
schema records model name, vector dimensions, source image, detection score,
and JSON metadata so vectors from incompatible recognition models are never
compared.

```python
from verifeye.storage import EmbeddingStore

with EmbeddingStore("backend/data/verifeye.db") as store:
    person_id = store.upsert_identity("employee-42", "Example Person")
    store.add_embedding(person_id, face.embedding, source_path="camera-1.jpg")
    matches = store.find_matches(face.embedding, min_similarity=0.4)
```

The database file and its parent directory are created on first use. Run the
storage tests with `python -m unittest discover -s backend/tests/unit`.

## Local web interface

The first local account becomes the sole administrator. After setup, the
administrator can create one shared guest login from the camera dashboard.
Administrators retain the full interface; guests are routed to a dedicated
preview-only camera page. Guest credential rotation and revocation invalidate
all guest sessions.

Install and start it from the project root:

```powershell
python -m pip install -r backend/requirements-web.txt
$env:PYTHONPATH = "backend/src"
python -m uvicorn verifeye.app:app --reload
```

Every HTTP request is logged with a request ID, method, path, status, and
elapsed time. If a request is still running after 10 seconds, VerifEye logs the
active requests and dumps every Python thread stack to the server console. Set
`VERIFEYE_SLOW_REQUEST_SECONDS` before startup to change the threshold, or set
it to `0` to disable slow-request stack dumps. Request bodies and query strings
are intentionally excluded from these diagnostics.

Then open `http://127.0.0.1:8000`. The recognition model defaults to
`~/.insightface/models/buffalo_l/w600k_r50.onnx`. Set
`VERIFEYE_MODEL_PATH` before starting the server if it is stored elsewhere.

Uploaded enrollment photos stay under `backend/data/enrollments`; the image
and its normalized embedding are never sent to a cloud service. Cameras,
identities, events, recognition results, and notification settings belong to
the installation. Only the administrator can read or change this data; guests
receive a non-secret camera list and short-lived preview grants.

If the administrator password is lost, stop VerifEye and run the offline
recovery command from the project root. Back up the database first. The command
changes only the administrator password and revokes all administrator sessions:

```powershell
python backend/scripts/reset_admin_password.py --database backend/data/verifeye.db
```

## Resetting local data after schema changes

Plan B automatically promotes a single legacy Plan A account to administrator.
Databases with multiple legacy users or other older development layouts still
require a reset. Before starting this version against unsupported local data,
stop VerifEye and rename the data directory so the reset is recoverable:

```powershell
Rename-Item -LiteralPath backend\data -NewName "data-backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

Start VerifEye normally to create a new `backend/data` directory and database.
Keep the renamed directory until the reset has been verified; VerifEye never
deletes or converts it automatically.

For custom locations, back up or rename each configured path instead. The
corresponding settings are `VERIFEYE_DATABASE` for the SQLite file,
`VERIFEYE_UPLOAD_DIR` for enrollment images,
`VERIFEYE_EVENT_SCREENSHOT_DIR` for event images, and
`VERIFEYE_MEDIAMTX_RUNTIME_DIR` for MediaMTX runtime files. Point all four at
fresh locations before startup when performing a full reset.

## Live camera media and recognition

VerifEye supervises up to four enabled RTSP cameras by default. The bundled,
SHA-256-verified MediaMTX 1.19.3 process owns the only camera-side pull of each
lightweight preview stream. Browser H.264/WHEP playback and the backend
`PreRollCapture` decoder are independent downstream readers of that same
`verifeye-camera-{id}-preview` path. In particular, pre-roll does not open the
camera URL or create a second lightweight path.

Each enabled camera has one continuous, bounded pre-roll decoder. Recognition
runs only for durable recognition sessions: it either opens one temporary
distinct/main-stream decoder, shares new pre-roll samples when no distinct
stream is configured, or falls back once to those samples if the distinct
stream fails. A failed distinct stream is not retried until the next session.
ONVIF PullPoint ingestion and durable event dispatch continue while MediaMTX or
a decoder is temporarily unavailable.

Generate and securely back up a camera-credential encryption key before
starting the application:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:VERIFEYE_CAMERA_KEY = "paste-the-generated-key"
```

Startup intentionally fails when this key is absent, malformed, or unable to
decrypt saved camera credentials. VerifEye never creates a replacement key.
Use the dashboard to enter a manual `rtsp://`/`rtsps://` URL or explicitly run
ONVIF discovery and import a media profile. Manual URL validation never scans
the network. Wired and Wi-Fi cameras are equivalent to VerifEye; the host only
needs a routable LAN connection to the endpoint. Firewall rules, client
isolation, weak Wi-Fi, and packet loss appear as connection failures or retries.

Backend decoders use FFmpeg/PyAV over loopback RTSP/TCP; browsers use the
vendored MediaMTX 1.19.3 WHEP reader and their existing VerifEye bearer. The
private MediaMTX auth callback delegates to existing VerifEye sessions and does
not maintain separate media credentials. Actual codec compatibility depends on
the installed FFmpeg and browser builds. The recognition threshold defaults to a provisional
`0.40`; validate it against a representative labeled golden set before treating
it as a production decision. A golden-set NPZ must contain `embeddings` (`N x D`)
and matching `labels` (`N`); evaluate it with:

```powershell
python backend/tests/integration/evaluate_threshold.py golden-set.npz --threshold 0.40
```

The dependency direction is deliberately one-way:

`HTTP routes -> application services -> domain ports <- infrastructure adapters`

Routes do not decode video, invoke recognition models, query camera tables, or
own worker threads. `CameraManager` owns provisioning and pre-roll lifecycle;
`RecognitionSessionManager` owns mode selection and monotonic session timing;
`InferenceExecutor` owns per-session detection and access to the single
process-wide ArcFace engine. Stop, delete, and shutdown close sources, clear
buffers, join workers, and only then release shared recognition resources.

An initial MediaMTX extraction, version, or Control API health failure aborts
startup. A later child-process exit instead marks camera media unavailable while
the API, ONVIF ingestion, and durable state stay online; the supervisor retries
with bounded monotonic backoff and reconciles paths after recovery.

For an opt-in test against real hardware (no frames are saved):

```powershell
$env:VERIFEYE_TEST_RTSP_URL = "rtsp://user:password@camera.local/stream"
python backend/tests/integration/live_camera.py --seconds 10
```

Optional tuning variables are `VERIFEYE_RECOGNITION_FPS`, `VERIFEYE_PREVIEW_FPS`,
`VERIFEYE_PRE_ROLL_FPS`, `VERIFEYE_SIMILARITY_THRESHOLD`, `VERIFEYE_FRAME_FRESHNESS_SECONDS`,
`VERIFEYE_RTSP_TIMEOUT_SECONDS`, `VERIFEYE_CLEANUP_TIMEOUT_SECONDS`, and
`VERIFEYE_MAX_ACTIVE_CAMERAS`. ONVIF motion handling can be tuned with
`VERIFEYE_ONVIF_MOTION_COOLDOWN_SECONDS` (default `20`),
`VERIFEYE_MOTION_NO_FACE_RETENTION_DAYS` (default `7`), and
`VERIFEYE_MOTION_UNRECOGNIZED_RETENTION_DAYS` (default `30`). Recognized motion
events are retained.

The legacy MJPEG endpoint, JPEG publisher, and preview-FPS setting remain as a
temporary compatibility cutover. They are intentionally removed together only
after the documented one-camera WHEP + pre-roll + ONVIF + recognition + overlap
acceptance run passes. The dashboard already uses WHEP.

The pinned Control API subprocess contract and the two-browser source invariant
are opt-in integration checks:

```powershell
$env:VERIFEYE_RUN_MEDIAMTX_CONTRACT = "1"
python -m unittest backend.tests.integration.test_mediamtx_contract

# Requires Playwright Chromium, a running VerifEye instance, and an enabled camera.
python backend/tests/integration/mediamtx_multi_browser.py --camera-id 1 --token "existing-session-token"
```

## Email and SMS notifications

The Notifications page stores per-identity rules plus three independent session-level
rules locally: unknown face (a detected face is not enrolled), no face (the entire
session contains no face detections), and system fallback (processing errors). Each
session-level rule queues at most one notification per completed recognition session. Provider
credentials are read only from the server environment and are never returned by the API.
Configure SMTP with `VERIFEYE_SMTP_HOST`, `VERIFEYE_SMTP_PORT`,
`VERIFEYE_SMTP_USERNAME`, `VERIFEYE_SMTP_PASSWORD`, `VERIFEYE_SMTP_SENDER`, and
`VERIFEYE_SMTP_TLS_MODE` (`starttls`, `ssl`, or `none`). Configure SMS with
`VERIFEYE_TWILIO_ACCOUNT_SID`, `VERIFEYE_TWILIO_AUTH_TOKEN`, and
`VERIFEYE_TWILIO_FROM_NUMBER`. Set `VERIFEYE_PUBLIC_BASE_URL` to the URL recipients
can use to reach this VerifEye instance from notification links.
