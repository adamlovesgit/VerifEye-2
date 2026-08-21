"""Opt-in contract test against the bundled MediaMTX 1.19.3 executable."""

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.cameras.media import MEDIAMTX_VERSION, MediaMTXClient, MediaMTXProcess


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(
    os.getenv("VERIFEYE_RUN_MEDIAMTX_CONTRACT") == "1",
    "set VERIFEYE_RUN_MEDIAMTX_CONTRACT=1 to run the bundled-process contract",
)
class MediaMTX1193Contract(unittest.TestCase):
    def test_verified_control_routes_and_pathconf_schema(self):
        api_port, rtsp_port, whep_port = free_port(), free_port(), free_port()
        client = MediaMTXClient(f"http://127.0.0.1:{api_port}")
        project = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as runtime:
            process = MediaMTXProcess(
                project / "backend" / "vendor" / "mediamtx" / MEDIAMTX_VERSION,
                Path(runtime), client,
                f"rtsp://127.0.0.1:{rtsp_port}", f"http://127.0.0.1:{whep_port}",
            )
            try:
                process.start()
                self.assertEqual(client.info()["version"], f"v{MEDIAMTX_VERSION}")
                name = "verifeye-camera-991-preview"
                configuration = {
                    "source": "rtsp://127.0.0.1:19991/lightweight",
                    "sourceOnDemand": True,
                    "sourceOnDemandStartTimeout": "10s",
                    "sourceOnDemandCloseAfter": "1s",
                    "rtspTransport": "tcp",
                    "record": False,
                }
                client.add_path(name, configuration)
                created = client.get_path(name)
                for key, value in configuration.items():
                    self.assertEqual(created[key], value)
                runtime_path = client.runtime_path(name)
                self.assertEqual(runtime_path["name"], name)
                self.assertFalse(runtime_path["ready"])
                self.assertFalse(runtime_path["available"])
                self.assertEqual(runtime_path["readers"], [])
                client.patch_path(name, {"sourceOnDemandCloseAfter": "2s"})
                self.assertEqual(client.get_path(name)["sourceOnDemandCloseAfter"], "2s")
                self.assertIn(name, {item["name"] for item in client.list_paths()})
                client.delete_path(name)
                self.assertIsNone(client.get_path(name))
            finally:
                process.stop()


if __name__ == "__main__":
    unittest.main()
