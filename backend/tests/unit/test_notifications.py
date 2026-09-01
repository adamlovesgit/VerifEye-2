import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError
from verifeye.app import NotificationRulePayload
from verifeye.events import EventRepository, utcnow
from unittest.mock import patch

from verifeye.notifications import (
    NotificationError, NotificationProviderStore, NotificationRepository,
    NotificationWorker, PermanentDeliveryError, ProviderSettings,
)


SCHEMA = (Path(__file__).resolve().parents[2] / "src/verifeye/storage/schema.sql").read_text()


class NotificationRulePayloadTests(unittest.TestCase):
    def test_rule_type_is_required_and_removed_fields_are_rejected(self):
        payload = {"identityId":1,"emailAddress":"","phoneNumber":"",
                   "emailEnabled":False,"smsEnabled":False,"cameraIds":[]}
        with self.assertRaises(ValidationError):
            NotificationRulePayload.model_validate(payload)
        with self.assertRaises(ValidationError):
            NotificationRulePayload.model_validate({
                **payload, "ruleType":"identity", "isFallback":False,
                "outcomes":["recognized"],
            })


class NotificationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "test.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(SCHEMA)
        with connection:
            connection.execute("INSERT INTO cameras(name,encrypted_url,url_fingerprint,sanitized_host) VALUES('Door',X'01','one','door')")
            connection.execute("INSERT INTO identities(external_id,display_name) VALUES('person-1','Ada')")
        connection.close()
        self.notifications = NotificationRepository(self.database)
        self.events = EventRepository(self.database)

    def tearDown(self): self.temp.cleanup()

    def save_identity_rule(self):
        return self.notifications.save_rule({"identityId":1,"ruleType":"identity","emailAddress":"ada@example.com",
            "phoneNumber":"+15551234567","emailEnabled":True,"smsEnabled":True,
            "cameraIds":[]})

    def test_smtp_overrides_are_encrypted_persistent_and_keep_blank_password(self):
        class Cipher:
            def encrypt(self, value): return value[::-1].encode()
            def decrypt(self, value): return value.decode()[::-1]

        store = NotificationProviderStore(self.database, Cipher())
        current = ProviderSettings(smtp_password="environment-secret")
        store.save_smtp({"host":"smtp.example.com","port":465,"username":"ada",
            "password":"saved-secret","sender":"alerts@example.com","tlsMode":"ssl",
            "clearPassword":False}, current)
        connection = sqlite3.connect(self.database)
        encrypted = connection.execute(
            "SELECT smtp_password_encrypted FROM notification_provider_settings WHERE id=1"
        ).fetchone()[0]
        connection.close()
        self.assertNotIn(b"saved-secret", encrypted)
        loaded = store.load(ProviderSettings())
        self.assertEqual(loaded.smtp_host, "smtp.example.com")
        self.assertEqual(loaded.smtp_password, "saved-secret")
        store.save_smtp({"host":"mail.example.com","port":587,"username":"ada",
            "password":None,"sender":"alerts@example.com","tlsMode":"starttls",
            "clearPassword":False}, loaded)
        self.assertEqual(store.load(ProviderSettings()).smtp_password, "saved-secret")

    def test_rule_crud_validation_and_optimistic_version(self):
        rule_id = self.save_identity_rule()
        rule = self.notifications.settings()["rules"][0]
        self.assertEqual(rule["identityName"], "Ada")
        self.assertNotIn("isFallback", rule)
        self.assertNotIn("outcomes", rule)
        rule.update(emailAddress="new@example.com")
        self.notifications.save_rule(rule, rule_id)
        with self.assertRaises(NotificationError): self.notifications.save_rule(rule, rule_id)
        self.assertTrue(self.notifications.delete_rule(rule_id))

    def test_rule_type_is_required_and_controls_identity_shape(self):
        base = {"identityId":1,"emailAddress":"","phoneNumber":"",
                "emailEnabled":False,"smsEnabled":False,"cameraIds":[]}
        with self.assertRaises(NotificationError):
            self.notifications.save_rule(base)
        with self.assertRaises(NotificationError):
            self.notifications.save_rule({**base, "ruleType":"identity", "identityId":None})
        with self.assertRaises(NotificationError):
            self.notifications.save_rule({**base, "ruleType":"no_face", "identityId":1})

    def test_session_rule_type_is_unique_and_phone_is_e164(self):
        payload={"identityId":None,"ruleType":"no_face","emailAddress":"","phoneNumber":"+15551234567",
                 "emailEnabled":False,"smsEnabled":True,"cameraIds":[]}
        self.notifications.save_rule(payload)
        with self.assertRaises(NotificationError): self.notifications.save_rule(payload)
        payload["phoneNumber"]="555"
        with self.assertRaises(NotificationError): self.notifications.save_rule(payload, 1)

    def test_planner_deduplicates_repeated_recognition_results(self):
        rule_id = self.save_identity_rule()
        event = self.events.accept_event(1,"motion-1","motion",utcnow(),{})
        session_id,_ = self.events.attach_event(event.id,0,10,60)
        self.events.add_results(session_id,[{"capture_timestamp":utcnow().isoformat(),"outcome":"recognized","identity_id":1,"displayed_label":"Ada"},
                                            {"capture_timestamp":utcnow().isoformat(),"outcome":"recognized","identity_id":1,"displayed_label":"Ada"}])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),2)
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),0)
        deliveries=self.notifications.deliveries()
        self.assertEqual({item["channel"] for item in deliveries},{"email","sms"})
        self.assertTrue(all(item["identityName"]=="Ada" for item in deliveries))

    def test_camera_filter_and_fallback_outcome(self):
        self.notifications.save_rule({"identityId":None,"ruleType":"unknown_face","emailAddress":"ops@example.com",
            "phoneNumber":"","emailEnabled":True,"smsEnabled":False,"cameraIds":[1]})
        event=self.events.accept_event(1,"motion-2","motion",utcnow(),{})
        session_id,_=self.events.attach_event(event.id,0,10,60)
        self.events.add_results(session_id,[{"capture_timestamp":utcnow().isoformat(),"outcome":"unrecognized_face"}])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),1)
        self.assertEqual(self.notifications.deliveries()[0]["outcome"],"unrecognized_face")

    def test_camera_filter_excludes_other_cameras(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """INSERT INTO cameras(name,encrypted_url,url_fingerprint,sanitized_host)
                   VALUES('Back',X'02','two','back')"""
            )
            connection.commit()
        finally:
            connection.close()
        self.notifications.save_rule({"identityId":None,"ruleType":"unknown_face",
            "emailAddress":"ops@example.com","phoneNumber":"","emailEnabled":True,
            "smsEnabled":False,"cameraIds":[1]})
        event=self.events.accept_event(2,"motion-other-camera","motion",utcnow(),{})
        session_id,_=self.events.attach_event(event.id,0,10,60)
        self.events.add_results(session_id,[{"capture_timestamp":utcnow().isoformat(),
                                             "outcome":"unrecognized_face"}])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),0)

    def test_unknown_face_is_detected_across_the_whole_session(self):
        self.notifications.save_rule({"identityId":None,"ruleType":"unknown_face",
            "emailAddress":"ops@example.com","phoneNumber":"","emailEnabled":True,"smsEnabled":False,
            "cameraIds":[]})
        occurred = utcnow()
        event = self.events.accept_event(1,"motion-session","motion",occurred,{})
        session_id,_ = self.events.attach_event(event.id,0,10,60)
        self.events.add_results(session_id,[{
            "capture_timestamp":(occurred+timedelta(seconds=30)).isoformat(),
            "outcome":"unrecognized_face",
        }])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),1)
        self.assertEqual(self.notifications.deliveries()[0]["outcome"],"unrecognized_face")

    def test_no_face_requires_the_entire_session_to_have_no_faces(self):
        self.notifications.save_rule({"identityId":None,"ruleType":"no_face",
            "emailAddress":"ops@example.com","phoneNumber":"","emailEnabled":True,"smsEnabled":False,
            "cameraIds":[]})
        event = self.events.accept_event(1,"motion-no-face","motion",utcnow(),{})
        session_id,_ = self.events.attach_event(event.id,0,10,60)
        self.events.add_results(session_id,[
            {"capture_timestamp":utcnow().isoformat(),"outcome":"no_face"},
            {"capture_timestamp":utcnow().isoformat(),"outcome":"recognized","identity_id":1},
        ])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),0)

    def test_global_rules_are_independent_and_once_per_session(self):
        for rule_type, outcome in (("unknown_face","unrecognized_face"),("system_error","processing_error")):
            self.notifications.save_rule({"identityId":None,"ruleType":rule_type,
                "emailAddress":"ops@example.com","phoneNumber":"","emailEnabled":True,"smsEnabled":False,
                "cameraIds":[]})
        occurred = utcnow()
        first = self.events.accept_event(1,"motion-first","motion",occurred,{})
        session_id,_ = self.events.attach_event(first.id,0,10,60)
        second = self.events.accept_event(1,"motion-second","motion",occurred+timedelta(seconds=5),{})
        linked_session,_ = self.events.attach_event(second.id,0,10,60)
        self.assertEqual(linked_session,session_id)
        self.events.add_results(session_id,[
            {"capture_timestamp":utcnow().isoformat(),"outcome":"unrecognized_face"},
            {"capture_timestamp":utcnow().isoformat(),"outcome":"processing_error"},
        ])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),2)
        self.assertEqual({item["outcome"] for item in self.notifications.deliveries()},
                         {"unrecognized_face","processing_error"})
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),0)

    def test_system_fallback_handles_session_failure_without_results(self):
        self.notifications.save_rule({"identityId":None,"ruleType":"system_error",
            "emailAddress":"ops@example.com","phoneNumber":"","emailEnabled":True,"smsEnabled":False,
            "cameraIds":[]})
        event = self.events.accept_event(1,"motion-failed","motion",utcnow(),{})
        session_id,_ = self.events.attach_event(event.id,0,10,60)
        self.events.transition_session(session_id,"failed",error_code="recognition_failed",
                                       error_message="model unavailable")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),1)
        self.assertEqual(self.notifications.deliveries()[0]["outcome"],"processing_error")

    def test_claim_retry_and_test_delivery(self):
        rule_id=self.save_identity_rule()
        delivery_id=self.notifications.enqueue_test(rule_id,"email","http://localhost")
        row=self.notifications.claim("worker",30)
        self.assertEqual(row["id"],delivery_id)
        self.notifications.failed(row,"temporary",False,5)
        self.assertEqual(self.notifications.deliveries()[0]["status"],"retrying")


class NotificationWorkerEmailTests(unittest.TestCase):
    class Repository:
        @staticmethod
        def screenshot(_screenshot_id): return None

    @staticmethod
    def row():
        return {"channel":"email","destination":"person@example.com","is_test":1,
                "outcome":"test","camera_name":"Test","occurred_at":utcnow().isoformat(),
                "identity_name":None,"event_link":None,"screenshot_id":None}

    def worker(self):
        providers = ProviderSettings(smtp_host="smtp.example.com", smtp_port=587,
            smtp_username="user", smtp_password="secret", smtp_sender="alerts@example.com",
            smtp_tls_mode="starttls")
        return NotificationWorker(self.Repository(), providers, Path("."))

    def test_email_has_trace_id_and_exercises_starttls_and_authentication(self):
        class Server:
            def __init__(self, *_args, **_kwargs): self.ehlo_count = 0; self.message = None
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def ehlo(self): self.ehlo_count += 1
            def starttls(self, **_kwargs): self.started_tls = True
            def login(self, username, password): self.credentials = (username, password)
            def send_message(self, message): self.message = message; return {}
        server = Server()
        with patch("verifeye.notifications.smtplib.SMTP", return_value=server):
            message_id = self.worker()._send(self.row())
        self.assertEqual(message_id, server.message["Message-ID"])
        self.assertTrue(message_id.endswith("@example.com>"))
        self.assertEqual(server.ehlo_count, 2)
        self.assertTrue(server.started_tls)
        self.assertEqual(server.credentials, ("user", "secret"))

    def test_recipient_refusal_is_not_recorded_as_sent(self):
        class Server:
            def __init__(self, *_args, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def ehlo(self): pass
            def starttls(self, **_kwargs): pass
            def login(self, *_args): pass
            def send_message(self, _message): return {"person@example.com": (550, b"blocked")}
        with patch("verifeye.notifications.smtplib.SMTP", Server):
            with self.assertRaises(PermanentDeliveryError): self.worker()._send(self.row())


if __name__ == "__main__": unittest.main()
