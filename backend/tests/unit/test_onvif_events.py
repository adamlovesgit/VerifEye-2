"""ONVIF notification normalization and durable ingestion."""

from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.events import EventRepository
from verifeye.onvif_events import OnvifEventWorker, normalize_motion_notification


def notification(value="true"):
    return {
        "Topic": {"_value_1": "tns1:RuleEngine/CellMotionDetector/Motion"},
        "Message": {"Message": {
            "UtcTime": "2026-08-06T10:20:30Z",
            "Source": {"SimpleItem": [{"Name": "VideoSourceConfigurationToken", "Value": "source-1"}]},
            "Data": {"SimpleItem": [{"Name": "IsMotion", "Value": value}]},
        }},
    }


class PullPoint:
    def __init__(self): self.calls = 0; self.stop = None
    def PullMessages(self, request):
        self.calls += 1
        if self.stop: self.stop.set()
        return {"NotificationMessage": [notification()]}


class Gateway:
    def __init__(self): self.pullpoint_service = PullPoint(); self.credentials = None
    def pullpoint(self, endpoint, username, password):
        self.credentials = (endpoint, username, password)
        return self.pullpoint_service


class OnvifEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            (Path(__file__).resolve().parents[2] / "src/verifeye/storage/schema.sql").read_text()
        )
        connection.execute(
            "INSERT INTO cameras(id,name,encrypted_url,url_fingerprint,sanitized_host,source_type,enabled) "
            "VALUES(1,'Door',X'01','fp','camera.local','onvif',1)"
        )
        connection.commit(); connection.close()
        self.repository = EventRepository(self.database)

    def tearDown(self): self.temp.cleanup()

    def test_active_motion_is_normalized_and_clear_is_ignored(self):
        occurred, metadata, source_id = normalize_motion_notification(notification())
        self.assertEqual(occurred.isoformat(), "2026-08-06T10:20:30+00:00")
        self.assertEqual(metadata["items"]["IsMotion"], "true")
        self.assertEqual(len(source_id), 64)
        self.assertIsNone(normalize_motion_notification(notification("false")))

    def test_raw_xml_message_with_missing_topic_is_normalized(self):
        element = ElementTree.fromstring(
            '<Message UtcTime="2026-08-06T10:20:30Z" PropertyOperation="Changed">'
            '<Data><SimpleItem Name="IsMotion" Value="true"/></Data></Message>'
        )
        value = SimpleNamespace(
            Topic=SimpleNamespace(_value_1=None),
            Message=SimpleNamespace(_value_1=element),
        )
        occurred, metadata, _source_id = normalize_motion_notification(value)
        self.assertEqual(occurred.isoformat(), "2026-08-06T10:20:30+00:00")
        self.assertEqual(metadata["topic"], "onvif_motion")
        self.assertEqual(metadata["items"]["IsMotion"], "true")
        self.assertIn("SimpleItem", metadata["message_xml"])

    def test_worker_stores_notification_and_dispatches_recognition(self):
        camera = SimpleNamespace(id=1, onvif_endpoint="http://camera/onvif/device_service",
                                 onvif_username="admin", onvif_password="secret")
        gateway = Gateway()
        worker = OnvifEventWorker(camera, self.repository, gateway)
        gateway.pullpoint_service.stop = worker._stop
        worker._run()
        self.assertEqual(gateway.credentials, (camera.onvif_endpoint, "admin", "secret"))
        with self.repository.connect() as connection:
            event = connection.execute("SELECT event_type, metadata_json FROM camera_events").fetchone()
            self.assertEqual(event["event_type"], "onvif_motion")
            self.assertIn("CellMotionDetector", event["metadata_json"])
            dispatch = connection.execute(
                "SELECT state, attempts FROM event_dispatch WHERE event_id = (SELECT id FROM camera_events)"
            ).fetchone()
            self.assertEqual(dict(dispatch), {"state": "pending", "attempts": 0})

    def test_active_edge_and_cooldown_suppress_motion_bursts(self):
        calls = []
        repository = SimpleNamespace(accept_event=lambda *args: calls.append(args))
        camera = SimpleNamespace(id=1)
        clock_values = iter([0.0, 5.0, 25.0])
        worker = OnvifEventWorker(
            camera, repository, Gateway(), cooldown_seconds=20,
            clock=lambda: next(clock_values),
        )
        worker._handle_notification(notification("true"))
        worker._handle_notification(notification("true"))
        worker._handle_notification(notification("false"))
        worker._handle_notification(notification("true"))
        worker._handle_notification(notification("false"))
        worker._handle_notification(notification("true"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__": unittest.main()
