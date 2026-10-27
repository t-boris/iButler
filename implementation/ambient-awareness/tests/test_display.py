"""The conversational display (feature butler-conversational-display): cards, history, visibility, actions."""
import datetime as dt
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from butler_ambient import display as module
from butler_ambient.delivery import ZONE
from butler_ambient.display import Display, CARD_SCHEMA, STATUS_OPENED
from butler_ambient.local_ui import feed, snapshot
from butler_ambient.mcp import serve
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.now = dt.datetime.now(ZONE).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.service = Service(self.store, Mock(audio_status={"ready": True}, playback={"state": "idle"}), Mock(), Mock(),
                               config={"butler_workspace": str(self.root / "workspace")}, clock=lambda: self.now, transport=Mock())
        self.runs = []
        self.service.display = self.display = Display(self.service, run=self.fake_run)
        self.service.voice = Mock(_debug=Mock(), state="ready", last_heard="", last_reply="", phase="idle", turn_committed=False,
                                  diagnostics=Mock(return_value={}), debug_snapshot=Mock(return_value={"events": [], "microphone": None}),
                                  general_permitted=Mock(return_value=False), private_state=Mock(return_value=False), visible_mode="private", ready=True)
        (self.root / "workspace/bin").mkdir(parents=True)
        (self.root / "workspace/bin/butler-mail").write_text("#!/bin/sh\n")
        self.mail_output = {}
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True), self.now)
            self.service.dialogue_gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True, frozenset({"owner"}), speech_detected=True, voice_certain=True), self.now, True)
        module_media = self.root / "media"
        module_media.mkdir()
        self.media = module_media
        self._roots = module.MEDIA_ROOT
        module.MEDIA_ROOT = module_media

    def tearDown(self):
        module.MEDIA_ROOT = self._roots
        self.service.stop(); self.store.close(); self.temp.cleanup()

    def fake_run(self, arguments, **kwargs):
        self.runs.append(arguments)
        sub = arguments[1]
        value = self.mail_output.get(sub)
        if value is None:
            return Done(1, "")
        return Done(0, json.dumps(value))

    def allowed(self):
        return bool(self.service.voice_gate.permits(self.now))

    def lose_the_lease(self, reason):
        """What the service does when a participant check fails: the dialogue gate has already dropped, then the invalidation runs."""
        self.service.dialogue_gate.invalidate("participant-check-failed")
        with self.service.lock:
            self.service._invalidate(reason)

    # -- cards -----------------------------------------------------------------------------

    def test_text_card_is_validated_shown_and_raises_the_window_once(self):
        result = self.display.present({"kind": "text", "title": "Задачи на сегодня", "markdown": "- одна\n- две",
                                       "links": [{"label": "Todoist", "url": "https://todoist.com"}]})
        self.assertEqual((result["shown"], result["kind"]), (True, "text"))
        self.assertNotIn("markdown", result)                                                  # the caller gets an id, not content
        state = self.display.snapshot()
        self.assertEqual((len(state["cards"]), state["current"], state["raise_serial"]), (1, result["id"], 1))
        self.assertEqual(state["cards"][0]["links"], [{"label": "Todoist", "url": "https://todoist.com"}])
        self.service.voice._debug.assert_called_with("display", "Задачи на сегодня · текст")
        for bad in ({"kind": "text"}, {"kind": "text", "markdown": "x", "links": [{"label": "f", "url": "file:///etc/passwd"}]},
                    {"kind": "text", "markdown": "x", "image": "/tmp/a.png"}, {"kind": "poster"}, {"kind": "text", "markdown": "x", "extra": 1},
                    {"kind": "text", "markdown": "x" * 9000}):
            with self.assertRaises(ValueError):
                self.display.present(bad)
        self.assertEqual(self.display.snapshot()["raise_serial"], 1)

    def test_media_cards_accept_only_files_butler_owns_and_hide_them_on_pause(self):
        picture = self.media / "scene.png"; picture.write_bytes(b"\x89PNG")
        video = self.media / "episode.mp4"; video.write_bytes(b"\x00\x00\x00\x18ftyp")
        elsewhere = self.root / "elsewhere.png"; elsewhere.write_bytes(b"\x89PNG")
        link = self.media / "link.png"; link.symlink_to(elsewhere)
        self.display.present({"kind": "image", "image": str(picture)})
        self.display.present({"kind": "video", "video": str(video), "title": "Выпуск"})
        for bad in ({"kind": "image", "image": str(elsewhere)}, {"kind": "image", "image": str(link)},
                    {"kind": "video", "video": str(picture)}, {"kind": "video", "video": str(self.media / "missing.mp4")}):
            with self.assertRaises(ValueError):
                self.display.present(bad)
        shown = self.display.snapshot()["cards"]
        self.assertEqual([c.get("image") or c.get("video") for c in shown], [str(picture), str(video)])
        # Owner, 2026-10-27: "show everything always". A paused recording gate no longer swaps the media for a placeholder.
        self.service.gate.invalidate(self.now, "permission-paused")
        self.assertFalse(self.service.gate.allowed)
        again = snapshot(self.service)["display"]["cards"]
        self.assertEqual([c.get("image") or c.get("video") for c in again], [str(picture), str(video)])
        self.assertTrue(all("unavailable" not in c for c in again))
        video.unlink()
        self.assertEqual(self.display.snapshot()["cards"][1]["unavailable"], "missing")

    def test_diagram_card_carries_mermaid_source_only(self):
        result = self.display.present({"kind": "diagram", "title": "Архитектура", "mermaid": "graph TD; A-->B; B-->C"})
        card = self.display.snapshot()["cards"][0]
        self.assertEqual((card["id"], card["mermaid"], card["actions"]), (result["id"], "graph TD; A-->B; B-->C", []))

    def test_email_card_is_built_locally_from_the_mail_store_by_key(self):
        self.mail_output["card"] = {"key": "acc:<m1>", "sender": "Anna <anna@example.com>", "subject": "Встреча", "date": "25.10 09:12",
                                    "account": "t.boris@gmail.com", "summary": "Просит перенести", "body": "Борис, можно перенести…",
                                    "draft": "Анна, добрый день! Да, давайте в четверг."}
        result = self.display.present({"kind": "email", "key": "acc:<m1>"})
        self.assertEqual(result["title"], "Встреча")
        self.assertEqual(self.runs[-1][1:], ["card", "acc:<m1>", "--json"])                     # the key goes to butler-mail, nothing else
        card = self.display.snapshot()["cards"][0]
        self.assertEqual(card["email"]["body"], "Борис, можно перенести…")
        self.assertEqual(card["actions"], [{"id": "open-reply-in-mail", "label": "Открыть ответ в Mail.app"}])
        self.mail_output.pop("card")
        with self.assertRaises(ValueError):
            self.display.present({"kind": "email", "key": "acc:<unknown>"})

    # -- actions ---------------------------------------------------------------------------

    def test_open_reply_action_reports_opened_or_failed_and_never_sent(self):
        self.mail_output["card"] = {"key": "k", "sender": "A", "subject": "S", "date": "d", "account": "acc", "summary": "", "body": "b", "draft": "Ответ."}
        card = self.display.present({"kind": "email", "key": "k"})
        with self.assertRaises(ValueError):
            self.display.action(card["id"], "send")                                           # no such action exists
        self.mail_output["open-reply"] = {"opened": True, "outgoing_id": 42}
        result = self.display.action(card["id"], "open-reply-in-mail")
        self.assertEqual(result["status"], STATUS_OPENED)
        self.assertEqual(self.runs[-1][1:], ["open-reply", "k", "--json", "--text", "Ответ."])
        shown = self.display.snapshot()["cards"][0]
        self.assertEqual(shown["status"], STATUS_OPENED)
        self.assertNotIn("outgoing_id", shown)
        self.assertNotIn("отправлен.", STATUS_OPENED.split("—")[0])
        self.mail_output["open-reply"] = {"opened": False, "reason": "Mail.app не запущена"}
        result = self.display.action(card["id"], "open-reply-in-mail")
        self.assertIn("Не удалось открыть ответ в Mail.app: Mail.app не запущена", result["status"])
        self.assertIn("Письмо не отправлено", result["status"])
        self.mail_output.pop("open-reply")
        self.assertIn("Mail.app не ответила", self.display.action(card["id"], "open-reply-in-mail")["status"])
        text = self.display.present({"kind": "text", "markdown": "x"})
        with self.assertRaises(ValueError):
            self.display.action(text["id"], "open-reply-in-mail")

    # -- history (REQ-011) -------------------------------------------------------------------

    def test_history_is_navigable_reopening_runs_nothing_and_the_session_close_clears_it(self):
        first = self.display.present({"kind": "text", "title": "Первый", "markdown": "1"})
        self.mail_output["card"] = {"key": "k", "sender": "A", "subject": "S", "date": "d", "account": "acc", "summary": "", "body": "b", "draft": "Ответ."}
        second = self.display.present({"kind": "email", "key": "k"})
        runs = len(self.runs)
        self.assertEqual(self.display.select(first["id"])["id"], first["id"])
        self.assertEqual(self.display.snapshot()["current"], first["id"])
        self.assertEqual(self.display.select(second["id"])["kind"], "email")
        self.assertEqual(len(self.runs), runs)                                                   # reopening ran no mail command
        self.assertEqual(self.display.snapshot()["raise_serial"], 2)   # no new raise
        with self.assertRaises(ValueError):
            self.display.select("nope")
        with self.service.lock:
            self.service._invalidate("owner-paused")                                           # a hard close of the owner session
        state = self.display.snapshot()
        self.assertEqual((state["cards"], state["current"], state["cleared_reason"]), ([], None, "owner-paused"))
        fresh = Display(self.service)                                                          # a restart starts empty: nothing persisted
        self.assertEqual(fresh.snapshot()["cards"], [])
        for index in range(module.HISTORY + 5):
            self.display.present({"kind": "text", "markdown": str(index)})
        self.assertEqual(len(self.display.snapshot()["cards"]), module.HISTORY)

    def test_a_brief_loss_of_the_lease_keeps_the_history_and_only_a_real_close_clears_it(self):
        # Owner, 2026-10-26 (DEC-047): the display is not cleared "for nothing"; it ends with the conversation, not with a blink.
        for reason in ("permission-paused", "stale-participant-check", "participant-check-gap", "participant-check-unavailable", "source-unavailable"):
            with self.subTest(soft=reason):
                self.display.clear("fixture")
                self.display.present({"kind": "text", "title": "Схема", "markdown": "x"})
                self.display.present({"kind": "text", "title": "Вторая", "markdown": "y"})
                before = self.display.snapshot()
                self.lose_the_lease(reason)
                self.assertFalse(self.service.dialogue_gate.allowed)                              # the session really closed
                after = self.display.snapshot()
                self.assertEqual(([c["id"] for c in after["cards"]], after["current"], after["cleared_reason"]),
                                 ([c["id"] for c in before["cards"]], before["current"], None))
        for reason in ("owner-paused", "consent-revoked", "settings-change", "service-stopped", "owner-voice-enrollment", "owner-reference-updated"):
            with self.subTest(hard=reason):
                self.display.clear("fixture")
                self.display.present({"kind": "text", "title": "Схема", "markdown": "x"})
                self.lose_the_lease(reason)
                after = self.display.snapshot()
                self.assertEqual((after["cards"], after["current"], after["cleared_reason"]), ([], None, reason))

    def test_a_blink_does_not_clear_a_card_the_owner_has_answered_or_replaced(self):
        first = self.display.present({"kind": "text", "title": "Выбор", "markdown": "x", "buttons": [{"label": "Да", "kind": "reply"}, {"label": "Нет", "kind": "reply"}]})
        self.display.present({"kind": "text", "title": "Выбор", "markdown": "z", "replaces": first["id"]})
        self.lose_the_lease("permission-paused")
        card = self.display.snapshot()["cards"][0]
        self.assertEqual((card["id"], card["rev"], card["markdown"]), (first["id"], 1, "z"))

    # -- visibility (REQ-010) and the panel ---------------------------------------------------

    def test_cards_stay_visible_when_the_owner_lease_drops_and_ui_state_carries_display_and_version(self):
        shown = self.display.present({"kind": "text", "title": "Личное", "markdown": "x"})
        self.lose_the_lease("permission-paused")                                               # the participant is not recognised for a moment
        self.assertFalse(self.allowed())                                                       # the lease really is down
        state = snapshot(self.service)
        self.assertFalse(state["voice"]["dialogue_allowed"])
        self.assertEqual(state["version"], __import__("butler_ambient").__version__)
        self.assertEqual(([c["id"] for c in state["display"]["cards"]], state["display"]["current"], state["display"]["hidden"]),
                         ([shown["id"]], shown["id"], None))
        rows = feed(Mock(notices=None), {"voice": {"dialogue_allowed": True, "debug": {"events": [
            {"at": 5.0, "event": "display", "detail": "Личное · текст", "who": "Система"}]}}, "visual": {}})
        self.assertEqual((rows[0]["kind"], rows[0]["label"]), ("display", "ПОКАЗ"))

    def test_a_presentation_relaunches_an_exited_panel_but_never_a_running_one(self):
        launched = []
        class Proc:
            def __init__(self, alive): self.alive = alive
            def poll(self): return None if self.alive else 0
        self.service.panel_launch = lambda: launched.append(1) or Proc(True)
        self.service.panel = Proc(True)
        self.display.present({"kind": "text", "markdown": "a"})
        self.assertEqual(launched, [])
        self.service.panel = Proc(False)
        self.display.present({"kind": "text", "markdown": "b"})
        self.assertEqual(launched, [1])
        self.assertTrue(self.service.ensure_panel())                                           # the new process is alive: no second launch
        self.assertEqual(launched, [1])

    def test_ready_visual_result_appears_on_the_display(self):
        from butler_ambient.visual import Visual
        picture = self.root / "state/images/generated/a.png"
        picture.parent.mkdir(parents=True); picture.write_bytes(b"\x89PNG")
        visual = self.service.visual = Visual(self.service); visual.model = Mock()
        key = "verified-owner-voice:abc"
        self.store.execute("INSERT INTO visual_requests VALUES (?,?,?,?,?)", (key, "verified-owner-voice", "working", self.now, 0))
        aid = visual.artifact("generated", picture, key)
        self.store.execute("UPDATE visual_artifacts SET caption='Generated locally: кот' WHERE id=?", (aid,))
        visual._generate = Mock(return_value=[aid])
        visual._work((key, "generate-image", "кот", threading.Event(), self.service.gate.generation, False))
        cards = self.display.snapshot()["cards"]
        self.assertEqual([(c["kind"], c["image"], c["title"], c["origin"]) for c in cards], [("image", str(picture), "Generated locally: кот", "visual")])

    # -- MCP ----------------------------------------------------------------------------------

    def test_mcp_display_tool_returns_only_the_card_id_and_refuses_bad_cards(self):
        self.assertEqual(CARD_SCHEMA["additionalProperties"], False)
        calls = []
        def call(command, args):
            calls.append((command, args))
            return self.display.present(args, origin="agent")
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "butler_display", "arguments": {"kind": "text", "title": "Список", "markdown": "- a"}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "butler_display", "arguments": {"kind": "image", "image": "/etc/hosts"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "butler_display", "arguments": {"kind": "text", "markdown": "x", "path": "/x"}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}]
        out = io.StringIO()
        serve(self.root, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out, call)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        shown = json.loads(answers[1]["result"]["content"][0]["text"])
        self.assertEqual(set(shown), {"id", "shown", "kind", "title"})
        self.assertEqual(calls[0], ("display", {"kind": "text", "title": "Список", "markdown": "- a"}))
        self.assertTrue(answers[2]["result"]["isError"])
        self.assertIn("refused", answers[2]["result"]["content"][0]["text"])
        self.assertTrue(answers[3]["result"]["isError"])                                       # an unknown field never reaches the service
        self.assertEqual(len(calls), 2)
        self.assertIn("butler_display", [t["name"] for t in answers[4]["result"]["tools"]])


if __name__ == "__main__":
    unittest.main()
