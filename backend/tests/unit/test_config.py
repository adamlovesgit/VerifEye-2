import os
import unittest
from unittest.mock import patch

from verifeye.config import Settings


class RemoteViewSettingsTests(unittest.TestCase):
    def test_remote_public_settings_accept_one_public_host(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "https://view.example.test",
            "VERIFEYE_PUBLIC_WHEP_URL": "https://view.example.test/media",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "view.example.test",
            "VERIFEYE_MEDIAMTX_WEBRTC_UDP_ADDRESS": "0.0.0.0:8189",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_environment()
            settings.validate()
        self.assertEqual(settings.public_whep_url, "https://view.example.test/media")

    def test_remote_whep_host_must_match_the_public_site(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "https://view.example.test",
            "VERIFEYE_PUBLIC_WHEP_URL": "https://other.example.test/media",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "view.example.test",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(ValueError, "same public host"):
                Settings.from_environment().validate()

    def test_advertised_webrtc_host_must_exactly_match_public_site(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "https://view.example.test",
            "VERIFEYE_PUBLIC_WHEP_URL": "https://view.example.test/media",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "public-ip.example.test",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(ValueError, "exactly match"):
                Settings.from_environment().validate()

    def test_local_development_allows_both_standard_loopback_origins(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "http://127.0.0.1:8000",
            "VERIFEYE_PUBLIC_WHEP_URL": "http://127.0.0.1:8889",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "127.0.0.1",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_environment()
            settings.validate()
        self.assertEqual(settings.webrtc_allowed_origins,
                         ("http://127.0.0.1:8000", "http://localhost:8000"))

    def test_remote_deployment_allows_only_its_configured_origin(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "https://view.example.test",
            "VERIFEYE_PUBLIC_WHEP_URL": "https://view.example.test/media",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "view.example.test",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_environment()
            settings.validate()
        self.assertEqual(settings.webrtc_allowed_origins, ("https://view.example.test",))

    def test_ipv6_loopback_includes_its_exact_bracketed_origin(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "http://[::1]:8000",
            "VERIFEYE_PUBLIC_WHEP_URL": "http://[::1]:8889",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "::1",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_environment()
            settings.validate()
        self.assertIn("http://[::1]:8000", settings.webrtc_allowed_origins)

    def test_public_base_path_is_rejected_until_proxy_routing_supports_it(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "https://view.example.test/verifeye",
            "VERIFEYE_PUBLIC_WHEP_URL": "https://view.example.test/media",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "view.example.test",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(ValueError, "without credentials, path"):
                Settings.from_environment().validate()

    def test_remote_http_urls_are_rejected_but_loopback_http_is_allowed(self):
        environment = {
            "VERIFEYE_PUBLIC_BASE_URL": "http://view.example.test",
            "VERIFEYE_PUBLIC_WHEP_URL": "http://view.example.test/media",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "view.example.test",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(ValueError, "PUBLIC_BASE_URL must use HTTPS"):
                Settings.from_environment().validate()

        loopback = {
            "VERIFEYE_PUBLIC_BASE_URL": "http://localhost:8000",
            "VERIFEYE_PUBLIC_WHEP_URL": "http://localhost:8889",
            "VERIFEYE_MEDIAMTX_WEBRTC_ADDITIONAL_HOST": "localhost",
        }
        with patch.dict(os.environ, loopback, clear=False):
            Settings.from_environment().validate()


if __name__ == "__main__":
    unittest.main()
