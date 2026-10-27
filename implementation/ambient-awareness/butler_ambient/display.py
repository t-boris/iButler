"""The conversational display (feature butler-conversational-display, DEC-002, DEC-040…042).

Butler shows material that is easier to see than to hear: a list, links, an image, an architecture
diagram, a prepared video, a letter with a suggested reply. The material is a structured card
(JSON) produced by the butler agent through the ``butler_display`` MCP tool, by the visual module
when a requested image is ready, or by the owner's own panel actions. The panel renders the
cards and brings its window forward when a new one appears (REQ-005).

What this module guards:
- A card is validated against a bounded schema; media paths are accepted only from the places
  Butler already owns or the owner already chose (generated images, visual roots,
  ~/.openclaw/media), never arbitrary files.
- An email card carries only a message key when it arrives; sender, subject, date, summary, body
  and the stored draft are read here from the local mail store (REQ-002: bodies never travel).
- History is this conversation's only: in memory, at most HISTORY cards, cleared when the owner
  session really closes (pause, consent revoked, settings change, enrollment, stop) or the service
  restarts; never persisted (REQ-011). A brief loss of the lease does not clear it (DEC-047).
- Visibility (REQ-010, lifted by DEC-047 and DEC-048): there is no privacy gate on the display. A valid card is
  shown, and stays visible, whatever the state of the private dialogue session or the recording gate.
- The only action with an external effect opens a prefilled reply in Mail.app; it never sends,
  never grants approval, and a failure is reported as a failure (REQ-008, REQ-009).
- A card may replace an earlier one (``replaces``): the same card, same place in the history, new content and a
  higher ``rev``. One evolving diagram is one row, not a pile; the window only comes forward when the owner was
  not already looking at that card.
- Card buttons (0.9.0) are a closed set: ``reply`` posts the button's own label to the owner's panel chat,
  ``link`` opens an http/https/mailto address, ``reveal`` shows an existing file or folder under the home
  directory in Finder. The panel only ever names a button by id; the address, the path and the reply stay
  here, so a button can do nothing its card did not declare. No button approves, sends, deletes or runs a
  command, and none of them changes what ``butler-core approve`` requires.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from . import processes as subprocess
from .svg_clean import SVG_MAX, SvgRefused, clean_svg

KINDS = ("text", "image", "diagram", "svg", "video", "email")
BUTTON_KINDS = ("reply", "link", "reveal")
BUTTON_FIELDS = {"reply": {"label", "kind"}, "link": {"label", "kind", "url"}, "reveal": {"label", "kind", "path"}}
HISTORY = 40
TITLE_MAX, MARKDOWN_MAX, MERMAID_MAX, LINK_MAX, LABEL_MAX, BUTTON_MAX = 120, 8000, 8000, 12, 80, 6
CONTEXT_EXCERPT = 600
CAPTION_MAX = 2000          # an image or diagram card may carry a short Markdown caption under the picture
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}
IMAGE_LIMIT, VIDEO_LIMIT = 20_000_000, 2_000_000_000
LINK_SCHEMES = {"http", "https", "mailto"}
MEDIA_ROOT = Path.home() / ".openclaw/media"
HOME = Path.home()
MAIL_TIMEOUT = 60
OPEN = "/usr/bin/open"      # a validated address starts with its scheme and a path with «/»: neither can be read as an option
OPEN_TIMEOUT = 15
OPEN_REPLY = "open-reply-in-mail"
ACTIONS = {"email": [{"id": OPEN_REPLY, "label": "Открыть ответ в Mail.app"}]}
KIND_TITLES = {"text": "Текст", "image": "Изображение", "diagram": "Схема", "svg": "Рисунок", "video": "Видео", "email": "Письмо"}
STATUS_OPENED = "Черновик ответа открыт в Mail.app — не отправлен. Отправка только после вашего подтверждения."
STATUS_FAILED = "Не удалось открыть ответ в Mail.app: {reason}. Письмо не отправлено."
STATUS_REPLY_SENT = "Ответ отправлен: «{label}»."
STATUS_REPLY_FAILED = "Ответ не отправлен: {reason}."
STATUS_REPLY_AGAIN = "На эту карточку уже ответили."
STATUS_LINK_OPENED = "Ссылка открыта: {hint}."
STATUS_REVEALED = "Показано в Finder."
STATUS_OPEN_FAILED = "Не удалось открыть: система не выполнила команду."

# The MCP tool's input schema: the card as the agent sends it. Email cards carry the key only.
CARD_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "markdown": {"type": "string", "maxLength": MARKDOWN_MAX, "description": "text card: Markdown with lists and links; image, diagram or svg card: an optional caption of up to 2000 characters"},
        "links": {"type": "array", "maxItems": LINK_MAX, "items": {"type": "object", "additionalProperties": False,
                  "required": ["label", "url"], "properties": {"label": {"type": "string", "maxLength": LABEL_MAX},
                                                               "url": {"type": "string", "maxLength": 2000}}}},
        "image": {"type": "string", "maxLength": 1000, "description": "image card: path of an image Butler already owns"},
        "mermaid": {"type": "string", "maxLength": MERMAID_MAX, "description": "diagram card: Mermaid source"},
        "svg": {"type": "string", "maxLength": SVG_MAX, "description": "svg card: your own drawing as SVG with a viewBox; only shapes, paths, text, gradients and markers survive "
                                                                      "(no scripts, styles, links, images or other files)"},
        "video": {"type": "string", "maxLength": 1000, "description": "video card: path of a prepared video under ~/.openclaw/media"},
        "key": {"type": "string", "maxLength": 300, "description": "email card: the message key from butler-mail; content is read locally"},
        "replaces": {"type": "string", "maxLength": 64, "description": "id of a card shown earlier in this conversation: that card becomes this one "
                                                                        "(a live canvas — redraw one diagram as it evolves instead of adding cards)"},
        "buttons": {"type": "array", "maxItems": BUTTON_MAX,
                    "description": "up to 6 buttons under a text, image, diagram or video card: reply (the label is sent to the chat as the "
                                   "owner's answer), link (url), reveal (path of an existing file or folder under the home directory)",
                    "items": {"type": "object", "additionalProperties": False, "required": ["label", "kind"],
                              "properties": {"label": {"type": "string", "maxLength": LABEL_MAX},
                                             "kind": {"type": "string", "enum": list(BUTTON_KINDS)},
                                             "url": {"type": "string", "maxLength": 2000},
                                             "path": {"type": "string", "maxLength": 1000}}}},
    },
}


class CardRefused(ValueError):
    """A malformed card. The reason names the field or the rule, never the content, so the agent can correct the card."""


class Display:
    def __init__(self, service, *, run=subprocess.run):
        self.service, self.run = service, run
        self.lock = threading.Lock()
        self.history = deque(maxlen=HISTORY)
        self.current = None
        self.raise_serial = 0
        self.cleared_reason = None
        self.counts = {}
        config = getattr(service, "config", None)
        workspace = Path(config.get("butler_workspace", Path.home() / ".openclaw/workspace-butler")) if isinstance(config, dict) \
            else Path.home() / ".openclaw/workspace-butler"
        self.mail_tool = workspace / "bin/butler-mail"

    # -- validation -----------------------------------------------------------------------

    def _media_roots(self):
        roots = [MEDIA_ROOT, Path(self.service.store.root) / "images"]
        visual = getattr(self.service, "visual", None)
        if visual is not None:
            try:
                roots.extend(Path(r) for r in visual.roots())
            except Exception:
                pass
        return roots

    def _media_allowed(self, value, suffixes, limit):
        """A file Butler already owns or the owner already chose: no symlinks, no hidden parts, a bounded size."""
        try:
            path = Path(str(value)).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in suffixes or path.stat().st_size > limit:
            return None
        for root in self._media_roots():
            try:
                root = root.resolve()
            except OSError:
                continue
            if path == root or root in path.parents:
                if any(part.startswith(".") for part in path.relative_to(root).parts):
                    return None
                return str(path)
        return None

    @staticmethod
    def _text(value, limit, name, *, required=False):
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise CardRefused(f"invalid {name}")
        return value.strip()

    def _caption(self, card, clean):
        caption = self._text(card.get("markdown"), CAPTION_MAX, "caption")
        if caption is not None:
            clean["markdown"] = caption

    def _reveal_path(self, value):
        """An existing file or folder under the home directory, no hidden part, symlinks resolved before the check."""
        try:
            path = Path(str(value)).expanduser().resolve(strict=True)
            home = HOME.resolve()
        except (OSError, RuntimeError, ValueError):
            raise CardRefused("button path not found") from None
        if path != home and home not in path.parents:
            raise CardRefused("button path is outside the home folder")
        if any(part.startswith(".") for part in path.relative_to(home).parts):
            raise CardRefused("button path is hidden")
        return str(path), "~" + str(path)[len(str(home)):]

    def _buttons(self, value):
        """The card's buttons, each kind with exactly its own fields; labels are unique within the card."""
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > BUTTON_MAX:
            raise CardRefused("invalid buttons")
        buttons, labels = [], set()
        for item in value:
            if not isinstance(item, dict) or item.get("kind") not in BUTTON_KINDS:
                raise CardRefused("invalid button kind")
            kind = item["kind"]
            if set(item) != BUTTON_FIELDS[kind]:
                raise CardRefused(f"a {kind} button takes exactly: " + ", ".join(sorted(BUTTON_FIELDS[kind])))
            label = self._text(item["label"], LABEL_MAX, "button label", required=True)
            if label in labels:
                raise CardRefused("duplicate button label")
            labels.add(label)
            button = {"label": label, "kind": kind}
            if kind == "link":
                url = self._text(item["url"], 2000, "button url", required=True)
                parts = urlsplit(url)
                if parts.scheme.lower() not in LINK_SCHEMES or re.search(r"\s", url) or \
                        (parts.scheme.lower() != "mailto" and not parts.hostname):
                    raise CardRefused("button url not allowed")
                button.update(target=url, hint=parts.path if parts.scheme.lower() == "mailto" else parts.hostname)
            elif kind == "reveal":
                button["target"], button["hint"] = self._reveal_path(self._text(item["path"], 1000, "button path", required=True))
            buttons.append(button)
        return buttons

    def validate(self, card):
        """A clean copy of the card or ValueError with a reason that names the field."""
        if not isinstance(card, dict):
            raise CardRefused("card must be an object")
        unknown = set(card) - set(CARD_SCHEMA["properties"])
        if unknown:
            raise CardRefused("unknown card fields: " + ", ".join(sorted(unknown)))
        kind = card.get("kind")
        if kind not in KINDS:
            raise CardRefused("invalid kind")
        clean = {"kind": kind, "title": self._text(card.get("title"), TITLE_MAX, "title") or KIND_TITLES[kind]}
        if card.get("replaces") is not None:
            clean["replaces"] = self._text(card.get("replaces"), 64, "replaces")
        if kind == "text":
            clean["markdown"] = self._text(card.get("markdown"), MARKDOWN_MAX, "markdown", required=True)
            links = card.get("links") or []
            if not isinstance(links, list) or len(links) > LINK_MAX:
                raise CardRefused("invalid links")
            clean["links"] = []
            for link in links:
                if not isinstance(link, dict) or set(link) != {"label", "url"}:
                    raise CardRefused("invalid link")
                url = self._text(link.get("url"), 2000, "link url", required=True)
                if urlsplit(url).scheme.lower() not in LINK_SCHEMES or re.search(r"\s", url):
                    raise CardRefused("link scheme not allowed")
                clean["links"].append({"label": self._text(link.get("label"), LABEL_MAX, "link label", required=True), "url": url})
        elif kind == "image":
            path = self._media_allowed(self._text(card.get("image"), 1000, "image", required=True), IMAGE_SUFFIXES, IMAGE_LIMIT)
            if path is None:
                raise CardRefused("image not available to Butler")
            clean["image"] = path
            self._caption(card, clean)
        elif kind == "diagram":
            clean["mermaid"] = self._text(card.get("mermaid"), MERMAID_MAX, "mermaid", required=True)
            self._caption(card, clean)
        elif kind == "svg":
            try:
                clean["svg"] = clean_svg(card.get("svg"))          # type, blankness and length are checked there, with their own reasons
            except SvgRefused as refusal:
                raise CardRefused(str(refusal)) from None
            self._caption(card, clean)
        elif kind == "video":
            path = self._media_allowed(self._text(card.get("video"), 1000, "video", required=True), VIDEO_SUFFIXES, VIDEO_LIMIT)
            if path is None:
                raise CardRefused("video not available to Butler")
            clean["video"] = path
        else:
            clean["key"] = self._text(card.get("key"), 300, "key", required=True)
        if kind == "email":
            if card.get("buttons") is not None:
                raise CardRefused("an email card has its own reply action and takes no buttons")
        else:
            clean["buttons"] = self._buttons(card.get("buttons"))
        for field in ("markdown", "links", "image", "mermaid", "svg", "video", "key"):
            if field in card and field not in clean:
                raise CardRefused(f"{field} does not belong to a {kind} card")
        return clean

    # -- presenting -----------------------------------------------------------------------

    def present(self, card, *, origin="agent"):
        """Validate, add to this conversation's history (or replace an earlier card), make it current and ask the panel to come forward."""
        clean = self.validate(card)
        replaces = clean.pop("replaces", None)
        if clean["kind"] == "email":
            clean.update(self.email_card(clean.pop("key")))
            clean["actions"] = [dict(action) for action in ACTIONS["email"]]
        else:
            clean["actions"] = [{"id": f"b{index}", **button} for index, button in enumerate(clean.pop("buttons"))]
        now = self.service.clock()
        with self.lock:
            earlier = None
            if replaces is not None:
                earlier = next((c for c in self.history if c["id"] == replaces), None)
                if earlier is None:
                    raise CardRefused("replaces: no such card")
            if earlier is not None:
                # Same card, same place: the content, buttons and status start over; the owner was looking at it, so no raise.
                keep = {"id": earlier["id"], "at": earlier["at"], "origin": earlier["origin"], "rev": earlier.get("rev", 0) + 1}
                quiet = self.current == earlier["id"]
                earlier.clear()                                # also drops the old reply choice, status and action state
                earlier.update(clean, **keep, status=None, updated_at=now)
                shown = earlier
                self.counts["replaced"] = self.counts.get("replaced", 0) + 1
                if not quiet:
                    self.raise_serial += 1
            else:
                clean.update(id=uuid.uuid4().hex, at=now, origin=str(origin)[:40], status=None, rev=0)
                self.history.append(clean)
                shown = clean
                self.raise_serial += 1
            self.current = shown["id"]
            self.cleared_reason = None
            self.counts[shown["kind"]] = self.counts.get(shown["kind"], 0) + 1
        voice = getattr(self.service, "voice", None)
        if voice is not None and callable(getattr(voice, "_debug", None)):
            voice._debug("display", f"{shown['title']} · {KIND_TITLES[shown['kind']].lower()}")
        ensure = getattr(self.service, "ensure_panel", None)
        if callable(ensure):
            ensure()
        return {"id": shown["id"], "shown": True, "kind": shown["kind"], "title": shown["title"],
                **({"replaced": True} if earlier is not None else {})}

    def present_image(self, path, caption, *, origin="visual"):
        """A ready visual result (I-3): the image appears on the display and the window comes forward."""
        try:
            return self.present({"kind": "image", "image": str(path), "title": str(caption or "Изображение")[:TITLE_MAX]}, origin=origin)
        except ValueError:
            self.counts["image-rejected"] = self.counts.get("image-rejected", 0) + 1
            return None

    def email_card(self, key):
        """Sender, subject, date, account, summary, body and draft from the local mail store; nothing else reads them."""
        result = self._mail("card", key, "--json")
        if result is None:
            raise CardRefused("mail card unavailable")
        fields = {}
        for name in ("key", "sender", "subject", "date", "account", "summary", "body", "draft"):
            value = result.get(name)
            fields[name] = value if isinstance(value, str) else ""
        if not fields["subject"] and not fields["sender"]:
            raise CardRefused("mail card unavailable")
        return {"email": fields, "title": (fields["subject"] or "Письмо")[:TITLE_MAX]}

    def _mail(self, *arguments):
        if not self.mail_tool.is_file():
            return None
        try:
            done = self.run([str(self.mail_tool), *arguments], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=MAIL_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode:
            return None
        try:
            value = json.loads(done.stdout)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    # -- navigation and actions --------------------------------------------------------------

    def _card(self, card_id):
        for card in self.history:
            if card["id"] == card_id:
                return card
        raise ValueError("no such card")

    def select(self, card_id):
        """The owner returns to an earlier card: it becomes current again; nothing is generated, opened or sent (REQ-011)."""
        with self.lock:
            card = self._card(card_id)
            self.current = card["id"]
            self.counts["reopened"] = self.counts.get("reopened", 0) + 1
        return {"id": card["id"], "kind": card["kind"], "title": card["title"]}

    def action(self, card_id, action_id):
        """An explicit owner action on a card. The panel names the action by id; what it does is decided here."""
        with self.lock:
            card = self._card(card_id)
            entry = next((dict(a) for a in card.get("actions", []) if a["id"] == action_id), None)
        if entry is None:
            raise ValueError("action not available for this card")
        if entry["id"] == OPEN_REPLY:
            return self._open_reply(card)
        if entry["kind"] == "reply":
            return self._reply(card, entry)
        return self._open(card, entry)

    def _note(self, text):
        """What the panel's diagnostics may show of an action: never a label, address or path."""
        voice = getattr(self.service, "voice", None)
        if voice is not None and callable(getattr(voice, "_debug", None)):
            voice._debug("display", text)

    def _finish(self, card, status, count, *, locked=False):
        """Record the outcome on the card. ``locked`` tells the panel the card's replies are spent (it also reads this from the snapshot)."""
        with self.lock:
            card["status"] = status
            card["action_at"] = self.service.clock()
            self.counts[count] = self.counts.get(count, 0) + 1
        return {"status": status, **({"locked": True} if locked else {})}

    def _reply_context(self, card, chosen):
        """What the answering agent is told about the card, in front of the button's text (the thread shows only the text)."""
        parts = [f"Владелец нажал кнопку на карточке «{card['title']}» в панели Butler."]
        others = [a["label"] for a in card["actions"] if a.get("kind") == "reply" and a["id"] != chosen["id"]]
        if others:
            parts.append("Другие варианты на карточке: " + "; ".join(f"«{label}»" for label in others) + ".")
        if card["kind"] == "text":
            parts.append("Текст карточки: " + card["markdown"][:CONTEXT_EXCERPT])
        parts.append("Ответ владельца (текст кнопки):")
        return " ".join(parts)

    def _reply(self, card, entry):
        """The button's label becomes the owner's next message in the panel chat. One answer per card."""
        chat = getattr(self.service, "panel_chat", None)
        if chat is None:
            raise ValueError("written conversation unavailable")
        with self.lock:
            if card.get("reply_choice") is not None:
                return {"status": STATUS_REPLY_AGAIN, "locked": True}
            card["reply_choice"] = entry["id"]            # reserved before sending: a second click cannot send twice
        try:
            chat.send(entry["label"], context=self._reply_context(card, entry))
        except ValueError as error:
            with self.lock:
                card["reply_choice"] = None
            self._note("кнопка: ответ не отправлен")
            return self._finish(card, STATUS_REPLY_FAILED.format(reason=str(error)[:160]), "button-failed")
        self._note("кнопка: ответ отправлен")
        return self._finish(card, STATUS_REPLY_SENT.format(label=entry["label"]), "button-reply", locked=True)

    def _open(self, card, entry):
        """A link in the default browser or mail client, or a file shown in Finder. Nothing is sent, run or changed."""
        argv = [OPEN, entry["target"]] if entry["kind"] == "link" else [OPEN, "-R", entry["target"]]
        try:
            done = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=OPEN_TIMEOUT)
            opened = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            opened = False
        self._note(f"кнопка: {entry['kind']} " + ("выполнена" if opened else "не удалась"))
        if not opened:
            return self._finish(card, STATUS_OPEN_FAILED, "button-failed")
        status = STATUS_LINK_OPENED.format(hint=entry["hint"]) if entry["kind"] == "link" else STATUS_REVEALED
        return self._finish(card, status, "button-" + entry["kind"])

    def _open_reply(self, card):
        email = card.get("email") or {}
        result = self._mail("open-reply", email.get("key", ""), "--json", *(["--text", email["draft"]] if email.get("draft") else []))
        if result is not None and result.get("opened") is True:
            status = STATUS_OPENED
            self.counts["reply-opened"] = self.counts.get("reply-opened", 0) + 1
        else:
            reason = (result or {}).get("reason") if isinstance(result, dict) else None
            status = STATUS_FAILED.format(reason=str(reason or "Mail.app не ответила")[:160])
            self.counts["reply-open-failed"] = self.counts.get("reply-open-failed", 0) + 1
        with self.lock:
            card["status"] = status
            card["action_at"] = self.service.clock()
            if result is not None and result.get("outgoing_id") is not None:
                card["outgoing_id"] = str(result["outgoing_id"])[:40]
        self._note(status)
        return {"status": status}

    def clear(self, reason):
        """The conversation really ended (the reasons that also end hearing) or the service restarts: the display history goes with it."""
        with self.lock:
            had = len(self.history)
            self.history.clear()
            self.current = None
            self.cleared_reason = str(reason)[:40]
        if had:
            self.counts["cleared"] = self.counts.get("cleared", 0) + 1

    # -- what the panel sees -----------------------------------------------------------------

    @staticmethod
    def _public(action, card):
        """A button as the panel gets it: id, label, kind and a short hint (host or folder). Never the address, path or reply."""
        shown = {key: action[key] for key in ("id", "label", "kind", "hint") if key in action}
        if action.get("kind") == "reply":
            choice = card.get("reply_choice")
            shown.update(used=choice is not None, chosen=choice == action["id"])
        return shown

    def snapshot(self):
        """What the panel gets: every card, always. The owner asked that the display is never cleared, hidden or withheld for
        privacy reasons (DEC-047, DEC-048); the history ends only with a real close (``clear``)."""
        with self.lock:
            cards = []
            for card in self.history:
                copy = {k: v for k, v in card.items() if k not in {"outgoing_id", "reply_choice"}}
                copy["actions"] = [self._public(action, card) for action in card.get("actions", [])]
                for field in ("image", "video"):
                    path = copy.get(field)
                    if path is None:
                        continue
                    if not Path(path).is_file():
                        copy.pop(field)
                        copy["unavailable"] = "missing"
                cards.append(copy)
            return {"cards": cards, "current": self.current, "raise_serial": self.raise_serial, "hidden": None,
                    "cleared_reason": self.cleared_reason, "counts": dict(self.counts)}
