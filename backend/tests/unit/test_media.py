"""MediaMTX 1.19.3 endpoint, path, and delegated-auth contracts."""

from pathlib import Path
from dataclasses import replace
import os
import sqlite3
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.app import MediaAuthRequest, app, media_auth
from verifeye.auth import AuthStore
from verifeye.cameras.media import (
    MediaError, MediaMTXClient, MediaMTXProcess, MediaMTXSource,
    _close_windows_job, _windows_kill_on_close_job,
)
from verifeye.cameras.models import Camera, CameraNotFound
from verifeye.storage import EmbeddingStore


class RecordingClient(MediaMTXClient):
    def __init__(self): self.requests = []
    def _request(self, method, path, payload=None, allow_404=False):
        self.requests.append((method, path, payload, allow_404)); return {}


class ControlApiContractTests(unittest.TestCase):
    def test_adapter_uses_v1193_control_api_routes(self):
        client = RecordingClient()
        client.info(); client.list_paths(); client.get_path("preview")
        client.add_path("preview", {"source":"rtsp://camera"})
        client.patch_path("preview", {"record":False}); client.delete_path("preview")
        client.runtime_path("preview")
        self.assertEqual([item[:2] for item in client.requests], [
            ("GET", "/v3/info"),
            ("GET", "/v3/config/paths/list?itemsPerPage=1000"),
            ("GET", "/v3/config/paths/get/preview"),
            ("POST", "/v3/config/paths/add/preview"),
            ("PATCH", "/v3/config/paths/patch/preview"),
            ("DELETE", "/v3/config/paths/delete/preview"),
            ("GET", "/v3/paths/get/preview"),
        ])


class MediaProcessFailureTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows job-object lifecycle")
    def test_windows_job_closure_terminates_the_child(self):
        child = subprocess.Popen([
            sys.executable, "-c", "import time; time.sleep(30)",
        ], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            child._verifeye_job = _windows_kill_on_close_job(child)
            _close_windows_job(child)
            child.wait(3)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill(); child.wait(2)

    def test_start_refuses_to_validate_an_existing_control_port(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as folder:
            listener.bind(("127.0.0.1", 0)); listener.listen()
            port = listener.getsockname()[1]
            process = MediaMTXProcess(
                Path(folder), Path(folder) / "runtime",
                MediaMTXClient(f"http://127.0.0.1:{port}"),
            )
            with self.assertRaisesRegex(MediaError, "already owned"):
                process._launch()

    def test_listener_configuration_rejects_non_loopback_bind(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(MediaError):
            MediaMTXProcess(Path(folder), Path(folder) / "runtime",
                            MediaMTXClient("http://0.0.0.0:9997"))

    def test_initial_failure_is_fatal_but_restart_failures_are_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            process = MediaMTXProcess(Path(folder), Path(folder) / "runtime",
                                      MediaMTXClient("http://127.0.0.1:9997"),
                                      restart_delay=lambda _attempt: 0)
            process._launch = lambda: (_ for _ in ()).throw(MediaError("initial failure"))
            with self.assertRaises(MediaError): process.start()

            class Exited:
                @staticmethod
                def poll(): return 1
            calls, ready = [], []
            process._process, process._healthy = Exited(), True
            def restart():
                calls.append(1)
                if len(calls) == 1: raise MediaError("restart failure")
                process._healthy = True
                process._stop.set()
            process._launch, process.on_ready = restart, lambda: ready.append(1)
            with self.assertLogs("verifeye.cameras.media", level="ERROR"):
                process._monitor_loop()
            self.assertEqual(len(calls), 2)
            self.assertEqual(ready, [1])
            self.assertTrue(process.healthy)


class FakeControl:
    def __init__(self): self.paths = {}; self.deleted = []
    def get_path(self, name): return self.paths.get(name)
    def add_path(self, name, value): self.paths[name] = dict(value)
    def patch_path(self, name, value): self.paths[name].update(value)
    def delete_path(self, name): self.deleted.append(name); self.paths.pop(name, None)
    def list_paths(self): return [dict(value, name=name) for name, value in self.paths.items()]
    def runtime_path(self, _name): return {"online":True}


class MediaSourceTests(unittest.TestCase):
    def test_preview_and_preroll_are_two_protocols_over_exactly_one_path(self):
        control = FakeControl(); process = SimpleNamespace(healthy=True)
        source = MediaMTXSource(control, process, "rtsp://127.0.0.1:8554", "http://127.0.0.1:8889")
        camera = Camera(7, "Door", "rtsp://camera/light", "camera", "manual", True)
        source.provision(camera)
        preview, preroll = source.preview_url(7), source.preroll_url(7)
        self.assertEqual(preview.path, preroll.path)
        self.assertEqual(set(control.paths), {"verifeye-camera-7-preview"})
        self.assertTrue(preview.url.endswith("/verifeye-camera-7-preview/whep"))
        self.assertTrue(preroll.url.endswith("/verifeye-camera-7-preview"))

    def test_distinct_recognition_stream_gets_only_one_optional_path(self):
        control = FakeControl(); source = MediaMTXSource(control, SimpleNamespace(healthy=True), "rtsp://router", "http://router")
        camera = Camera(2, "Door", "rtsp://camera/light", "camera", "manual", True,
                        recognition_url="rtsp://camera/main")
        source.provision(camera)
        self.assertEqual(set(control.paths), {
            "verifeye-camera-2-preview", "verifeye-camera-2-recognition"
        })
        self.assertEqual(control.paths["verifeye-camera-2-preview"]["rtspTransport"], "tcp")
        self.assertFalse(control.paths["verifeye-camera-2-preview"]["record"])

    def test_configured_but_unconnected_on_demand_source_is_not_ready(self):
        control = FakeControl(); process = SimpleNamespace(healthy=True)
        source = MediaMTXSource(control, process, "rtsp://127.0.0.1:8554", "http://127.0.0.1:8889")
        control.runtime_path = lambda _name: {
            "online": True, "ready": False, "available": False,
            "source": {"type": "rtspSource", "id": ""},
        }
        self.assertFalse(source.media_ready(1))


class DelegatedAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); database = Path(self.temp.name) / "auth.db"
        with EmbeddingStore(database) as store:
            auth = AuthStore(store._connection)
            user = auth.create_user("user@example.com", "User", "password123")
            self.token = auth.create_session(user.id)
        self.camera = Camera(1, "Door", "rtsp://camera/light", "camera", "manual", True,
                             recognition_url="rtsp://camera/main")
        def get_camera(camera_id):
            if camera_id != 1: raise CameraNotFound("missing")
            return self.camera
        self.previous_settings = getattr(app.state, "settings", None)
        self.previous_cameras = getattr(app.state, "cameras", None)
        app.state.settings = SimpleNamespace(database=database)
        app.state.cameras = SimpleNamespace(repository=SimpleNamespace(get=get_camera))
    def tearDown(self):
        if self.previous_settings is None: del app.state.settings
        else: app.state.settings = self.previous_settings
        if self.previous_cameras is None: del app.state.cameras
        else: app.state.cameras = self.previous_cameras
        self.temp.cleanup()
    def test_existing_bearer_is_delegated_to_preview_read(self):
        response = media_auth(MediaAuthRequest(action="read", path="verifeye-camera-1-preview",
                                               protocol="webrtc", token=self.token, ip="127.0.0.1"))
        self.assertEqual(response.status_code, 204)
    def test_loopback_rtsp_is_internal_and_recognition_webrtc_is_rejected(self):
        response = media_auth(MediaAuthRequest(action="read", path="verifeye-camera-1-recognition",
                                               protocol="rtsp", ip="127.0.0.1"))
        self.assertEqual(response.status_code, 204)
        with self.assertRaises(HTTPException):
            media_auth(MediaAuthRequest(action="read", path="verifeye-camera-1-recognition",
                                        protocol="webrtc", token=self.token, ip="127.0.0.1"))
    def test_invalid_expired_wrong_camera_disabled_and_unauthorized_requests_fail(self):
        denied = [
            MediaAuthRequest(action="publish", path="verifeye-camera-1-preview", protocol="webrtc", token=self.token),
            MediaAuthRequest(action="read", path="verifeye-camera-2-preview", protocol="webrtc", token=self.token),
            MediaAuthRequest(action="read", path="verifeye-camera-1-preview", protocol="webrtc", token="invalid"),
        ]
        for request in denied:
            with self.subTest(request=request), self.assertRaises(HTTPException) as raised:
                media_auth(request)
            self.assertEqual(raised.exception.status_code, 401)
        self.camera = replace(self.camera, enabled=False)
        with self.assertRaises(HTTPException):
            media_auth(MediaAuthRequest(action="read", path="verifeye-camera-1-preview",
                                        protocol="webrtc", token=self.token))
        self.camera = replace(self.camera, enabled=True)
        with EmbeddingStore(app.state.settings.database) as store:
            AuthStore(store._connection).delete_session(self.token)
        with self.assertRaises(HTTPException):
            media_auth(MediaAuthRequest(action="read", path="verifeye-camera-1-preview",
                                        protocol="webrtc", token=self.token))


if __name__ == "__main__": unittest.main()
