"""Durable camera-event storage and shared-session behavior."""

from datetime import timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.events import (
    EventRepository, RecognitionPersistenceSink, ScreenshotStorage, iso, utcnow,
)


class EventRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            (Path(__file__).resolve().parents[2] / "src/verifeye/storage/schema.sql").read_text()
        )
        connection.execute(
            """INSERT INTO cameras(
                   id, name, encrypted_url, url_fingerprint, sanitized_host, enabled
               ) VALUES (1, 'Door', X'01', 'fingerprint', 'camera.local', 1)"""
        )
        connection.commit()
        connection.close()
        self.repository = EventRepository(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, source, occurred=None):
        return self.repository.accept_event(
            1, source, "motion", occurred or utcnow(), {"zone": "door"}
        )

    def test_acceptance_is_durable_and_duplicate_is_idempotent(self):
        first = self.accept("source-1")
        second = self.accept("source-1")
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.id, second.id)
        with self.repository.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM camera_events").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT state FROM event_dispatch").fetchone()[0], "pending"
            )

    def test_event_can_be_recorded_without_dispatching_recognition(self):
        event = self.repository.accept_event(
            1, "onvif-1", "onvif_motion", utcnow(), {"topic": "MotionAlarm"}, dispatch=False
        )
        self.assertTrue(event.created)
        with self.repository.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM camera_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_dispatch").fetchone()[0], 0)

    def test_overlapping_events_share_one_session_and_event_has_one_link(self):
        occurred = utcnow()
        first = self.accept("one", occurred)
        second = self.accept("two", occurred + timedelta(seconds=2))
        first_session, created = self.repository.attach_event(first.id, 5, 10, 60)
        second_session, second_created = self.repository.attach_event(second.id, 5, 10, 60)
        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(first_session, second_session)
        self.assertEqual(
            self.repository.attach_event(first.id, 5, 10, 60)[0], first_session
        )

    def test_terminal_session_state_propagates_without_rewriting_terminal_event(self):
        event = self.accept("terminal")
        session_id, _ = self.repository.attach_event(event.id, 5, 10, 60)
        self.repository.transition_session(session_id, "active")
        self.repository.transition_session(session_id, "completed")
        self.repository.transition_session(
            session_id, "failed", error_code="late", error_message="late failure"
        )
        detail = self.repository.get_event(event.id)
        self.assertEqual(detail["state"], "completed")
        self.assertEqual(detail["session"]["session_state"], "completed")

    def test_restart_interrupts_active_work_and_preserves_results(self):
        event = self.accept("restart")
        session_id, _ = self.repository.attach_event(event.id, 5, 10, 60)
        self.repository.transition_session(session_id, "active")
        self.repository.add_results(session_id, [{
            "capture_timestamp": iso(utcnow()), "outcome": "unrecognized_face",
            "displayed_label": "Unknown",
        }])
        self.repository.reconcile()
        detail = self.repository.get_event(event.id)
        self.assertEqual(detail["state"], "failed")
        self.assertEqual(detail["session"]["session_state"], "interrupted")
        self.assertEqual(len(detail["results"]), 1)
        self.assertEqual(detail["dispatch"]["state"], "failed")

    def test_dispatch_claim_has_owner_lease_and_can_be_renewed(self):
        event = self.accept("lease")
        claimed = self.repository.claim_dispatch("worker-a", 30)
        self.assertEqual(claimed["event_id"], event.id)
        self.assertEqual(claimed["attempts"], 1)
        self.assertTrue(self.repository.renew_dispatch(claimed["id"], "worker-a", 30))
        self.assertFalse(self.repository.renew_dispatch(claimed["id"], "worker-b", 30))
        self.repository.complete_dispatch(claimed["id"], "worker-a")
        self.assertIsNone(self.repository.claim_dispatch("worker-b", 30))

    def test_specific_manual_token_can_be_revoked_without_revoking_another(self):
        first_id, first = self.repository.issue_token(1)
        _second_id, second = self.repository.issue_token(1)
        self.assertTrue(self.repository.revoke_token(1, first_id))
        self.assertFalse(self.repository.authenticate_camera_token(1, first))
        self.assertTrue(self.repository.authenticate_camera_token(1, second))

    def test_result_screenshot_is_owned_by_result_and_visible_through_event(self):
        event = self.accept("crop")
        session_id, _ = self.repository.attach_event(event.id, 5, 10, 60)
        self.repository.add_results(session_id, [{
            "capture_timestamp": iso(utcnow()), "outcome": "recognized",
            "identity_id": None, "displayed_label": "Person",
            "screenshot": {
                "relative_path": "generated/crop.jpg", "media_type": "image/jpeg",
                "byte_size": 4, "sha256": "hash",
            },
        }])
        detail = self.repository.get_event(event.id)
        self.assertEqual(detail["screenshots"][0]["role"], "face_crop")
        self.assertIsNotNone(detail["screenshots"][0]["result_id"])

    def test_result_screenshot_persists_explicit_context_role(self):
        event = self.accept("context")
        session_id, _ = self.repository.attach_event(event.id, 5, 10, 60)
        self.repository.add_results(session_id, [{
            "capture_timestamp": iso(utcnow()), "outcome": "recognized",
            "identity_id": None, "displayed_label": "Person",
            "screenshot": {
                "role": "annotated_context", "relative_path": "generated/context.jpg",
                "media_type": "image/jpeg", "byte_size": 4, "sha256": "hash",
            },
        }])

        detail = self.repository.get_event(event.id)

        self.assertEqual(detail["screenshots"][0]["role"], "annotated_context")

    def test_persistence_sink_propagates_image_role_to_screenshot(self):
        event = self.accept("sink-context")
        session_id, _ = self.repository.attach_event(event.id, 5, 10, 60)
        storage = ScreenshotStorage(Path(self.temp.name) / "screenshots")
        sink = RecognitionPersistenceSink(self.repository, storage)

        sink.results(session_id, [{
            "capture_timestamp": iso(utcnow()), "outcome": "unrecognized_face",
            "displayed_label": "Unknown", "image_bytes": b"full-frame-jpeg",
            "image_role": "annotated_context",
        }])

        detail = self.repository.get_event(event.id)
        self.assertEqual(detail["screenshots"][0]["role"], "annotated_context")
        stored = self.repository.screenshot(detail["screenshots"][0]["id"])
        self.assertEqual(storage.resolve(stored["relative_path"]).read_bytes(), b"full-frame-jpeg")

    def test_motion_retention_keeps_recognized_and_prunes_by_outcome(self):
        def completed(source, outcome, age_days):
            event = self.repository.accept_event(
                1, source, "onvif_motion", utcnow(), {}, dispatch=False
            )
            session_id, _ = self.repository.attach_event(event.id, 0, 1, 1)
            self.repository.add_results(session_id, [{
                "capture_timestamp": iso(utcnow()), "outcome": outcome,
                "displayed_label": "test",
            }])
            self.repository.transition_session(session_id, "completed")
            with self.repository.connect() as connection:
                connection.execute(
                    "UPDATE camera_events SET accepted_at = ? WHERE id = ?",
                    (iso(utcnow() - timedelta(days=age_days)), event.id),
                )
            return event.id

        recognized = completed("recognized-old", "recognized", 100)
        unrecognized = completed("unknown-old", "unrecognized_face", 31)
        no_face = completed("no-face-old", "no_face", 8)
        recent = completed("no-face-recent", "no_face", 1)

        self.assertEqual(self.repository.prune_motion_events(7, 30), 2)
        self.assertIsNotNone(self.repository.get_event(recognized))
        self.assertIsNone(self.repository.get_event(unrecognized))
        self.assertIsNone(self.repository.get_event(no_face))
        self.assertIsNotNone(self.repository.get_event(recent))


if __name__ == "__main__":
    unittest.main()
