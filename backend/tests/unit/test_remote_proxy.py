import unittest
from pathlib import Path
from urllib.parse import urljoin


class CaddyWhepRoutingTests(unittest.TestCase):
    def test_session_location_is_rewritten_under_the_public_whep_prefix(self):
        caddyfile = (Path(__file__).resolve().parents[3] / "deploy" / "Caddyfile.example").read_text()
        self.assertIn("header_down Location ^/(.*)$ /media/$1", caddyfile)

        endpoint = "https://view.example.com/media/verifeye-camera-7-preview/whep"
        # This models the reader's `new URL(Location, endpoint)`.  Without the
        # proxy rewrite, MediaMTX's root-relative Location escapes /media.
        upstream_location = "/verifeye-camera-7-preview/whep/012345"
        rewritten_location = "/media" + upstream_location
        self.assertEqual(urljoin(endpoint, rewritten_location),
                         "https://view.example.com/media/verifeye-camera-7-preview/whep/012345")
        self.assertEqual(urljoin(endpoint, upstream_location),
                         "https://view.example.com/verifeye-camera-7-preview/whep/012345")


if __name__ == "__main__":
    unittest.main()
