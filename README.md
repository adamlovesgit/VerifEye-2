VerifEye 2.0 Charter

VerifEye 2.0 is designed to be a self hosted security system with minimal cloud coupling. The reason for this ethos is for the consumer to own the full lifecycle of data, not the vendor. 

The architecture for this application is inintally designed to be local first modular monolith. This means: one backend application, one database, and one local web interface. 

The recognition pipeline is migrated from VerifEye 1.0, a school project where the core logic for this project was first designed and implemented. 

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

The browser interface supports local account creation, sign-in, persistent
sessions, image preview, drag-and-drop upload, and face enrollment into the
SQLite embedding database.

Install and start it from the project root:

```powershell
python -m pip install -r backend/requirements-web.txt
$env:PYTHONPATH = "backend/src"
python -m uvicorn verifeye.app:app --reload
```

Then open `http://127.0.0.1:8000`. The recognition model defaults to
`~/.insightface/models/buffalo_l/w600k_r50.onnx`. Set
`VERIFEYE_MODEL_PATH` before starting the server if it is stored elsewhere.

Uploaded enrollment photos stay under `backend/data/enrollments`; the image
and its normalized embedding are never sent to a cloud service.

## Live RTSP recognition

VerifEye can supervise up to four enabled RTSP cameras by default. Recognition
runs in the backend even when no browser is open. Every camera keeps one newest
decoded frame and one latest annotated JPEG, so slow inference or viewers do not
build a latency-producing queue. MJPEG feeds and all camera controls require an
authenticated VerifEye session.

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

Streams use FFmpeg/PyAV over RTSP/TCP. Actual codec compatibility depends on the
installed FFmpeg build. The recognition threshold defaults to a provisional
`0.40`; validate it against a representative labeled golden set before treating
it as a production decision. A golden-set NPZ must contain `embeddings` (`N x D`)
and matching `labels` (`N`); evaluate it with:

```powershell
python backend/tests/integration/evaluate_threshold.py golden-set.npz --threshold 0.40
```

The dependency direction is deliberately one-way:

`HTTP routes -> application services -> domain ports <- infrastructure adapters`

Routes do not decode video, invoke recognition models, query camera tables, or
own worker threads. The process-wide camera manager owns one worker per active
camera; workers own their RTSP sources and detectors; the process-wide
recognition engine exclusively owns ArcFace. Stop, delete, and shutdown close
sources, clear buffers, join workers, and only then release shared recognition
resources.

For an opt-in test against real hardware (no frames are saved):

```powershell
$env:VERIFEYE_TEST_RTSP_URL = "rtsp://user:password@camera.local/stream"
python backend/tests/integration/live_camera.py --seconds 10
```

Optional tuning variables are `VERIFEYE_RECOGNITION_FPS`,
`VERIFEYE_SIMILARITY_THRESHOLD`, `VERIFEYE_FRAME_FRESHNESS_SECONDS`,
`VERIFEYE_RTSP_TIMEOUT_SECONDS`, `VERIFEYE_CLEANUP_TIMEOUT_SECONDS`, and
`VERIFEYE_MAX_ACTIVE_CAMERAS`.


