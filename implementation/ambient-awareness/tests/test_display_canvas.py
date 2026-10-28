"""The visual layer of the display: a live canvas (one card redrawn in place) and captions under pictures and diagrams."""
import io
import json
import unittest
from unittest.mock import Mock

from butler_ambient.display import CardRefused
from butler_ambient.mcp import serve

from test_display_buttons import ButtonCase


class CanvasTests(ButtonCase):
    def setUp(self):
        super().setUp()
        self.panel_calls = Mock()
        self.service.ensure_panel = self.panel_calls

    def snapshot(self):
        return self.display.snapshot()

    def diagram(self, source, **extra):
        return {"kind": "diagram", "title": "Архитектура", "mermaid": source, **extra}

    def test_a_replacement_is_the_same_card_in_the_same_place_with_new_content(self):
        first = self.display.present(self.diagram("graph TD; A-->B"))
        other = self.display.present({"kind": "text", "markdown": "рядом"})
        before = self.snapshot()
        self.now += 60; self.renew_owner()                                                                          # the redraw happens a minute later
        again = self.display.present(self.diagram("graph TD; A-->B-->C", title="Архитектура v2", replaces=first["id"]))
        self.assertEqual((again["id"], again["replaced"]), (first["id"], True))
        state = self.snapshot()
        self.assertEqual([c["id"] for c in state["cards"]], [first["id"], other["id"]])          # same order, no new row
        card = state["cards"][0]
        self.assertEqual((card["mermaid"], card["title"], card["rev"]), ("graph TD; A-->B-->C", "Архитектура v2", 1))
        self.assertEqual(card["at"], before["cards"][0]["at"])                                  # the row keeps its place
        self.assertEqual(card["updated_at"], self.now)
        self.assertEqual(state["current"], first["id"])
        self.assertEqual(self.display.counts["replaced"], 1)

    def test_every_redraw_raises_the_revision(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        for number in (1, 2, 3):
            self.display.present(self.diagram(f"graph TD; A-->B{number}", replaces=shown["id"]))
            self.assertEqual(self.snapshot()["cards"][0]["rev"], number)
        self.assertEqual(len(self.snapshot()["cards"]), 1)

    def test_the_window_does_not_come_forward_for_the_card_the_owner_is_already_looking_at(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        serial = self.snapshot()["raise_serial"]
        self.display.present(self.diagram("graph TD; A-->C", replaces=shown["id"]))
        self.assertEqual(self.snapshot()["raise_serial"], serial)
        self.assertEqual(self.panel_calls.call_count, 2)          # a panel that exited is still relaunched, a running one is never raised

    def test_a_card_the_owner_left_comes_forward_again_when_it_is_redrawn(self):
        first = self.display.present(self.diagram("graph TD; A-->B"))
        self.display.present({"kind": "text", "markdown": "другое"})
        serial = self.snapshot()["raise_serial"]
        self.display.present(self.diagram("graph TD; A-->C", replaces=first["id"]))
        state = self.snapshot()
        self.assertEqual((state["current"], state["raise_serial"]), (first["id"], serial + 1))

    def test_a_replacement_starts_over_buttons_status_and_choice(self):
        shown = self.display.present({"kind": "text", "markdown": "Выбор", "buttons": [{"label": "Да", "kind": "reply"}, {"label": "Нет", "kind": "reply"}]})
        self.display.action(shown["id"], "b0")
        self.assertTrue(self.snapshot()["cards"][0]["actions"][0]["used"])
        self.display.present({"kind": "text", "markdown": "Новый выбор", "buttons": [{"label": "Ещё раз", "kind": "reply"}], "replaces": shown["id"]})
        card = self.snapshot()["cards"][0]
        self.assertEqual((card["status"], [a["label"] for a in card["actions"]], card["actions"][0]["used"]), (None, ["Ещё раз"], False))
        self.display.action(shown["id"], "b0")                                                    # the new button works once more
        self.assertEqual(self.chat.send.call_count, 2)
        with self.assertRaises(ValueError):
            self.display.action(shown["id"], "b1")                                                # the old second button is gone

    def test_a_card_may_change_kind_when_it_is_redrawn(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        self.display.present({"kind": "text", "markdown": "Теперь словами", "replaces": shown["id"]})
        card = self.snapshot()["cards"][0]
        self.assertEqual((card["kind"], card["markdown"], "mermaid" in card), ("text", "Теперь словами", False))

    def test_an_email_card_can_be_replaced_too(self):
        self.mail_output["card"] = {"key": "k", "sender": "A", "subject": "S", "date": "d", "account": "a", "summary": "", "body": "b", "draft": ""}
        self.display.run = self.fake_run
        shown = self.display.present({"kind": "email", "key": "k"})
        self.mail_output["card"]["subject"] = "S2"
        self.display.present({"kind": "email", "key": "k", "replaces": shown["id"]})
        card = self.snapshot()["cards"][0]
        self.assertEqual((len(self.snapshot()["cards"]), card["title"], card["rev"]), (1, "S2", 1))

    def test_an_unknown_or_malformed_target_is_refused_with_its_reason_and_changes_nothing(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        serial = self.snapshot()["raise_serial"]
        for bad, reason in (("no-such-card", "replaces: no such card"), ("", "invalid replaces"), ("я" * 65, "invalid replaces"), (5, "invalid replaces")):
            with self.subTest(bad=str(bad)[:12]):
                with self.assertRaises(CardRefused) as caught:
                    self.display.present(self.diagram("graph TD; X-->Y", replaces=bad))
                self.assertEqual(str(caught.exception), reason)
        state = self.snapshot()
        self.assertEqual((len(state["cards"]), state["cards"][0]["mermaid"], state["raise_serial"]), (1, "graph TD; A-->B", serial))

    def test_a_card_cleared_with_the_conversation_cannot_be_replaced(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        self.display.clear("owner")
        with self.assertRaises(CardRefused):
            self.display.present(self.diagram("graph TD; A-->C", replaces=shown["id"]))
        self.assertEqual(self.snapshot()["cards"], [])

    def test_a_replacement_is_validated_like_any_card_and_a_bad_one_leaves_the_card_untouched(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        with self.assertRaises(CardRefused):
            self.display.present({"kind": "diagram", "mermaid": "", "replaces": shown["id"]})
        with self.assertRaises(CardRefused):
            self.display.present(self.diagram("graph TD; A-->C", replaces=shown["id"], buttons=[{"label": "a", "kind": "link", "url": "file:///x"}]))
        card = self.snapshot()["cards"][0]
        self.assertEqual((card["mermaid"], card["rev"]), ("graph TD; A-->B", 0))

    def test_the_agent_is_told_it_was_a_replacement_and_the_schema_offers_the_field(self):
        shown = self.display.present(self.diagram("graph TD; A-->B"))
        def call(command, args):
            return self.run_command(command, args)["result"]
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "butler_display", "arguments": self.diagram("graph TD; A-->C", replaces=shown["id"])}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "butler_display", "arguments": self.diagram("graph TD; A-->C", replaces="nope")}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}]
        out = io.StringIO()
        serve(self.root, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out, call)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(json.loads(answers[1]["result"]["content"][0]["text"]), {"id": shown["id"], "shown": True, "kind": "diagram", "title": "Архитектура", "replaced": True})
        self.assertEqual(answers[2]["result"]["content"][0]["text"], "Card refused: replaces: no such card")
        tool = next(t for t in answers[3]["result"]["tools"] if t["name"] == "butler_display")
        self.assertIn("replaces", tool["inputSchema"]["properties"])
        self.assertIn("redrawn in place", tool["description"])


class CaptionTests(ButtonCase):
    def snapshot(self):
        return self.display.snapshot()["cards"]

    def test_a_diagram_and_an_image_may_carry_a_markdown_caption(self):
        image = self.media / "a.png"
        image.write_bytes(b"\x89PNG")
        self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->B", "markdown": "  **Вход** идёт в *ядро*.  "})
        self.display.present({"kind": "image", "image": str(image), "markdown": "Скриншот"})
        cards = self.snapshot()
        self.assertEqual([(c["kind"], c["markdown"]) for c in cards], [("diagram", "**Вход** идёт в *ядро*."), ("image", "Скриншот")])

    def test_the_caption_is_optional_and_absent_means_absent(self):
        self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->B"})
        self.assertNotIn("markdown", self.snapshot()[0])

    def test_a_caption_is_bounded_at_two_thousand_characters_but_a_text_card_keeps_its_own_limit(self):
        self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->B", "markdown": "я" * 2000})
        for bad in ("", "   ", "я" * 2001, 5):
            with self.assertRaises(CardRefused) as caught:
                self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->B", "markdown": bad})
            self.assertEqual(str(caught.exception), "invalid caption")
        self.display.present({"kind": "text", "markdown": "я" * 8000})
        with self.assertRaises(CardRefused):
            self.display.present({"kind": "text", "markdown": "я" * 8001})

    def test_video_and_email_cards_still_take_no_markdown(self):
        video = self.media / "a.mp4"
        video.write_bytes(b"x")
        with self.assertRaises(CardRefused) as caught:
            self.display.present({"kind": "video", "video": str(video), "markdown": "подпись"})
        self.assertEqual(str(caught.exception), "markdown does not belong to a video card")

    def test_redrawing_without_a_caption_removes_the_old_one(self):
        shown = self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->B", "markdown": "старая подпись"})
        self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->C", "replaces": shown["id"]})
        self.assertNotIn("markdown", self.snapshot()[0])


DRAWING = ('<svg xmlns="http://www.w3.org/2000/svg" width="900" height="500" viewBox="0 0 800 450"><rect x="10" y="10" width="200" height="80" fill="#143d2a"/>'
           '<text x="20" y="50" fill="#d6ffe0">Ядро</text></svg>')


class SvgCardTests(ButtonCase):
    def snapshot(self):
        return self.display.snapshot()["cards"]

    def test_the_card_stores_the_sanitised_drawing_not_the_original_text(self):
        shown = self.display.present({"kind": "svg", "title": "Ядро", "svg": DRAWING, "markdown": "Главное — ядро."})
        card = self.snapshot()[0]
        self.assertEqual((shown["kind"], card["kind"], card["title"], card["markdown"]), ("svg", "svg", "Ядро", "Главное — ядро."))
        self.assertTrue(card["svg"].startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450">'))
        self.assertNotIn('width="900"', card["svg"])
        self.assertNotEqual(card["svg"], DRAWING)

    def test_the_default_title_and_the_row_label(self):
        self.display.present({"kind": "svg", "svg": DRAWING})
        self.assertEqual(self.snapshot()[0]["title"], "Рисунок")

    def test_a_hostile_drawing_is_refused_with_its_reason_and_nothing_is_shown(self):
        for source, reason in (('<svg viewBox="0 0 1 1"><script>x</script></svg>', "svg: element not allowed: <script>"),
                               ('<svg viewBox="0 0 1 1" onload="x"/>', "svg: attribute not allowed: onload"),
                               ('<svg><rect/></svg>', "svg: the root needs a viewBox such as 0 0 800 450"),
                               ("<svg", "svg: not well-formed XML"),
                               ("", "invalid svg"), ("   ", "invalid svg"), (5, "invalid svg"), (None, "invalid svg"),
                               (DRAWING + " " * 60001, "svg: too long")):
            with self.subTest(reason=reason):
                with self.assertRaises(CardRefused) as caught:
                    self.display.present({"kind": "svg", "svg": source})
                self.assertEqual(str(caught.exception), reason)
        with self.assertRaises(CardRefused) as missing:
            self.display.present({"kind": "svg"})
        self.assertEqual(str(missing.exception), "invalid svg")
        self.assertEqual(self.snapshot(), [])

    def test_the_drawing_field_belongs_to_the_svg_card_only(self):
        for card in ({"kind": "text", "markdown": "x", "svg": DRAWING}, {"kind": "diagram", "mermaid": "graph TD", "svg": DRAWING}):
            with self.assertRaises(CardRefused) as caught:
                self.display.present(card)
            self.assertEqual(str(caught.exception), "svg does not belong to a " + card["kind"] + " card")
        with self.assertRaises(CardRefused) as caught:
            self.display.present({"kind": "svg", "svg": DRAWING, "mermaid": "graph TD"})
        self.assertEqual(str(caught.exception), "mermaid does not belong to a svg card")

    def test_a_drawing_takes_buttons_a_caption_and_can_replace_or_be_replaced(self):
        shown = self.display.present({"kind": "svg", "svg": DRAWING, "buttons": [{"label": "Подробнее", "kind": "reply"}]})
        self.assertEqual([a["label"] for a in self.snapshot()[0]["actions"]], ["Подробнее"])
        self.display.present({"kind": "svg", "svg": DRAWING.replace("Ядро", "Ядро 2"), "replaces": shown["id"]})
        self.assertEqual((len(self.snapshot()), self.snapshot()[0]["rev"], "Ядро 2" in self.snapshot()[0]["svg"]), (1, 1, True))
        self.display.present({"kind": "diagram", "mermaid": "graph TD; A-->B", "replaces": shown["id"]})
        self.assertNotIn("svg", self.snapshot()[0])
        self.display.present({"kind": "svg", "svg": DRAWING, "replaces": shown["id"]})
        self.assertEqual(self.snapshot()[0]["kind"], "svg")

    def test_the_caption_of_a_drawing_keeps_the_caption_limit(self):
        self.display.present({"kind": "svg", "svg": DRAWING, "markdown": "я" * 2000})
        with self.assertRaises(CardRefused) as caught:
            self.display.present({"kind": "svg", "svg": DRAWING, "markdown": "я" * 2001})
        self.assertEqual(str(caught.exception), "invalid caption")

    def test_the_agent_sees_the_kind_and_the_limits_in_the_tool_description_and_schema(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
        out = io.StringIO()
        serve(self.root, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out, lambda command, args: self.run_command(command, args)["result"])
        tool = next(t for t in json.loads(out.getvalue().splitlines()[1])["result"]["tools"] if t["name"] == "butler_display")
        self.assertIn("svg", tool["inputSchema"]["properties"])
        self.assertIn("svg", tool["inputSchema"]["properties"]["kind"]["enum"])
        self.assertEqual(tool["inputSchema"]["properties"]["svg"]["maxLength"], 60000)
        for phrase in ("svg: your own drawing", "needs a viewBox", "no scripts, styles, links, images"):
            self.assertIn(phrase, tool["description"])

    def test_a_refused_drawing_reaches_the_agent_as_an_error_that_names_the_rule(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "butler_display", "arguments": {"kind": "svg", "svg": '<svg viewBox="0 0 1 1"><script/></svg>'}}}]
        out = io.StringIO()
        serve(self.root, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out, lambda command, args: self.run_command(command, args)["result"])
        answer = json.loads(out.getvalue().splitlines()[1])["result"]
        self.assertTrue(answer["isError"])
        self.assertEqual(answer["content"][0]["text"], "Card refused: svg: element not allowed: <script>")


if __name__ == "__main__":
    unittest.main()
