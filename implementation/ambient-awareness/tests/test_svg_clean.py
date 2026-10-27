"""The drawing sanitizer: only drawing survives, everything else is refused with a reason that names the rule."""
import re
import unittest
import xml.etree.ElementTree as ET

from butler_ambient.svg_clean import ATTRIBUTES, CANVAS_MAX, ELEMENTS, MAX_DEPTH, MAX_ELEMENTS, MAX_VALUE, SVG_MAX, SVG_NS, SvgRefused, clean_svg

BOX = 'viewBox="0 0 400 200"'


def drawing(inner="", attrs=BOX):
    return f'<svg xmlns="{SVG_NS}" {attrs}>{inner}</svg>'


class AcceptTests(unittest.TestCase):
    def test_a_plain_drawing_comes_back_normalised(self):
        out = clean_svg(drawing('<rect x="1" y="2" width="30" height="40" fill="#143d2a"/><circle cx="5" cy="5" r="3"/>'))
        self.assertEqual(out, f'<svg xmlns="{SVG_NS}" viewBox="0 0 400 200"><rect x="1" y="2" width="30" height="40" fill="#143d2a" /><circle cx="5" cy="5" r="3" /></svg>')

    def test_missing_namespace_prefixed_namespace_and_xml_declaration_are_all_accepted_and_unified(self):
        plain = clean_svg(f'<svg {BOX}><rect width="1" height="1"/></svg>')
        prefixed = clean_svg(f'<s:svg xmlns:s="{SVG_NS}" {BOX}><s:rect width="1" height="1"/></s:svg>')
        declared = clean_svg(f'<?xml version="1.0" encoding="UTF-8"?>\n  {drawing("<rect width=\"1\" height=\"1\"/>")}\n')
        self.assertEqual(plain, prefixed)
        self.assertEqual(plain, declared)
        self.assertEqual(plain.count("xmlns"), 1)

    def test_any_xml_declaration_is_accepted_and_the_text_is_not_misread(self):
        for encoding in ("UTF-8", "utf-8", "ISO-8859-1", "windows-1251", "UTF-16"):
            with self.subTest(encoding=encoding):
                out = clean_svg(f'<?xml version="1.0" encoding="{encoding}" standalone="no"?>{drawing("<text>Привет</text>")}')
                self.assertIn("<text>Привет</text>", out)
                self.assertNotIn("<?", out)

    def test_gradients_markers_text_and_local_references_survive(self):
        out = clean_svg(drawing(
            '<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#143d2a"/><stop offset="1" stop-color="#7fbf92"/></linearGradient>'
            '<marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0 0L6 3L0 6z"/></marker></defs>'
            '<rect width="100" height="50" fill="url(#g)"/><line x1="0" y1="0" x2="50" y2="50" stroke="#fff" marker-end="url( #arrow )"/>'
            '<text x="5" y="20" font-size="14" text-anchor="middle">Привет &amp; <tspan font-weight="bold">мир</tspan> &lt;ok&gt;</text>'
            '<title>Схема</title><desc>Описание</desc>'))
        for kept in ('fill="url(#g)"', 'marker-end="url( #arrow )"', "Привет &amp; ", "&lt;ok&gt;", "<linearGradient", "<marker", "<tspan", "<title>Схема</title>"):
            self.assertIn(kept, out)

    def test_comments_processing_instructions_and_cdata_do_not_survive_as_markup(self):
        out = clean_svg(drawing('<!-- note --><?pi data?><text>a<![CDATA[<b>&]]>b</text>'))
        self.assertNotIn("<!--", out)
        self.assertNotIn("<?", out)
        self.assertNotIn("CDATA", out)
        self.assertIn("a&lt;b&gt;&amp;b", out)

    def test_an_absolute_size_on_the_root_is_dropped_so_the_page_decides(self):
        out = clean_svg(f'<svg width="4000" height="2000" {BOX}><rect width="1" height="1"/></svg>')
        self.assertNotRegex(out, r'<svg[^>]* (?:width|height)=')

    def test_cleaning_is_idempotent(self):
        once = clean_svg(drawing('<g transform="translate(2 3)"><rect width="1" height="1" fill="url(#g)"/></g><text>я</text>'))
        self.assertEqual(clean_svg(once), once)

    def test_the_limits_are_inclusive(self):
        clean_svg(drawing("<rect/>" * (MAX_ELEMENTS - 1)))
        clean_svg(drawing("<g>" * (MAX_DEPTH - 1) + "</g>" * (MAX_DEPTH - 1)))
        clean_svg(drawing(f'<path d="{"M0 0 " * ((MAX_VALUE - 10) // 5)}"/>'))
        clean_svg(f'<svg viewBox="-{CANVAS_MAX} -{CANVAS_MAX} {CANVAS_MAX} {CANVAS_MAX}"/>')


class RefuseTests(unittest.TestCase):
    def refuses(self, source, reason):
        with self.assertRaises(SvgRefused) as caught:
            clean_svg(source)
        self.assertEqual(str(caught.exception), reason)

    def test_not_a_drawing_at_all(self):
        for value in (None, 5, [], "", "   ", "\n"):
            self.refuses(value, "invalid svg")
        self.refuses(drawing("<rect/>" * 9000), "svg: too long")
        self.refuses("x" * (SVG_MAX + 1), "svg: too long")

    def test_not_well_formed(self):
        for source in ("<svg", "plain text", f"<svg {BOX}><rect></svg>", f"<svg {BOX}>&nbsp;</svg>", f"<svg {BOX}/><svg {BOX}/>", f"<svg {BOX} a='1' a='2'/>"):
            with self.subTest(source=source[:30]):
                self.refuses(source, "svg: not well-formed XML")

    def test_doctype_and_entities_are_refused_before_parsing(self):
        bomb = '<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>' + drawing("<text>&b;</text>")
        xxe = '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + drawing("<text>&x;</text>")
        lower = "<!doctype svg>" + drawing()
        spaced = "<! ENTITY a 'b'>" + drawing()
        for source in (bomb, xxe, lower, spaced):
            self.refuses(source, "svg: DOCTYPE and entities are not allowed")

    def test_the_root_must_be_svg(self):
        for source in ("<html/>", "<g/>", f'<svg:g xmlns:svg="{SVG_NS}"/>'):
            self.refuses(source, "svg: the root must be <svg>")

    def test_elements_that_can_run_fetch_or_style_are_refused_by_name(self):
        for name in ("script", "style", "foreignObject", "image", "use", "a", "animate", "set", "animateTransform", "animateMotion", "filter", "pattern",
                     "clipPath", "mask", "symbol", "switch", "iframe", "object", "embed", "link", "meta", "video", "audio", "canvas", "view", "cursor", "SCRIPT", "Script"):
            with self.subTest(element=name):
                self.refuses(drawing(f"<{name}/>"), f"svg: element not allowed: <{name}>")

    def test_a_script_hidden_inside_allowed_markup_is_still_found(self):
        self.refuses(drawing("<g><g><text><tspan><script>alert(1)</script></tspan></text></g></g>"), "svg: element not allowed: <script>")

    def test_an_element_of_another_namespace_is_refused(self):
        self.refuses(drawing('<div xmlns="http://www.w3.org/1999/xhtml">x</div>'), "svg: element from another namespace is not allowed")
        self.refuses(f'<svg xmlns="http://example.com/ns" {BOX}/>', "svg: element from another namespace is not allowed")

    def test_attributes_outside_the_list_are_refused_by_name(self):
        for name in ("onload", "onclick", "onmouseover", "onerror", "style", "class", "href", "src", "filter", "clip-path", "mask", "cursor", "data-x", "tabindex"):
            with self.subTest(attribute=name):
                self.refuses(drawing(f'<rect {name}="x"/>'), f"svg: attribute not allowed: {name}")

    def test_namespaced_attributes_are_refused(self):
        self.refuses(drawing('<rect xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="http://evil/x"/>'), "svg: namespaced attributes are not allowed")
        self.refuses(drawing('<text xml:space="preserve">a</text>'), "svg: namespaced attributes are not allowed")

    def test_values_that_fetch_or_run_are_refused(self):
        for value in ("url(http://evil/x.svg#a)", "url(//evil/x)", "url(data:image/svg+xml;base64,AAA)", "url(#a) url(//evil/x)", "url(#a", "url()", "url('#a')",
                      "URL(http://x)", "javascript:alert(1)", "JaVaScRiPt : alert(1)", "data:text/html,&lt;b&gt;", "vbscript:x", "file:///etc/passwd", "https://evil/x"):
            for attribute in ("fill", "aria-label"):
                with self.subTest(attribute=attribute, value=value):
                    self.refuses(drawing(f'<rect {attribute}="{value}"/>'), f"svg: unsafe value of {attribute}")

    def test_ids_must_be_plain_identifiers(self):
        for value in ("1a", "a b", "a" * 42, "", "#a", "a;b"):
            with self.subTest(value=value):
                self.refuses(drawing(f'<rect id="{value}"/>'), "svg: invalid id")

    def test_the_canvas_must_be_declared_and_reasonable(self):
        for attrs in ("", 'viewBox="0 0 0 10"', 'viewBox="0 0 10 -5"', 'viewBox="a b c d"', 'viewBox="0 0 10"', f'viewBox="0 0 {CANVAS_MAX + 1} 10"', f'viewBox="{CANVAS_MAX + 1} 0 10 10"'):
            with self.subTest(attrs=attrs):
                reason = "svg: the root needs a viewBox such as 0 0 800 450" if "a b" in attrs or attrs in ("", 'viewBox="0 0 10"') else "svg: viewBox out of range"
                self.refuses(f'<svg xmlns="{SVG_NS}" {attrs}/>', reason)

    def test_size_limits(self):
        self.refuses(drawing("<rect/>" * MAX_ELEMENTS), "svg: too many elements")
        self.refuses(drawing("<g>" * MAX_DEPTH + "</g>" * MAX_DEPTH), "svg: nested too deeply")
        self.refuses(drawing(f'<path d="{"M" * (MAX_VALUE + 1)}"/>'), "svg: invalid value of d")
        self.refuses(drawing("<g><svg/></g>"), "svg: nested <svg> is not allowed")

    def test_text_is_only_allowed_where_text_belongs(self):
        self.refuses(drawing("<g>stray</g>"), "svg: text is not allowed inside <g>")
        self.refuses(drawing("<g><rect/>stray</g>"), "svg: text is not allowed inside <g>")
        self.refuses(drawing("stray<rect/>"), "svg: text is not allowed inside <svg>")
        self.refuses(drawing("<text>" + "я" * 601 + "</text>"), "svg: text too long or has control characters")
        self.refuses(drawing("<text>a\x7fb</text>"), "svg: text too long or has control characters")
        self.refuses(drawing('<rect fill="a\x7fb"/>'), "svg: invalid value of fill")

    def test_the_reason_never_repeats_the_drawings_text(self):
        secret = "Секрет-12345"
        for source in (drawing(f'<script>{secret}</script>'), drawing(f'<rect fill="url(http://x/{secret})"/>'), drawing(f"<g>{secret}</g>"),
                       drawing(f'<rect aria-label="javascript:{secret}"/>'), drawing(f'<rect id="{secret}"/>'), f"<svg {secret}>"):
            with self.subTest(source=source[:25]):
                with self.assertRaises(SvgRefused) as caught:
                    clean_svg(source)
                self.assertNotIn(secret, str(caught.exception))

    def test_a_hostile_name_is_made_printable_and_short(self):
        with self.assertRaises(SvgRefused) as caught:
            clean_svg(drawing(f'<rect {"a" * 80}="1"/>'))
        self.assertLessEqual(len(str(caught.exception)), len("svg: attribute not allowed: ") + 30)


# Classic SVG injection payloads that must be refused outright.
REFUSED = [
    '<svg onload="alert(1)" viewBox="0 0 1 1"/>',
    f'<svg {BOX}><script>alert(1)</script></svg>',
    f'<svg {BOX}><script xlink:href="data:,alert(1)" xmlns:xlink="http://www.w3.org/1999/xlink"/></svg>',
    f'<svg {BOX}><rect onmouseover="alert(1)" width="9999" height="9999"/></svg>',
    f'<svg {BOX}><a xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
    f'<svg {BOX}><foreignObject><body xmlns="http://www.w3.org/1999/xhtml"><iframe src="javascript:alert(1)"/></body></foreignObject></svg>',
    f'<svg {BOX}><image href="http://evil/track.png"/></svg>',
    f'<svg {BOX}><use href="http://evil/x.svg#a"/></svg>',
    f'<svg {BOX}><style>@import url(http://evil/x.css);</style></svg>',
    f'<svg {BOX}><rect style="fill:url(http://evil/x)" width="1" height="1"/></svg>',
    f'<svg {BOX}><animate attributeName="href" values="javascript:alert(1)"/></svg>',
    f'<svg {BOX}><set attributeName="onmouseover" to="alert(1)"/></svg>',
    f'<svg {BOX}><rect fill="url(javascript:alert(1))"/></svg>',
    f'<svg {BOX}><math><mi/></math></svg>',
    f'<svg {BOX}><noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript></svg>',
    f'<svg {BOX} xmlns:evil="http://evil/ns"><evil:script/></svg>',
    '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://evil/x.dtd">' + drawing(),
    f'<svg {BOX}><rect width="1" height="1" class="x" id="a" onfocus="alert(1)" tabindex="0"/></svg>',
]
# Payloads whose dangerous part is only words: they may survive, but as escaped text, never as markup.
INERT = [
    f'<svg {BOX}><text><![CDATA[</text><script>alert(1)</script><text>]]></text></svg>',
    f'<svg {BOX}><title><![CDATA[</title><script>alert(1)</script>]]></title></svg>',
    f'<svg {BOX}><desc>&lt;script&gt;alert(1)&lt;/script&gt;</desc></svg>',
    f'<svg {BOX}><g><title>&lt;/title&gt;&lt;img src=x onerror=alert(1)&gt;</title></g></svg>',
    f'<svg {BOX}><text>&#60;script&#62;alert(1)&#60;/script&#62;</text></svg>',
    f'<svg {BOX}><rect id="a" /><rect aria-label="&lt;img src=x onerror=alert(1)&gt;"/></svg>',
    '<?xml-stylesheet href="http://evil/x.css"?>' + drawing(),
]


class CorpusTests(unittest.TestCase):
    def test_every_hostile_payload_is_refused(self):
        for source in REFUSED:
            with self.subTest(source=source[:70]):
                with self.assertRaises(SvgRefused):
                    clean_svg(source)

    def test_payloads_that_are_only_words_come_out_as_text_never_as_markup(self):
        for source in INERT:
            with self.subTest(source=source[:70]):
                out = clean_svg(source)
                root = ET.fromstring(out)                    # what comes out is well-formed and says only what the allow list says
                for element in root.iter():
                    self.assertIn(element.tag.rsplit("}", 1)[-1], ELEMENTS)
                    for attribute in element.attrib:
                        self.assertIn(attribute, ATTRIBUTES | {"xmlns"})
                self.assertEqual(out.count("<"), len(re.findall(r"<[A-Za-z/]", out)))                      # every "<" opens or closes a real tag
                self.assertEqual(len(re.findall(r"<[A-Za-z]", out)), sum(1 for _ in root.iter()))         # and there is a tag for each element, no more
                self.assertNotRegex(out, r"<(?:script|img|iframe|style|foreignObject)")
                self.assertEqual(out.count(f'xmlns="{SVG_NS}"'), 1)

    def test_whatever_comes_out_uses_only_allowed_names(self):
        out = clean_svg(drawing('<g transform="rotate(5)"><rect x="1" y="2" width="3" height="4" fill="#143d2a" stroke="#fff" opacity="0.5"/></g><text font-family="Menlo">я</text>'))
        for tag in re.findall(r"<([A-Za-z]+)", out):
            self.assertIn(tag, ELEMENTS)
        for attribute in re.findall(r'\s([\w-]+)="', out):
            self.assertIn(attribute, ATTRIBUTES | {"xmlns"})

    def test_the_allow_lists_contain_nothing_that_runs_fetches_or_restyles(self):
        for banned in ("script", "style", "foreignObject", "image", "use", "a", "animate", "set", "filter", "iframe"):
            self.assertNotIn(banned, ELEMENTS)
        for banned in ("href", "style", "class", "src", "filter", "mask", "clip-path", "cursor"):
            self.assertNotIn(banned, ATTRIBUTES)
        self.assertFalse([name for name in ATTRIBUTES if name.lower().startswith("on")])


if __name__ == "__main__":
    unittest.main()
