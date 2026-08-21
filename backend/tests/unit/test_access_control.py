"""Authorization defaults and role boundaries."""

import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.app import LoginRateLimiter, app, current_user, guest_camera_json  # noqa: E402
from verifeye.auth import User  # noqa: E402


class AccessControlTests(unittest.TestCase):
    def test_administrator_dependency_rejects_guest(self):
        guest = User(2, "guest@example.com", "Guest", "guest")
        with self.assertRaises(HTTPException) as raised:
            current_user(guest)
        self.assertEqual(raised.exception.status_code, 403)
        admin = User(1, "owner@example.com", "Owner", "admin")
        self.assertEqual(current_user(admin), admin)

    def test_all_non_allowlisted_api_routes_require_administrator(self):
        non_admin_paths = {
            "/api/auth/setup",
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/me",
            "/api/auth/logout",
            "/api/guest/cameras",
            "/api/guest/cameras/{camera_id}/preview-authorization",
            "/api/cameras/{camera_id}/events",
        }
        for route in app.routes:
            if not route.path.startswith("/api/") or route.path in non_admin_paths:
                continue
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            with self.subTest(path=route.path, methods=route.methods):
                self.assertIn(current_user, dependencies)

    def test_login_failures_are_rate_limited(self):
        limiter = LoginRateLimiter(maximum_attempts=2, window_seconds=60)
        limiter.failed("local")
        limiter.failed("local")
        with self.assertRaises(HTTPException) as raised:
            limiter.check("local")
        self.assertEqual(raised.exception.status_code, 429)
        limiter.succeeded("local")
        limiter.check("local")

    def test_guest_camera_shape_is_an_explicit_non_secret_allowlist(self):
        camera = SimpleNamespace(id=7, name="Front door", enabled=True)
        status = SimpleNamespace(
            running=True, connection_state=SimpleNamespace(value="live"),
            last_error="rtsp://secret:password@camera.local/private",
        )
        self.assertEqual(
            guest_camera_json(camera, status),
            {"id": 7, "name": "Front door", "connectionState": "live", "previewAvailable": True},
        )


if __name__ == "__main__":
    unittest.main()
