"""MediaMTX 1.19.3 process supervision and media endpoint adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile


logger = logging.getLogger(__name__)
MEDIAMTX_VERSION = "1.19.3"
ARCHIVES = {
    ("Windows", "AMD64"): (
        "mediamtx_v1.19.3_windows_amd64.zip",
        "5d82148d1032a6a190d9909a2997d9989457aaadf49af87dd02cd4512d31bebe",
        "mediamtx.exe",
    ),
    ("Linux", "x86_64"): (
        "mediamtx_v1.19.3_linux_amd64.tar.gz",
        "a7ba21268fccda3ebc43fdad76b87fddb85ce77e725b5cb637bca724b5394fbe",
        "mediamtx",
    ),
}


class MediaError(RuntimeError):
    pass


def _windows_kill_on_close_job(process):
    """Put a Windows child in a job that dies with the owning API process."""
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return job


def _close_windows_job(process):
    job = getattr(process, "_verifeye_job", None)
    if os.name == "nt" and job:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(job)
        process._verifeye_job = None


class MediaMTXClient:
    """Small adapter pinned to MediaMTX 1.19.3's v3 Control API."""

    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def _request(self, method: str, path: str, payload=None, allow_404=False):
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")[:300]
            raise MediaError(f"MediaMTX {method} {path} failed ({exc.code}): {detail}") from exc
        except (URLError, OSError) as exc:
            raise MediaError(f"MediaMTX is unavailable at {self.base_url}: {exc}") from exc

    def info(self):
        return self._request("GET", "/v3/info")

    def list_paths(self):
        return self._request("GET", "/v3/config/paths/list?itemsPerPage=1000").get("items", [])

    def get_path(self, name: str):
        return self._request("GET", f"/v3/config/paths/get/{quote(name, safe='')}", allow_404=True)

    def add_path(self, name: str, configuration: dict):
        return self._request("POST", f"/v3/config/paths/add/{quote(name, safe='')}", configuration)

    def patch_path(self, name: str, configuration: dict):
        return self._request("PATCH", f"/v3/config/paths/patch/{quote(name, safe='')}", configuration)

    def delete_path(self, name: str):
        return self._request("DELETE", f"/v3/config/paths/delete/{quote(name, safe='')}", allow_404=True)

    def runtime_path(self, name: str):
        return self._request("GET", f"/v3/paths/get/{quote(name, safe='')}", allow_404=True)


class MediaMTXProcess:
    """Own the bundled child process; a post-start crash degrades and retries."""

    def __init__(self, vendor_dir: Path, runtime_dir: Path, client: MediaMTXClient,
                 rtsp_url="rtsp://127.0.0.1:8554", whep_url="http://127.0.0.1:8889",
                 auth_url="http://127.0.0.1:8000/internal/media-auth",
                 allowed_origins=("http://127.0.0.1:8000", "http://localhost:8000"),
                 webrtc_udp_address="127.0.0.1:8189", webrtc_additional_host="127.0.0.1", clock=time.monotonic,
                 restart_delay=lambda attempt: min(30.0, 2 ** min(attempt, 5))):
        self.vendor_dir, self.runtime_dir, self.client = Path(vendor_dir), Path(runtime_dir), client
        self.auth_url, self.clock = auth_url, clock
        self.allowed_origins = tuple(origin.rstrip("/") for origin in allowed_origins)
        self.webrtc_udp_address = webrtc_udp_address
        self.webrtc_additional_host = webrtc_additional_host
        self.restart_delay = restart_delay
        self.api_address = self._loopback_address(client.base_url, {"http", "https"})
        self.rtsp_address = self._loopback_address(rtsp_url, {"rtsp", "rtsps"})
        self.whep_address = self._loopback_address(whep_url, {"http", "https"})
        self._process = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor = None
        self._healthy = False
        self._generation = 0
        self.on_ready = None

    @staticmethod
    def _loopback_address(url, schemes):
        parsed = urlsplit(url)
        if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.port:
            raise MediaError(f"MediaMTX listener must be an explicit loopback URL with a port: {url}")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return f"{host}:{parsed.port}"

    @property
    def healthy(self):
        with self._lock:
            return self._healthy

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def _archive(self):
        key = (platform.system(), platform.machine())
        if key not in ARCHIVES:
            raise MediaError(f"MediaMTX {MEDIAMTX_VERSION} is not bundled for {key[0]} {key[1]}.")
        return ARCHIVES[key]

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _executable(self):
        archive_name, expected, member = self._archive()
        archive = self.vendor_dir / archive_name
        if not archive.is_file() or self._sha256(archive) != expected:
            raise MediaError(f"Bundled MediaMTX archive is missing or has the wrong SHA-256: {archive}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        executable = self.runtime_dir / member
        if not executable.is_file():
            if archive.suffix == ".zip":
                with zipfile.ZipFile(archive) as bundle:
                    with bundle.open(member) as source, executable.open("wb") as target:
                        shutil.copyfileobj(source, target)
            else:
                with tarfile.open(archive, "r:gz") as bundle:
                    entry = bundle.getmember(member)
                    if not entry.isfile() or Path(entry.name).name != member:
                        raise MediaError("MediaMTX archive contains an invalid executable entry.")
                    source = bundle.extractfile(entry)
                    if source is None:
                        raise MediaError("MediaMTX executable could not be extracted.")
                    with source, executable.open("wb") as target:
                        shutil.copyfileobj(source, target)
            if os.name != "nt":
                executable.chmod(0o755)
        return executable

    def _config(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        path = self.runtime_dir / "mediamtx.yml"
        path.write_text(
            "\n".join([
                "logLevel: warn", "logDestinations: [stdout]", "logStructured: false",
                "authMethod: http", f'authHTTPAddress: "{self.auth_url}"',
                "authHTTPExclude:", "  - action: api", "api: true", f'apiAddress: "{self.api_address}"',
                "apiEncryption: false", "apiAllowOrigins: []", "metrics: false", "pprof: false",
                "playback: false", "rtsp: true", f'rtspAddress: "{self.rtsp_address}"',
                'rtspEncryption: "no"', "rtspTransports: [tcp]", "rtmp: false", "hls: false",
                "webrtc: true", f'webrtcAddress: "{self.whep_address}"', "webrtcEncryption: false",
                f"webrtcAllowOrigins: {json.dumps(self.allowed_origins)}",
                f"webrtcLocalUDPAddress: {json.dumps(self.webrtc_udp_address)}", 'webrtcLocalTCPAddress: ""',
                "webrtcIPsFromInterfaces: false",
                f"webrtcAdditionalHosts: [{json.dumps(self.webrtc_additional_host)}]",
                "srt: false", "moq: false", "pathDefaults:", "  source: publisher",
                "  sourceOnDemand: true", "  sourceOnDemandStartTimeout: 10s",
                "  sourceOnDemandCloseAfter: 1s", "  record: false", "paths: {}", "",
            ]), encoding="utf-8"
        )
        return path

    def _launch(self):
        parsed_api = urlsplit(self.client.base_url)
        try:
            with socket.create_connection((parsed_api.hostname, parsed_api.port), timeout=.25):
                raise MediaError(
                    f"MediaMTX Control API port {parsed_api.hostname}:{parsed_api.port} "
                    "is already owned by another or orphaned process."
                )
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            # WSAEACCES means the port check itself was denied, not that it is free.
            if isinstance(exc, PermissionError):
                raise MediaError(f"Cannot verify MediaMTX Control API port ownership: {exc}") from exc
        executable, config = self._executable(), self._config()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(executable), str(config)], cwd=str(self.runtime_dir), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, creationflags=creationflags,
        )
        drain = threading.Thread(target=self._drain, args=(process,), daemon=True, name="mediamtx-log")
        process._verifeye_drain = drain
        drain.start()
        try:
            # CREATE_NO_WINDOW only hides the console; the job object is what
            # prevents MediaMTX surviving an abrupt API-worker exit on Windows.
            process._verifeye_job = _windows_kill_on_close_job(process)
        except OSError as exc:
            self._terminate(process)
            raise MediaError(f"Could not bind MediaMTX lifetime to VerifEye: {exc}") from exc
        if self._stop.wait(.15) or process.poll() is not None:
            self._terminate(process)
            raise MediaError("The launched MediaMTX process exited before health verification.")
        deadline = self.clock() + 10
        last = None
        while self.clock() < deadline and process.poll() is None:
            try:
                info = self.client.info()
                if info.get("version") != f"v{MEDIAMTX_VERSION}":
                    raise MediaError(f"Expected MediaMTX v{MEDIAMTX_VERSION}, got {info.get('version')}.")
                if process.poll() is not None:
                    raise MediaError("The launched MediaMTX process exited during health verification.")
                if self._stop.wait(.1) or process.poll() is not None:
                    raise MediaError("The launched MediaMTX process did not remain stable after health verification.")
                with self._lock:
                    self._process, self._healthy = process, True
                    self._generation += 1
                return
            except MediaError as exc:
                last = exc
                self._stop.wait(.1)
        self._terminate(process)
        raise MediaError(f"MediaMTX did not become healthy: {last or 'process exited'}")

    @staticmethod
    def _drain(process):
        if process.stdout:
            with process.stdout:
                for line in process.stdout:
                    logger.info("MediaMTX: %s", line.rstrip())

    @staticmethod
    def _terminate(process):
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(2)
            drain = getattr(process, "_verifeye_drain", None)
            if drain and drain is not threading.current_thread():
                drain.join(2)
        finally:
            _close_windows_job(process)

    def start(self):
        self._launch()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True, name="mediamtx-supervisor")
        self._monitor.start()

    def _monitor_loop(self):
        attempt, reconcile_pending = 0, False
        while not self._stop.wait(.5):
            with self._lock:
                process = self._process
            if process is not None and process.poll() is None:
                if reconcile_pending and self.on_ready:
                    try:
                        self.on_ready(); reconcile_pending = False
                    except Exception:
                        logger.exception("MediaMTX path reconciliation failed; retrying without stopping the API.")
                continue
            if process is not None:
                self._terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
                self._healthy = False
            delay = self.restart_delay(attempt); attempt += 1
            if self._stop.wait(delay):
                return
            try:
                self._launch(); attempt = 0
                callback = self.on_ready
                if callback:
                    try: callback()
                    except Exception:
                        reconcile_pending = True
                        logger.exception("MediaMTX recovered but path reconciliation failed; it will be retried.")
            except Exception:
                logger.exception("MediaMTX restart failed; VerifEye remains available in degraded mode.")

    def stop(self):
        self._stop.set()
        with self._lock:
            process, self._process, self._healthy = self._process, None, False
        if process:
            self._terminate(process)
        if self._monitor:
            self._monitor.join(5)


@dataclass(frozen=True)
class MediaEndpoint:
    path: str
    url: str


class MediaSource(Protocol):
    """Only boundary through which application code provisions or resolves media."""
    def provision(self, camera): ...
    def remove(self, camera_id): ...
    def preview_url(self, camera_id) -> MediaEndpoint: ...
    def preroll_url(self, camera_id) -> MediaEndpoint: ...
    def recognition_url(self, camera) -> MediaEndpoint | None: ...
    def media_ready(self, camera_id) -> bool: ...


class MediaMTXSource:
    """Provision camera sources and resolve all consumer endpoints."""

    PREFIX = "verifeye-camera-"

    def __init__(self, client: MediaMTXClient, process: MediaMTXProcess,
                 rtsp_base: str, whep_base: str):
        self.client, self.process = client, process
        self.rtsp_base, self.whep_base = rtsp_base.rstrip("/"), whep_base.rstrip("/")

    @classmethod
    def preview_path(cls, camera_id): return f"{cls.PREFIX}{camera_id}-preview"
    @classmethod
    def recognition_path(cls, camera_id): return f"{cls.PREFIX}{camera_id}-recognition"

    @staticmethod
    def _configuration(source):
        return {"source": source, "sourceOnDemand": True, "sourceOnDemandStartTimeout": "10s",
                "sourceOnDemandCloseAfter": "1s", "rtspTransport": "tcp", "record": False}

    def _upsert(self, name, configuration):
        current = self.client.get_path(name)
        if current is None:
            self.client.add_path(name, configuration)
            return
        changes = {key: value for key, value in configuration.items() if current.get(key) != value}
        if changes:
            self.client.patch_path(name, changes)

    def provision(self, camera):
        self._upsert(self.preview_path(camera.id), self._configuration(camera.url))
        if camera.recognition_url:
            self._upsert(self.recognition_path(camera.id), self._configuration(camera.recognition_url))
        else:
            self.client.delete_path(self.recognition_path(camera.id))

    def remove(self, camera_id):
        self.client.delete_path(self.recognition_path(camera_id))
        self.client.delete_path(self.preview_path(camera_id))

    def reconcile(self, cameras):
        enabled = {camera.id: camera for camera in cameras if camera.enabled}
        for camera in enabled.values():
            self.provision(camera)
        expected = {self.preview_path(i) for i in enabled}
        expected.update(self.recognition_path(i) for i, c in enabled.items() if c.recognition_url)
        for item in self.client.list_paths():
            name = item.get("name", "")
            if name.startswith(self.PREFIX) and name not in expected:
                self.client.delete_path(name)

    def preview_url(self, camera_id):
        path = self.preview_path(camera_id)
        return MediaEndpoint(path, f"{self.whep_base}/{path}/whep")

    def preroll_url(self, camera_id):
        path = self.preview_path(camera_id)
        return MediaEndpoint(path, f"{self.rtsp_base}/{path}")

    def recognition_url(self, camera):
        if not camera.recognition_url:
            return None
        path = self.recognition_path(camera.id)
        return MediaEndpoint(path, f"{self.rtsp_base}/{path}")

    def media_ready(self, camera_id):
        if not self.process.healthy:
            return False
        try:
            value = self.client.runtime_path(self.preview_path(camera_id))
            if not value:
                return False
            source_id = (value.get("source") or {}).get("id")
            return bool(value.get("ready") or value.get("available") or
                        (value.get("online") and source_id))
        except MediaError:
            return False
