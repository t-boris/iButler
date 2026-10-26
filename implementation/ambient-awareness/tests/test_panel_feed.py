"""The owner panel's timeline: who said what and when, images, and the notices Butler sent."""
import unittest

from butler_ambient.delivery import CoreNotices
from butler_ambient.local_ui import feed


class Stub:
    notices = None


class FeedTests(unittest.TestCase):
    def result(self, allowed=True, events=(), artifacts=()):
        return {"voice": {"dialogue_allowed": allowed, "debug": {"events": list(events)}},
                "visual": {"artifacts": list(artifacts)}}

    def test_dialogue_events_carry_author_kind_and_label_in_time_order(self):
        events = [{"at": 20.0, "event": "reply", "detail": "Готово.", "who": "Butler"},
                  {"at": 10.0, "event": "heard", "detail": "Батлер, покажи задачи", "who": "Борис"},
                  {"at": 15.0, "event": "participant-check-wait", "detail": "", "who": "Система"},
                  {"at": None, "event": "heard", "detail": "dropped: no time"}]
        entries = feed(Stub(), self.result(events=events))
        self.assertEqual([e["kind"] for e in entries], ["heard", "event", "reply"])
        self.assertEqual([e["who"] for e in entries], ["Борис", "Система", "Butler"])
        self.assertEqual(entries[0]["label"], "РАСПОЗНАНО")
        self.assertEqual(entries[2]["text"], "Готово.")
        self.assertTrue(all(e["id"] for e in entries))

    def test_the_same_utterance_heard_passively_and_in_dialogue_is_one_attributed_line(self):
        events = [{"at": 10.0, "event": "heard", "detail": "Расскажи о моих проектах", "who": "Голос"},
                  {"at": 12.0, "event": "heard", "detail": "Расскажи о моих проектах", "who": "Борис"},
                  {"at": 40.0, "event": "heard", "detail": "Расскажи о моих проектах", "who": "Голос"}]
        entries = feed(Stub(), self.result(events=events))
        self.assertEqual([(e["at"], e["who"]) for e in entries], [(10.0, "Борис"), (40.0, "Голос")])

    def test_images_appear_only_with_a_visible_path(self):
        artifacts = [{"id": "a1", "kind": "captured", "at": 5.0, "path": "/synthetic/a1.jpg", "caption": "Снимок"},
                     {"id": "a2", "kind": "generated", "at": 6.0, "caption": "hidden on pause"}]
        entries = feed(Stub(), self.result(artifacts=artifacts))
        self.assertEqual([(e["kind"], e["image"], e["label"]) for e in entries], [("image", "/synthetic/a1.jpg", "СНИМОК")])

    def test_notices_show_only_while_the_owner_is_present(self):
        class Notices:
            started = 0
            def start(self): self.started += 1
            def entries(self): return [{"id": "delivery:dl-1", "at": 30.0, "who": "Butler → Telegram", "kind": "notification",
                                        "label": "status · доставлено", "text": "starting — plan"}]
        service = Stub(); service.notices = Notices()
        self.assertEqual([e["kind"] for e in feed(service, self.result(allowed=True))], ["notification"])
        self.assertEqual(feed(service, self.result(allowed=False)), [])
        self.assertEqual(service.notices.started, 1)

    def test_core_notices_keep_the_last_list_when_the_core_is_away(self):
        calls = []
        class Module:
            mode = "ok"
            class Client:
                def __init__(self, timeout=None): calls.append(timeout)
                def call(self, method, **params):
                    if Module.mode == "down": raise ConnectionError("down")
                    return [{"id": "dl-1", "kind": "status", "state": "delivered", "created_at": 1.0, "persona": "butler", "content": "done"},
                            {"id": "dl-2", "kind": "imported", "state": "uncertain", "created_at": 2.0, "persona": "butler"},
                            {"id": "dl-3", "kind": "result", "state": "pending", "created_at": 3.0, "persona": "tetyana-dyck", "content": "x"}]
        notices = CoreNotices(Module)
        notices.poll()
        self.assertEqual([(e["id"], e["label"], e["text"]) for e in notices.entries()], [("delivery:dl-1", "status · доставлено", "done")])
        Module.mode = "down"
        notices.poll()
        self.assertEqual(len(notices.entries()), 1)
        self.assertEqual(calls, [2.0, 2.0])
        self.assertIsNone(CoreNotices(None).start())


if __name__ == "__main__":
    unittest.main()
