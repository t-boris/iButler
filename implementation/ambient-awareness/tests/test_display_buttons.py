"""Card buttons (0.9.0): a closed set the card declares, resolved here, never by the panel."""
import io
import json
import os
import unittest
from unittest.mock import Mock

from butler_ambient import display as module
from butler_ambient.display import (BUTTON_MAX, CardRefused, STATUS_LINK_OPENED, STATUS_OPEN_FAILED, STATUS_REPLY_AGAIN,
                                    STATUS_REPLY_FAILED, STATUS_REPLY_SENT, STATUS_REVEALED)
from butler_ambient.local_ui import snapshot
from butler_ambient.mcp import serve
from butler_ambient.panel_chat import PanelChat
from butler_ambient.service import Handler

import test_display
from test_display import Done
from test_panel_chat import FakeBrain, answered, row, service as chat_service, wait_for


class ButtonCase(unittest.TestCase):
    # The fixtures of test_display, borrowed by function so that its tests are not collected (and run) a second time here.
    setUp_display = test_display.DisplayTests.setUp
    tearDown = test_display.DisplayTests.tearDown
    fake_run = test_display.DisplayTests.fake_run
    allowed = test_display.DisplayTests.allowed
    renew_owner = test_display.DisplayTests.renew_owner

    def setUp(self):
        self.setUp_display()
        self.home = self.root / "home"
        (self.home / "projects").mkdir(parents=True)
        (self.home / "projects/report.txt").write_text("x")
        (self.home / ".hidden").mkdir()
        (self.home / ".hidden/secret.txt").write_text("x")
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "etc.txt").write_text("x")
        (self.home / "escape").symlink_to(self.outside)
        previous, module.HOME = module.HOME, self.home
        self.addCleanup(setattr, module, "HOME", previous)
        self.opened, self.open_code = [], 0
        self.display.run = self.open_run
        self.chat = Mock(send=Mock(return_value={"queued": True, "id": "t1"}))
        self.service.panel_chat = self.chat

    def open_run(self, arguments, **kwargs):
        self.opened.append(list(arguments))
        if isinstance(self.open_code, Exception):
            raise self.open_code
        return Done(self.open_code, "")

    def card(self, buttons, **extra):
        return {"kind": "text", "title": "Очистка кэшей", "markdown": "Нашёл **12 ГБ** кэшей.", "buttons": buttons, **extra}

    def state(self, index=-1):
        return self.display.snapshot()["cards"][index]

    def run_command(self, command, args):
        handler = Handler.__new__(Handler)
        handler.connection = Mock()
        handler.rfile = io.BytesIO(json.dumps({"command": command, "args": args}).encode() + b"\n")
        handler.wfile = io.BytesIO()
        handler.server = Mock(service=self.service)
        handler.handle()
        return json.loads(handler.wfile.getvalue())


class ButtonValidationTests(ButtonCase):
    def test_each_kind_is_accepted_with_exactly_its_own_fields(self):
        report = str(self.home / "projects/report.txt")
        shown = self.display.present(self.card([
            {"label": "Да, почистить", "kind": "reply"},
            {"label": "Документация", "kind": "link", "url": "https://example.com/docs"},
            {"label": "Написать", "kind": "link", "url": "mailto:boris@example.com"},
            {"label": "Показать отчёт", "kind": "reveal", "path": report}]))
        self.assertTrue(shown["shown"])
        card = self.state()
        self.assertEqual([(a["id"], a["kind"]) for a in card["actions"]], [("b0", "reply"), ("b1", "link"), ("b2", "link"), ("b3", "reveal")])
        self.assertEqual([a.get("hint") for a in card["actions"]], [None, "example.com", "boris@example.com", "~/projects/report.txt"])

    def test_every_card_kind_but_email_takes_buttons(self):
        image = self.media / "a.png"
        image.write_bytes(b"\x89PNG")
        video = self.media / "a.mp4"
        video.write_bytes(b"x")
        button = [{"label": "Дальше", "kind": "reply"}]
        for card in ({"kind": "image", "image": str(image)}, {"kind": "diagram", "mermaid": "graph TD; A-->B"},
                     {"kind": "video", "video": str(video)}, {"kind": "text", "markdown": "x"}):
            self.display.present({**card, "buttons": button})
            self.assertEqual([a["id"] for a in self.state()["actions"]], ["b0"], card["kind"])
        self.mail_output["card"] = {"key": "k", "sender": "A", "subject": "S", "date": "d", "account": "a", "summary": "", "body": "b", "draft": ""}
        with self.assertRaises(CardRefused) as caught:
            self.display.present({"kind": "email", "key": "k", "buttons": button})
        self.assertIn("email card", str(caught.exception))

    def test_a_card_without_buttons_has_no_actions(self):
        self.display.present({"kind": "text", "markdown": "x"})
        self.assertEqual(self.state()["actions"], [])

    def test_malformed_buttons_are_refused_with_a_reason_that_names_the_rule(self):
        report = str(self.home / "projects/report.txt")
        cases = {
            "invalid buttons": ["not a list", {"label": "a", "kind": "reply"}, [{"label": str(n), "kind": "reply"} for n in range(7)]],
            "invalid button kind": [[{"label": "a", "kind": "delete"}], [{"label": "a"}], ["a"], [{"label": "a", "kind": None}]],
            "a reply button takes exactly": [[{"label": "a", "kind": "reply", "url": "https://x.org"}], [{"label": "a", "kind": "reply", "text": "hidden"}]],
            "a link button takes exactly": [[{"label": "a", "kind": "link"}], [{"label": "a", "kind": "link", "url": "https://x.org", "path": report}]],
            "a reveal button takes exactly": [[{"label": "a", "kind": "reveal"}], [{"label": "a", "kind": "reveal", "path": report, "url": "https://x.org"}]],
            "invalid button label": [[{"label": "", "kind": "reply"}], [{"label": "  ", "kind": "reply"}], [{"label": "я" * 81, "kind": "reply"}], [{"label": 5, "kind": "reply"}]],
            "duplicate button label": [[{"label": "Да", "kind": "reply"}, {"label": "Да", "kind": "reply"}]],
            "button url not allowed": [[{"label": "a", "kind": "link", "url": url}] for url in (
                "file:///etc/passwd", "javascript:alert(1)", "ftp://x.org/a", "x-apple.systempreferences:", "https://x.org/a b", "https:///nohost", "//x.org")],
            "button path not found": [[{"label": "a", "kind": "reveal", "path": str(self.home / "missing.txt")}]],
            "button path is outside the home folder": [[{"label": "a", "kind": "reveal", "path": "/etc/hosts"}],
                                                      [{"label": "a", "kind": "reveal", "path": str(self.outside)}],
                                                      [{"label": "a", "kind": "reveal", "path": str(self.home / "escape/etc.txt")}],   # a symlink out of the home folder
                                                      [{"label": "a", "kind": "reveal", "path": str(self.home / "projects/../../outside")}]],
            "button path is hidden": [[{"label": "a", "kind": "reveal", "path": str(self.home / ".hidden/secret.txt")}],
                                      [{"label": "a", "kind": "reveal", "path": str(self.home / ".hidden")}]],
        }
        for reason, variants in cases.items():
            for buttons in variants:
                with self.subTest(reason=reason, buttons=str(buttons)[:60]):
                    with self.assertRaises(CardRefused) as caught:
                        self.display.present(self.card(buttons))
                    self.assertIn(reason, str(caught.exception))
        self.assertEqual(self.display.snapshot()["cards"], [])

    def test_a_refusal_never_echoes_the_content_it_refused(self):
        secret = "https://token-in-url.example/a b?key=SEKRET"
        with self.assertRaises(CardRefused) as caught:
            self.display.present(self.card([{"label": "ПРИВАТНАЯ-МЕТКА", "kind": "link", "url": secret}]))
        self.assertNotIn("SEKRET", str(caught.exception))
        self.assertNotIn("ПРИВАТНАЯ", str(caught.exception))
        for field, card in (("label", self.card([{"label": "ПРИВАТНАЯ-МЕТКА" * 8, "kind": "reply"}])),
                            ("title", self.card([], title="ПРИВАТНЫЙ-ЗАГОЛОВОК" * 8)),
                            ("markdown", {"kind": "text", "markdown": "ПРИВАТНЫЙ-ТЕКСТ" * 600})):
            with self.assertRaises(CardRefused) as caught:
                self.display.present(card)
            self.assertIn("invalid", str(caught.exception), field)
            self.assertNotIn("ПРИВАТН", str(caught.exception), field)

    def test_unknown_card_fields_are_still_refused(self):
        with self.assertRaises(CardRefused):
            self.display.present(self.card([], target="/x"))
        self.assertIn("buttons", module.CARD_SCHEMA["properties"])
        self.assertEqual((BUTTON_MAX, module.CARD_SCHEMA["properties"]["buttons"]["maxItems"]), (6, 6))
        self.display.present(self.card([{"label": str(n), "kind": "reply"} for n in range(6)]))         # six are fine, seven are not


class ButtonSecrecyTests(ButtonCase):
    def test_the_panel_gets_id_label_kind_and_hint_but_never_an_address_a_path_or_a_reply(self):
        report = str(self.home / "projects/report.txt")
        self.display.present(self.card([{"label": "Сайт", "kind": "link", "url": "https://example.com/private/path?token=SEKRET"},
                                        {"label": "Отчёт", "kind": "reveal", "path": report},
                                        {"label": "Да", "kind": "reply"}]))
        text = json.dumps(self.display.snapshot(), ensure_ascii=False)
        for forbidden in ("SEKRET", "/private/path", report, str(self.home), "target"):
            self.assertNotIn(forbidden, text)
        actions = self.state()["actions"]
        self.assertEqual([set(a) for a in actions], [{"id", "label", "kind", "hint"}, {"id", "label", "kind", "hint"}, {"id", "label", "kind", "used", "chosen"}])

    def test_persistent_display_clear_works_without_biometrics_and_removes_database_rows(self):
        self.display.present(self.card([{"label":"Да","kind":"reply"}]))
        self.service.dialogue_gate.invalidate("participant-check-failed")
        self.service.gate.invalidate(self.now,"owner-paused")
        self.assertTrue(self.run_command("ui-display-clear",{})["result"]["cleared"])
        self.assertEqual(self.store.execute("SELECT * FROM local_ui_history WHERE namespace='display'"),[])

    def test_display_selection_and_explicit_buttons_work_when_both_gates_deny(self):
        shown=self.display.present(self.card([{"label":"Да","kind":"reply"}]))
        self.service.dialogue_gate.invalidate("participant-check-failed")
        self.service.gate.invalidate(self.now,"owner-paused")
        self.assertFalse(self.allowed())
        self.assertEqual(self.display.select(shown['id'])['id'],shown['id'])
        self.assertEqual(len(self.display.snapshot()['cards']),1)
        self.display.action(shown['id'],'b0')
        self.chat.send.assert_called_once()
        self.assertEqual(self.chat.send.call_args.args,('Да',))


class ButtonActionTests(ButtonCase):
    def test_a_reply_sends_exactly_the_label_once_with_the_card_as_hidden_context(self):
        shown = self.display.present(self.card([{"label": "Да, почистить", "kind": "reply"}, {"label": "Нет", "kind": "reply"},
                                                {"label": "Сайт", "kind": "link", "url": "https://example.com/secret-path"}]))
        result = self.display.action(shown["id"], "b0")
        self.assertEqual(result, {"status": STATUS_REPLY_SENT.format(label="Да, почистить"), "locked": True})
        self.chat.send.assert_called_once()
        args, kwargs = self.chat.send.call_args
        self.assertEqual(args, ("Да, почистить",))
        context = kwargs["context"]
        self.assertIn("«Очистка кэшей»", context)
        self.assertIn("«Нет»", context)                       # the other option
        self.assertNotIn("«Да, почистить»", context)           # the chosen one is the message itself
        self.assertIn("Нашёл **12 ГБ** кэшей.", context)
        self.assertNotIn("secret-path", context)               # an address never goes along
        self.assertEqual(self.opened, [])

    def test_a_card_is_answered_once_and_the_other_replies_are_disabled(self):
        shown = self.display.present(self.card([{"label": "Да", "kind": "reply"}, {"label": "Нет", "kind": "reply"},
                                                {"label": "Сайт", "kind": "link", "url": "https://example.com"}]))
        self.display.action(shown["id"], "b1")
        again = self.display.action(shown["id"], "b0")
        self.assertEqual(again, {"status": STATUS_REPLY_AGAIN, "locked": True})
        self.assertEqual(self.chat.send.call_count, 1)
        self.assertNotIn("reply_choice", self.state())
        actions = {a["id"]: a for a in self.state()["actions"]}
        self.assertEqual((actions["b0"]["used"], actions["b0"]["chosen"], actions["b1"]["used"], actions["b1"]["chosen"]), (True, False, True, True))
        self.assertNotIn("used", actions["b2"])                # a link stays usable
        self.display.action(shown["id"], "b2")
        self.assertEqual(self.opened, [["/usr/bin/open", "https://example.com"]])

    def test_a_reply_that_could_not_be_sent_can_be_tried_again(self):
        shown = self.display.present(self.card([{"label": "Да", "kind": "reply"}]))
        self.chat.send.side_effect = ValueError("слишком много сообщений ждут ответа")
        result = self.display.action(shown["id"], "b0")
        self.assertEqual(result, {"status": STATUS_REPLY_FAILED.format(reason="слишком много сообщений ждут ответа")})      # not locked: try again
        self.assertFalse(self.state()["actions"][0]["used"])
        self.chat.send.side_effect = None
        self.assertEqual(self.display.action(shown["id"], "b0")["status"], STATUS_REPLY_SENT.format(label="Да"))
        self.assertEqual(self.chat.send.call_count, 2)

    def test_without_a_written_conversation_a_reply_is_an_error_not_a_silent_success(self):
        shown = self.display.present(self.card([{"label": "Да", "kind": "reply"}]))
        self.service.panel_chat = None
        try:
            with self.assertRaises(ValueError):
                self.display.action(shown["id"], "b0")
        finally:
            self.service.panel_chat = self.chat                 # Service.stop() needs one
        self.assertFalse(self.state()["actions"][0]["used"])

    def test_a_link_opens_the_address_and_reports_the_host_only(self):
        shown = self.display.present(self.card([{"label": "Документация", "kind": "link", "url": "https://example.com/docs?token=SEKRET"}]))
        result = self.display.action(shown["id"], "b0")
        self.assertEqual(self.opened, [["/usr/bin/open", "https://example.com/docs?token=SEKRET"]])
        self.assertEqual(result["status"], STATUS_LINK_OPENED.format(hint="example.com"))
        self.assertNotIn("SEKRET", result["status"])
        self.assertEqual(self.state()["status"], result["status"])

    def test_reveal_shows_the_item_in_finder(self):
        report = str((self.home / "projects/report.txt").resolve())
        shown = self.display.present(self.card([{"label": "Отчёт", "kind": "reveal", "path": report}]))
        self.assertEqual(self.display.action(shown["id"], "b0"), {"status": STATUS_REVEALED})
        self.assertEqual(self.opened, [["/usr/bin/open", "-R", report]])

    def test_a_failed_open_is_reported_as_a_failure_without_the_system_message(self):
        shown = self.display.present(self.card([{"label": "Сайт", "kind": "link", "url": "https://example.com"}]))
        for failure in (1, OSError("/Users/boris/secret not found")):
            self.open_code = failure
            result = self.display.action(shown["id"], "b0")
            self.assertEqual(result["status"], STATUS_OPEN_FAILED)
            self.assertNotIn("secret", result["status"])
        self.assertEqual(self.display.counts["button-failed"], 2)

    def test_only_declared_actions_run_and_only_on_their_own_card(self):
        first = self.display.present(self.card([{"label": "Да", "kind": "reply"}]))
        second = self.display.present(self.card([{"label": "Нет", "kind": "reply"}, {"label": "Сайт", "kind": "link", "url": "https://example.com"}]))
        for bad in ("b9", "b-1", "send", "open-reply-in-mail", "", "B0"):
            with self.assertRaises(ValueError, msg=bad):
                self.display.action(first["id"], bad)
        with self.assertRaises(ValueError):
            self.display.action(first["id"], "b1")             # b1 exists on the second card only
        self.assertEqual((self.chat.send.call_count, self.opened), (0, []))
        self.display.action(second["id"], "b1")
        self.assertEqual(len(self.opened), 1)

    def test_diagnostics_and_counts_never_carry_labels_addresses_or_paths(self):
        report = str(self.home / "projects/report.txt")
        shown = self.display.present(self.card([{"label": "ПРИВАТНАЯ-МЕТКА", "kind": "reply"},
                                                {"label": "Сайт", "kind": "link", "url": "https://private.example/SEKRET"},
                                                {"label": "Отчёт", "kind": "reveal", "path": report}]))
        for action in ("b0", "b1", "b2"):
            self.display.action(shown["id"], action)
        noted = json.dumps([call.args for call in self.service.voice._debug.call_args_list], ensure_ascii=False)
        for text in (noted, json.dumps(self.display.counts)):
            for forbidden in ("ПРИВАТНАЯ", "SEKRET", "private.example", "report.txt", str(self.home)):
                self.assertNotIn(forbidden, text)
        self.assertEqual({k: v for k, v in self.display.counts.items() if k.startswith("button-")},
                         {"button-reply": 1, "button-link": 1, "button-reveal": 1})

    def test_the_email_card_keeps_its_own_action(self):
        self.mail_output["card"] = {"key": "k", "sender": "A", "subject": "S", "date": "d", "account": "a", "summary": "", "body": "b", "draft": "Ответ."}
        self.mail_output["open-reply"] = {"opened": True}
        self.display.run = self.fake_run
        shown = self.display.present({"kind": "email", "key": "k"})
        self.assertEqual([a["id"] for a in self.state()["actions"]], ["open-reply-in-mail"])
        self.assertEqual(self.display.action(shown["id"], "open-reply-in-mail")["status"], module.STATUS_OPENED)


class ButtonAgentFlowTests(ButtonCase):
    def test_a_malformed_card_comes_back_to_the_agent_with_its_reason(self):
        answer = self.run_command("display", {"kind": "text", "markdown": "x", "buttons": [{"label": "a", "kind": "reveal", "path": "/etc/hosts"}]})
        self.assertEqual(answer, {"result": {"shown": False, "refused": "button path is outside the home folder"}})
        good = self.run_command("display", self.card([{"label": "Да", "kind": "reply"}]))["result"]
        self.assertTrue(good["shown"])

    def test_a_card_is_shown_even_when_the_owner_lease_has_expired(self):
        # Owner, 2026-10-27: "убери все приватные фичи, показывай все всегда". The agent's card no longer needs the confirmed session.
        self.assertTrue(self.allowed())
        self.now += 3600                                        # the owner's dialogue session lapsed
        self.assertFalse(self.allowed())
        answer = self.run_command("display", self.card([{"label": "Да", "kind": "reply"}]))
        self.assertTrue(answer["result"]["shown"])
        cards = snapshot(self.service)["display"]["cards"]
        self.assertEqual(len(cards),1)
        self.renew_owner()
        self.assertEqual([[a["label"] for a in c["actions"]] for c in snapshot(self.service)["display"]["cards"]], [["Да"]])

    def test_a_malformed_card_is_still_refused_with_its_rule_when_the_session_is_closed(self):
        self.now += 3600
        self.assertFalse(self.allowed())
        answer = self.run_command("display", {"kind": "text", "markdown": "x", "buttons": [{"label": "a", "kind": "reveal", "path": "/etc/hosts"}]})
        self.assertEqual(answer, {"result": {"shown": False, "refused": "button path is outside the home folder"}})

    def test_the_mcp_tool_reports_the_field_to_correct_and_nothing_else(self):
        def call(command, args):
            return self.run_command(command, args)["result"]
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "butler_display", "arguments": {
                        "kind": "text", "markdown": "x", "buttons": [{"label": "a", "kind": "link", "url": "file:///etc/passwd"}]}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "butler_display", "arguments": self.card([{"label": "Да", "kind": "reply"}])}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}]
        out = io.StringIO()
        serve(self.root, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out, call)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertTrue(answers[1]["result"]["isError"])
        self.assertEqual(answers[1]["result"]["content"][0]["text"], "Card refused: button url not allowed")
        self.assertNotIn("passwd", json.dumps(answers[1]))
        shown = json.loads(answers[2]["result"]["content"][0]["text"])
        self.assertEqual(set(shown), {"id", "shown", "kind", "title"})
        tool = next(t for t in answers[3]["result"]["tools"] if t["name"] == "butler_display")
        self.assertIn("buttons", tool["inputSchema"]["properties"])
        self.assertIn("reply", tool["description"])


class McpOutputTests(unittest.TestCase):
    def test_the_agent_gets_only_the_card_id_even_if_the_service_returns_more(self):
        leaked = {"id": "c1", "shown": True, "kind": "text", "title": "T", "markdown": "СЕКРЕТНЫЙ ТЕКСТ", "image": "/Users/boris/secret.png",
                  "actions": [{"target": "https://x.org/SEKRET"}]}
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "butler_display", "arguments": {"kind": "text", "markdown": "x"}}}]
        out = io.StringIO()
        serve(None, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out, lambda command, args: dict(leaked))
        text = json.loads(out.getvalue().splitlines()[1])["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"id": "c1", "shown": True, "kind": "text", "title": "T"})
        self.assertNotIn("СЕКРЕТ", text)


class ReplyReachesTheChatTests(ButtonCase):
    """The real panel chat behind the button: the thread shows the label, the agent also gets the card's context."""

    def test_the_thread_shows_only_the_label_and_the_agent_receives_context_in_front_of_it(self):
        brain = FakeBrain(lambda text, on_text, token: "Хорошо, чищу.")
        chat = PanelChat(chat_service(), brain=brain, follower=False)
        self.addCleanup(chat.stop)
        self.service.panel_chat = chat
        shown = self.display.present(self.card([{"label": "Да, почистить", "kind": "reply"}, {"label": "Нет", "kind": "reply"}]))
        self.display.action(shown["id"], "b0")
        turn = chat.snapshot()["entries"][0]["id"]
        self.assertTrue(answered(chat, turn))
        self.assertEqual(row(chat, turn)["text"], "Да, почистить")
        sent = brain.calls[0]["text"]
        self.assertTrue(sent.endswith("\n\nДа, почистить"))
        self.assertIn("«Очистка кэшей»", sent)
        self.assertNotIn("context", json.dumps(chat.snapshot(), ensure_ascii=False))
        self.assertNotIn("Нашёл", json.dumps(chat.snapshot(), ensure_ascii=False))     # the card text stays out of the thread


class PanelContextTests(unittest.TestCase):
    def make(self):
        self.brain = FakeBrain(lambda text, on_text, token: "ок")
        chat = PanelChat(chat_service(), brain=self.brain, follower=False)
        self.addCleanup(chat.stop)
        return chat

    def test_context_goes_to_the_agent_in_front_of_the_text_and_never_into_the_rows(self):
        chat = self.make()
        sent = chat.send("Да", context="Карточка «Х».")
        self.assertTrue(answered(chat, sent["id"]))
        self.assertEqual(self.brain.calls[0]["text"], "Карточка «Х».\n\nДа")
        self.assertNotIn("Карточка", json.dumps(chat.snapshot(), ensure_ascii=False))

    def test_a_plain_message_is_unchanged_and_bad_context_is_refused_before_queueing(self):
        chat = self.make()
        self.assertTrue(answered(chat, chat.send("привет")["id"]))
        self.assertEqual(self.brain.calls[0]["text"], "привет")
        for bad in ("", "   ", 5, "я" * 1501):
            with self.assertRaises(ValueError):
                chat.send("привет", context=bad)
        self.assertEqual(len(chat.snapshot()["entries"]), 2)

    def test_context_is_not_logged(self):
        chat = self.make()
        with self.assertLogs("butler.ambient", level="INFO") as logs:
            self.assertTrue(answered(chat, chat.send("Да", context="СЕКРЕТНЫЙ-КОНТЕКСТ")["id"]))
        self.assertNotIn("СЕКРЕТНЫЙ", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
