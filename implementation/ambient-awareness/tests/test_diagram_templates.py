"""The Mermaid templates handed to the agent: well formed, inside the card limits, shipped, and in step with the agent's playbook."""
import contextlib
import io
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

import build_app
from butler_ambient import diagram_templates as templates_module
from butler_ambient.display import CardRefused, MERMAID_MAX
from butler_ambient.svg_clean import ELEMENTS, clean_svg

from test_display_buttons import ButtonCase

ROOT = Path(__file__).resolve().parent.parent


class TemplateDataTests(ButtonCase):
    def setUp(self):
        super().setUp()
        self.items = templates_module.load()

    def test_there_is_one_template_for_each_kind_of_picture_the_agent_is_told_about(self):
        self.assertEqual([item["id"] for item in self.items],
                         ["architecture", "layers", "pipeline", "c4-context", "sequence", "state", "er", "timeline", "bars", "quadrant", "mindmap"])

    def test_every_template_is_complete_and_unique(self):
        ids = [item["id"] for item in self.items]
        self.assertEqual(len(ids), len(set(ids)))
        for item in self.items:
            with self.subTest(template=item["id"]):
                self.assertEqual(set(item), {"id", "title", "use", "mermaid"})
                self.assertRegex(item["id"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertTrue(10 <= len(item["use"]) <= 200)
                self.assertTrue(item["mermaid"].endswith("\n"))

    def test_every_template_is_a_card_the_display_accepts(self):
        for item in self.items:
            with self.subTest(template=item["id"]):
                self.assertLess(len(item["mermaid"]), MERMAID_MAX // 4)          # room to grow to a real diagram
                self.display.present({"kind": "diagram", "title": item["id"], "mermaid": item["mermaid"]})
        self.assertEqual(len(self.display.snapshot()["cards"]), len(self.items))

    def test_every_template_is_a_diagram_type_the_bundled_mermaid_draws(self):
        for item in self.items:
            with self.subTest(template=item["id"]):
                self.assertIn(templates_module.kind_of(item["mermaid"]), templates_module.SUPPORTED)

    def test_the_types_the_bundle_cannot_draw_are_not_offered(self):
        text = "\n".join(item["mermaid"] for item in self.items)
        for unsupported in ("architecture-beta", "block-beta", "kanban", "packet-beta", "click "):
            self.assertNotIn(unsupported, text)

    def test_non_ascii_text_sits_in_quotes_where_the_mermaid_lexer_needs_it(self):
        by_id = {item["id"]: item["mermaid"] for item in self.items}
        for line in by_id["er"].splitlines():
            if "||" in line:
                self.assertRegex(line, r': "[^"]+"$')
        self.assertRegex(by_id["bars"], r'x-axis \["')
        for line in by_id["quadrant"].splitlines():
            if re.search(r"[А-Яа-я]", line) and not line.strip().startswith(("title", "%%")):
                self.assertIn('"', line)
        self.assertRegex(by_id["quadrant"], r"title [^\"\n]+\n")                 # a quoted title shows its quotes on screen

    def test_the_templates_carry_the_console_palette(self):
        for item in self.items:
            if templates_module.kind_of(item["mermaid"]) in ("flowchart", "C4Context"):
                with self.subTest(template=item["id"]):
                    self.assertIn("#143d2a", item["mermaid"])

    def test_the_supported_list_matches_what_the_bundled_mermaid_contains(self):
        bundle = (ROOT / "butler_ambient" / "assets" / "mermaid.min.js").read_text(encoding="utf-8", errors="replace")
        for name in templates_module.SUPPORTED:
            with self.subTest(supported=name):
                self.assertIn(name, bundle)
        # The playbook tells the agent these do not work; when a Mermaid upgrade adds one, revise the playbook and this list.
        for name in ("architecture-beta", "block-beta", "kanban", "packet-beta"):
            with self.subTest(unsupported=name):
                self.assertNotIn(name, bundle)

    def test_kind_of_skips_init_directives(self):
        self.assertEqual(templates_module.kind_of('%%{init: {"a": 1}}%%\nxychart-beta\n  bar [1]'), "xychart-beta")
        self.assertEqual(templates_module.kind_of(""), "")


class TemplateShippingTests(unittest.TestCase):
    def test_the_file_is_a_resource_of_the_app_bundle_and_of_the_wheel(self):
        self.assertIn("diagram_templates.json", build_app.RESOURCES)
        self.assertTrue((ROOT / "butler_ambient" / "assets" / "diagram_templates.json").is_file())
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("assets/*.json", data["tool"]["setuptools"]["package-data"]["butler_ambient"])


class DrawingExampleTests(unittest.TestCase):
    """The example drawing in the playbook is the one the sanitizer and the real panel are tested with."""

    def source(self):
        return templates_module.EXAMPLE.read_text(encoding="utf-8")

    def test_the_example_passes_the_sanitizer_and_keeps_what_it_draws(self):
        out = clean_svg(self.source())
        for kept in ("<marker", "<linearGradient", 'marker-end="url(#arrow)"', 'fill="url(#glow)"', "<circle", "<title>Путь голоса</title>", "Ambient", "Агент", "решает"):
            self.assertIn(kept, out)
        self.assertEqual(out.count("<rect"), 2)
        self.assertEqual(clean_svg(out), out)

    def test_the_example_is_a_resource_of_the_bundle_and_of_the_wheel(self):
        self.assertIn("drawing_example.svg", build_app.RESOURCES)
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("assets/*.svg", data["tool"]["setuptools"]["package-data"]["butler_ambient"])

    def test_the_playbook_teaches_the_drawing_card_with_the_rules_the_sanitizer_enforces(self):
        text = templates_module.markdown()
        self.assertIn(self.source().rstrip(), text)
        for rule in ("viewBox", "Card refused", "marker-end", "url(#id)", "Ширину и высоту корня не задавай", "#0b1410"):
            self.assertIn(rule, text)
        listed = re.search(r"Разрешены только: `([^`]+)`", text).group(1).split()
        self.assertEqual(set(listed), set(ELEMENTS))                       # the playbook lists exactly what the sanitizer allows

    def test_the_palette_in_the_rules_is_the_one_the_templates_use(self):
        rules = templates_module.DRAWING_RULES
        palette = re.search(r"Палитра[^\n]*", rules).group(0)
        flowchart = next(item["mermaid"] for item in templates_module.load() if item["id"] == "architecture")
        for color in re.findall(r"#[0-9a-f]{6}", palette):
            if color != "#0b1410":                                         # the window's own background
                self.assertIn(color, flowchart)


class PlaybookTests(unittest.TestCase):
    def test_the_playbook_holds_every_template_verbatim_and_says_which_do_not_work(self):
        text = templates_module.markdown()
        self.assertEqual(templates_module.missing(text), [])
        for item in templates_module.load():
            self.assertIn(f"## {item['id']}\n", text)
        for rule in ("architecture-beta", "replaces", "в кавычках", "8000"):
            self.assertIn(rule, text)

    def test_a_changed_or_missing_template_is_reported(self):
        items = templates_module.load()
        text = templates_module.markdown(items)
        self.assertEqual(templates_module.missing(text.replace("subgraph You[Вы]", "subgraph You[Мы]")), ["architecture"])
        self.assertEqual(templates_module.missing(""), [item["id"] for item in items] + ["drawing-example"])
        changed = text.replace('<text x="130" y="145"', '<text x="131" y="145"')
        self.assertEqual(templates_module.missing(changed), ["drawing-example"])

    def test_check_command_compares_an_installed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            good, bad = Path(folder, "good.md"), Path(folder, "bad.md")
            good.write_text(templates_module.markdown(), encoding="utf-8")
            bad.write_text(templates_module.markdown().replace("```mermaid\nsequenceDiagram", "```mermaid\nsequenceDiagram\n  %% edited", 1), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(templates_module.main(["--check", str(good)]), 0)
                self.assertEqual(templates_module.main(["--check", str(bad)]), 1)
            self.assertIn("missing or changed: sequence", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
