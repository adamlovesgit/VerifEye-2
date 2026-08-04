"""ONVIF profile lookup credential handling."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi import HTTPException
from verifeye.app import OnvifCredentials, OnvifImport, app, onvif_import, onvif_profiles


class FakeOnvifGateway:
    def __init__(self):
        self.calls = []

    def profiles(self, endpoint, username, password):
        self.calls.append((endpoint, username, password))
        if (username, password) != ("admin", "correct-password"):
            raise RuntimeError("authentication rejected")
        return [
            {"token": "main", "name": "Main stream", "uri": "rtsp://camera/main"},
            {"token": "sub", "name": "Sub stream", "uri": "rtsp://camera/sub"},
        ]


class OnvifProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous = getattr(app.state, "onvif", None)
        self.gateway = FakeOnvifGateway()
        app.state.onvif = self.gateway

    def tearDown(self):
        if self.previous is None:
            del app.state.onvif
        else:
            app.state.onvif = self.previous

    def test_correct_credentials_are_forwarded_and_profiles_returned(self):
        payload = OnvifCredentials(
            endpoint="http://192.168.1.20/onvif/device_service",
            username="admin",
            password="correct-password",
        )

        result = onvif_profiles(payload, _user=object())

        self.assertEqual(result, [{"token": "main", "name": "Main stream"}, {"token": "sub", "name": "Sub stream"}])
        self.assertEqual(self.gateway.calls, [(payload.endpoint, payload.username, payload.password)])

    def test_import_uses_substream_for_preview_and_main_for_recognition(self):
        previous_cameras = getattr(app.state, "cameras", None)
        captured = []
        app.state.cameras = SimpleNamespace(create=lambda *args: captured.append(args) or (SimpleNamespace(
            id=1, name=args[0], sanitized_host="camera", source_type="onvif", enabled=True, recognition_url=args[4]
        ), SimpleNamespace(
            running=True, connection_state=SimpleNamespace(value="live"), last_frame_at=None, last_error=None,
            retry_attempt=0, next_retry_at=None, recognition_session_state=SimpleNamespace(value="idle"),
            recognition_stream_mode=SimpleNamespace(value="none"), recognition_session_started_at=None,
            recognition_deadline=None, recognition_maximum_deadline=None, recognition_error=None,
        )))
        try:
            onvif_import(OnvifImport(
                endpoint="http://camera/onvif/device_service", username="admin", password="correct-password",
                previewToken="sub", recognitionToken="main", name="Door",
            ), _user=object())
        finally:
            if previous_cameras is None: del app.state.cameras
            else: app.state.cameras = previous_cameras
        self.assertIn("/sub", captured[0][1])
        self.assertIn("/main", captured[0][4])

    def test_incorrect_credentials_return_gateway_error(self):
        payload = OnvifCredentials(
            endpoint="http://192.168.1.20/onvif/device_service",
            username="admin",
            password="wrong-password",
        )

        with self.assertRaises(HTTPException) as raised:
            onvif_profiles(payload, _user=object())

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "ONVIF authentication or profile lookup failed.")
        self.assertEqual(self.gateway.calls, [(payload.endpoint, payload.username, payload.password)])


if __name__ == "__main__":
    unittest.main()
