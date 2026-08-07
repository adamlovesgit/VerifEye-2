import sqlite3
import tempfile
import unittest
from pathlib import Path

from verifeye.events import EventRepository, utcnow
from verifeye.notifications import NotificationError, NotificationRepository


SCHEMA = (Path(__file__).resolve().parents[2] / "src/verifeye/storage/schema.sql").read_text()


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
        return self.notifications.save_rule({"identityId":1,"isFallback":False,"emailAddress":"ada@example.com",
            "phoneNumber":"+15551234567","emailEnabled":True,"smsEnabled":True,
            "outcomes":["recognized"],"cameraIds":[]})

    def test_rule_crud_validation_and_optimistic_version(self):
        rule_id = self.save_identity_rule()
        rule = self.notifications.settings()["rules"][0]
        self.assertEqual(rule["identityName"], "Ada")
        rule.update(emailAddress="new@example.com")
        self.notifications.save_rule(rule, rule_id)
        with self.assertRaises(NotificationError): self.notifications.save_rule(rule, rule_id)
        self.assertTrue(self.notifications.delete_rule(rule_id))

    def test_fallback_is_unique_and_phone_is_e164(self):
        payload={"identityId":None,"isFallback":True,"emailAddress":"","phoneNumber":"+15551234567",
                 "emailEnabled":False,"smsEnabled":True,"outcomes":["no_face"],"cameraIds":[]}
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
        self.notifications.save_rule({"identityId":None,"isFallback":True,"emailAddress":"ops@example.com",
            "phoneNumber":"","emailEnabled":True,"smsEnabled":False,"outcomes":["unrecognized_face"],"cameraIds":[1]})
        event=self.events.accept_event(1,"motion-2","motion",utcnow(),{})
        session_id,_=self.events.attach_event(event.id,0,10,60)
        self.events.add_results(session_id,[{"capture_timestamp":utcnow().isoformat(),"outcome":"unrecognized_face"}])
        self.events.transition_session(session_id,"completed")
        self.assertEqual(self.notifications.enqueue_session(session_id,"http://localhost"),1)
        self.assertEqual(self.notifications.deliveries()[0]["outcome"],"unrecognized_face")

    def test_claim_retry_and_test_delivery(self):
        rule_id=self.save_identity_rule()
        delivery_id=self.notifications.enqueue_test(rule_id,"email","http://localhost")
        row=self.notifications.claim("worker",30)
        self.assertEqual(row["id"],delivery_id)
        self.notifications.failed(row,"temporary",False,5)
        self.assertEqual(self.notifications.deliveries()[0]["status"],"retrying")


if __name__ == "__main__": unittest.main()
