"""W8: released observations and owner signals reach butler-core as metadata, through an outbox that survives outages."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from butler_ambient.core_link import CoreLink
from butler_ambient.delivery import ZONE
from butler_ambient.service import Service
from butler_ambient.store import Store


class FakeCore:
    """Stands in for the deployed client module: records calls, can be down, refusing or switched off."""

    class CoreError(RuntimeError):
        def __init__(self, code="invalid"):
            super().__init__(code)
            self.code = code

    def __init__(self):
        self.calls, self.down, self.on, self.refuse = [], False, True, False

    def switched(self, name):
        return self.on and name == "observation"

    def Client(self, timeout=None):
        core = self

        class Client:
            def call(self, method, **params):
                if core.down:
                    raise ConnectionError("down")
                if core.refuse:
                    raise FakeCore.CoreError("invalid")
                core.calls.append((method, params))
                return {"ok": True}
        return Client()


class CoreLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.core = FakeCore()
        self.link = CoreLink(self.core, self.root, clock=lambda: 1000.0, interval=0.05)
        self.link.start = lambda: None                # flush by hand in tests

    def tearDown(self):
        self.temp.cleanup()

    def test_observation_event_carries_span_and_counts_only(self):
        facts = [{"kind": "activity", "detail": "Борис читает договор с Анной"}, {"kind": "object", "detail": "чашка"}]
        self.link.observation_released("obs-1", "camera", 990.0, 1000.0, facts)
        self.assertEqual(self.link.flush(), 1)
        method, params = self.core.calls[0]
        event = params["event"]
        self.assertEqual(method, "events.admit")
        self.assertEqual((event["source"], event["kind"], event["privacy"], event["dedup_key"]),
                         ("ambient:camera", "observation", "raw_local_only", "ambient:observation:obs-1"))
        self.assertEqual(event["payload"], {"source": "camera", "duration_s": 10.0, "facts": 2, "kinds": ["activity", "object"]})
        self.assertNotIn("Анной", json.dumps(params, ensure_ascii=False))

    def test_outbox_keeps_order_through_an_outage(self):
        self.core.down = True
        self.link.feedback("full_pause", {"modality": "video+audio", "by": "owner", "transcript": "x" * 100})
        self.link.observation_released("obs-2", "microphone", 0.0, 5.0, [])
        self.assertEqual(self.link.flush(), 0)
        self.assertEqual(self.link.snapshot()["pending"], 2)
        self.assertEqual(self.link.failed, 1)
        self.core.down = False
        self.assertEqual(self.link.flush(), 2)
        self.assertEqual([m for m, _ in self.core.calls], ["feedback.record", "events.admit"])
        self.assertEqual(self.core.calls[0][1]["detail"], {"modality": "video+audio", "by": "owner"})   # long values dropped
        self.assertEqual(self.link.snapshot()["pending"], 0)

    def test_refused_record_is_dropped_and_the_queue_moves_on(self):
        self.core.refuse = True
        self.link.feedback("weird_kind", {})
        self.link.observation_released("obs-9", "camera", 0.0, 1.0, [])
        self.assertEqual(self.link.flush(), 2)
        self.assertEqual((self.link.rejected, self.link.snapshot()["pending"]), (2, 0))
        self.core.refuse = False
        self.link.observation_released("obs-10", "camera", 0.0, 1.0, [])
        self.assertEqual(self.link.flush(), 1)
        self.assertEqual(self.link.sent, 1)

    def test_switch_off_records_nothing(self):
        self.core.on = False
        self.link.observation_released("obs-3", "camera", 0.0, 1.0, [])
        self.assertFalse(self.link.outbox.exists())
        self.assertEqual(self.link.snapshot()["enabled"], False)


class ServiceSignalsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = dt.datetime.now(ZONE).replace(hour=12).timestamp()
        self.inputs = Mock(audio_status={"ready": True}, playback={"state": "idle", "id": None})
        self.service = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.core = FakeCore()
        self.service.core_link = CoreLink(self.core, Path(self.temp.name), clock=lambda: self.now)
        self.service.core_link.start = lambda: None
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def test_pause_resume_and_revocation_are_feedback_events(self):
        self.service.control("enabled", {"value": True})
        self.service.control("enabled", {"value": False})
        self.service.control("revoke", {"id": "owner"})
        self.service.core_link.flush()
        kinds = [params["kind"] for method, params in self.core.calls if method == "feedback.record"]
        self.assertEqual(kinds, ["resume", "full_pause", "consent_revoked"])

    def test_saved_observation_is_released_to_the_core(self):
        self.service.control("enabled", {"value": True})
        mid = self.store.media("camera", self.now - 5, self.now, b"raw", "jpg")
        observation = self.store.save_observation(mid, [{"kind": "activity", "detail": "работа"}])
        self.service.core_link.observation_released(observation, "camera", self.now - 5, self.now, [{"kind": "activity", "detail": "работа"}])
        self.service.core_link.flush()
        events = [params["event"] for method, params in self.core.calls if method == "events.admit"]
        self.assertEqual(events[0]["dedup_key"], f"ambient:observation:{observation}")
        self.assertNotIn("работа", json.dumps(events[0], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
