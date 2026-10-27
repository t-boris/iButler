"""A drawing the agent made, made safe to show (ambient 0.9.0, display card ``svg``).

The source is never shown as received. It is parsed as XML, checked element by element and attribute by attribute
against a short allow list, and written out again from the parsed tree. Anything outside the list is refused with a
reason that names the rule (at most an element or attribute name), never the drawing's text, so the agent can fix it.

What the allow list can do is draw: shapes, paths, text, gradients and arrowheads. It cannot run or fetch anything:
no scripts, styles, links, images, ``use``, animation, event handlers, references to other files, DOCTYPE or entities.
The panel adds two independent layers (a page that runs no JavaScript and a Content-Security-Policy that allows
nothing), so a mistake here is not a hole.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
SVG_MAX = 60_000                      # characters of source
MAX_ELEMENTS, MAX_DEPTH = 600, 12
MAX_VALUE, MAX_TEXT = 6_000, 600      # one attribute value (path data is long), one element's text
CANVAS_MAX = 10_000                   # largest coordinate or size accepted in the viewBox

ELEMENTS = frozenset("svg g defs linearGradient radialGradient stop rect circle ellipse line polyline polygon path text tspan title desc marker".split())
TEXTUAL = frozenset(("text", "tspan", "title", "desc"))
ATTRIBUTES = frozenset((
    "id version xmlns viewBox preserveAspectRatio width height x y dx dy cx cy r rx ry fx fy x1 y1 x2 y2 points d transform "
    "fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-linecap stroke-linejoin stroke-dasharray "
    "stroke-dashoffset stroke-miterlimit opacity font-family font-size font-weight font-style text-anchor dominant-baseline "
    "letter-spacing text-decoration offset stop-color stop-opacity gradientUnits gradientTransform markerWidth markerHeight "
    "markerUnits refX refY orient marker-start marker-mid marker-end role aria-label").split())

DTD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.I)
LOCAL_REFERENCE = re.compile(r"url\(\s*#[A-Za-z][\w-]*\s*\)")      # the only url() allowed: a gradient or marker of this drawing
REFERENCE_OPEN = re.compile(r"url\(", re.I)
REFERENCE_WHOLE = re.compile(r"url\([^)]*\)", re.I)
SCHEME = re.compile(r"(?:javascript|data|vbscript|file|http|https)\s*:", re.I)
IDENTIFIER = re.compile(r"[A-Za-z][\w-]{0,40}")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)"
VIEW_BOX = re.compile(rf"\s*({NUMBER})[\s,]+({NUMBER})[\s,]+({NUMBER})[\s,]+({NUMBER})\s*")


class SvgRefused(ValueError):
    """The drawing cannot be shown. The reason names the rule, not the content."""


def _name(raw):
    """An element or attribute name that is safe to quote back to the agent."""
    local = raw.rsplit("}", 1)[-1]
    return re.sub(r"[^A-Za-z0-9:_.-]", "?", local)[:30]


def _tag(element):
    if "}" not in element.tag:
        return element.tag
    namespace, local = element.tag[1:].split("}", 1)
    if namespace != SVG_NS:
        raise SvgRefused("svg: element from another namespace is not allowed")
    return local


def _canvas(root):
    match = VIEW_BOX.fullmatch(root.attrib.get("viewBox", ""))
    if match is None:
        raise SvgRefused("svg: the root needs a viewBox such as 0 0 800 450")
    x, y, width, height = (float(value) for value in match.groups())
    if width <= 0 or height <= 0 or max(abs(x), abs(y), width, height) > CANVAS_MAX:
        raise SvgRefused("svg: viewBox out of range")


def _safe_value(value):
    """No scheme that fetches or runs something, and every url() points at an element of this drawing."""
    if SCHEME.search(value):
        return False
    wholes = REFERENCE_WHOLE.findall(value)
    return len(wholes) == len(REFERENCE_OPEN.findall(value)) and all(LOCAL_REFERENCE.fullmatch(whole) for whole in wholes)


def _check_attributes(element, tag):
    for name, value in element.attrib.items():
        if "{" in name:
            raise SvgRefused("svg: namespaced attributes are not allowed")
        if name not in ATTRIBUTES:
            raise SvgRefused(f"svg: attribute not allowed: {_name(name)}")
        if len(value) > MAX_VALUE or CONTROL.search(value):
            raise SvgRefused(f"svg: invalid value of {_name(name)}")
        if not _safe_value(value):
            raise SvgRefused(f"svg: unsafe value of {_name(name)}")
        if name == "id" and not IDENTIFIER.fullmatch(value):
            raise SvgRefused("svg: invalid id")


def _check_text(element, tag, parent):
    """Text lives only inside text, tspan, title and desc; between other elements only whitespace may sit."""
    for part, allowed, holder in ((element.text, tag in TEXTUAL, tag), (element.tail, parent in TEXTUAL, parent)):
        if not part or not part.strip():
            continue
        if not allowed:
            raise SvgRefused(f"svg: text is not allowed inside <{_name(holder)}>")
        if len(part) > MAX_TEXT or CONTROL.search(part):
            raise SvgRefused("svg: text too long or has control characters")


def clean_svg(source):
    """The drawing as safe markup, or ``SvgRefused``. Idempotent: cleaning the result gives the same text."""
    if not isinstance(source, str) or not source.strip():
        raise SvgRefused("invalid svg")
    if len(source) > SVG_MAX:
        raise SvgRefused("svg: too long")
    text = source.strip()
    if DTD.search(text):
        raise SvgRefused("svg: DOCTYPE and entities are not allowed")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise SvgRefused("svg: not well-formed XML") from None
    if _tag(root) != "svg":
        raise SvgRefused("svg: the root must be <svg>")
    count, stack = 0, [(root, 1, None)]
    while stack:
        element, depth, parent = stack.pop()
        count += 1
        if count > MAX_ELEMENTS:
            raise SvgRefused("svg: too many elements")
        if depth > MAX_DEPTH:
            raise SvgRefused("svg: nested too deeply")
        tag = _tag(element)
        if tag not in ELEMENTS:
            raise SvgRefused(f"svg: element not allowed: <{_name(tag)}>")
        _check_attributes(element, tag)
        _check_text(element, tag, parent)
        element.tag = tag
        if parent is not None and tag == "svg":
            raise SvgRefused("svg: nested <svg> is not allowed")
        stack.extend((child, depth + 1, tag) for child in element)
    _canvas(root)
    # The page sizes the drawing itself; an absolute width or height from the agent would only fight that.
    rest = {name: value for name, value in root.attrib.items() if name not in ("xmlns", "width", "height")}
    root.attrib.clear()
    root.attrib["xmlns"] = SVG_NS
    root.attrib.update(rest)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)
