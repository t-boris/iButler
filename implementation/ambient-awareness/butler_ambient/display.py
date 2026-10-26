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
  session hard-closes or the service restarts; never persisted (REQ-011).
- Visibility follows the existing rules (REQ-010): cards are shown only while the private
  dialogue lease holds; image and video paths are withheld while recording is paused.
- The only action with an external effect opens a prefilled reply in Mail.app; it never sends,
  never grants approval, and a failure is reported as a failure (REQ-008, REQ-009).
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

KINDS = ("text", "image", "diagram", "video", "email")
HISTORY = 40
TITLE_MAX, MARKDOWN_MAX, MERMAID_MAX, LINK_MAX, LABEL_MAX = 120, 8000, 8000, 12, 80
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}
IMAGE_LIMIT, VIDEO_LIMIT = 20_000_000, 2_000_000_000
LINK_SCHEMES = {"http", "https", "mailto"}
MEDIA_ROOT = Path.home() / ".openclaw/media"
MAIL_TIMEOUT = 60
OPEN_REPLY = "open-reply-in-mail"
ACTIONS = {"email": [{"id": OPEN_REPLY, "label": "Открыть ответ в Mail.app"}]}
KIND_TITLES = {"text": "Текст", "image": "Изображение", "diagram": "Схема", "video": "Видео", "email": "Письмо"}
STATUS_OPENED = "Черновик ответа открыт в Mail.app — не отправлен. Отправка только после вашего подтверждения."
STATUS_FAILED = "Не удалось открыть ответ в Mail.app: {reason}. Письмо не отправлено."

# The MCP tool's input schema: the card as the agent sends it. Email cards carry the key only.
CARD_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "markdown": {"type": "string", "maxLength": MARKDOWN_MAX, "description": "text card: Markdown with lists and links"},
        "links": {"type": "array", "maxItems": LINK_MAX, "items": {"type": "object", "additionalProperties": False,
                  "required": ["label", "url"], "properties": {"label": {"type": "string", "maxLength": LABEL_MAX},
                                                               "url": {"type": "string", "maxLength": 2000}}}},
        "image": {"type": "string", "maxLength": 1000, "description": "image card: path of an image Butler already owns"},
        "mermaid": {"type": "string", "maxLength": MERMAID_MAX, "description": "diagram card: Mermaid source"},
        "video": {"type": "string", "maxLength": 1000, "description": "video card: path of a prepared video under ~/.openclaw/media"},
        "key": {"type": "string", "maxLength": 300, "description": "email card: the message key from butler-mail; content is read locally"},
    },
}


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
            raise ValueError(f"invalid {name}")
        return value.strip()

    def validate(self, card):
        """A clean copy of the card or ValueError with a reason that names the field."""
        if not isinstance(card, dict):
            raise ValueError("card must be an object")
        unknown = set(card) - set(CARD_SCHEMA["properties"])
        if unknown:
            raise ValueError("unknown card fields: " + ", ".join(sorted(unknown)))
        kind = card.get("kind")
        if kind not in KINDS:
            raise ValueError("invalid kind")
        clean = {"kind": kind, "title": self._text(card.get("title"), TITLE_MAX, "title") or KIND_TITLES[kind]}
        if kind == "text":
            clean["markdown"] = self._text(card.get("markdown"), MARKDOWN_MAX, "markdown", required=True)
            links = card.get("links") or []
            if not isinstance(links, list) or len(links) > LINK_MAX:
                raise ValueError("invalid links")
            clean["links"] = []
            for link in links:
                if not isinstance(link, dict) or set(link) != {"label", "url"}:
                    raise ValueError("invalid link")
                url = self._text(link.get("url"), 2000, "link url", required=True)
                if urlsplit(url).scheme.lower() not in LINK_SCHEMES or re.search(r"\s", url):
                    raise ValueError("link scheme not allowed")
                clean["links"].append({"label": self._text(link.get("label"), LABEL_MAX, "link label", required=True), "url": url})
        elif kind == "image":
            path = self._media_allowed(self._text(card.get("image"), 1000, "image", required=True), IMAGE_SUFFIXES, IMAGE_LIMIT)
            if path is None:
                raise ValueError("image not available to Butler")
            clean["image"] = path
        elif kind == "diagram":
            clean["mermaid"] = self._text(card.get("mermaid"), MERMAID_MAX, "mermaid", required=True)
        elif kind == "video":
            path = self._media_allowed(self._text(card.get("video"), 1000, "video", required=True), VIDEO_SUFFIXES, VIDEO_LIMIT)
            if path is None:
                raise ValueError("video not available to Butler")
            clean["video"] = path
        else:
            clean["key"] = self._text(card.get("key"), 300, "key", required=True)
        for field in ("markdown", "links", "image", "mermaid", "video", "key"):
            if field in card and field not in clean:
                raise ValueError(f"{field} does not belong to a {kind} card")
        return clean

    # -- presenting -----------------------------------------------------------------------

    def present(self, card, *, origin="agent"):
        """Validate, add to this conversation's history, make it current and ask the panel to come forward."""
        clean = self.validate(card)
        if clean["kind"] == "email":
            clean.update(self.email_card(clean.pop("key")))
            clean["actions"] = list(ACTIONS["email"])
        else:
            clean["actions"] = []
        clean.update(id=uuid.uuid4().hex, at=self.service.clock(), origin=str(origin)[:40], status=None)
        with self.lock:
            self.history.append(clean)
            self.current = clean["id"]
            self.raise_serial += 1
            self.cleared_reason = None
            self.counts[clean["kind"]] = self.counts.get(clean["kind"], 0) + 1
        voice = getattr(self.service, "voice", None)
        if voice is not None and callable(getattr(voice, "_debug", None)):
            voice._debug("display", f"{clean['title']} · {KIND_TITLES[clean['kind']].lower()}")
        ensure = getattr(self.service, "ensure_panel", None)
        if callable(ensure):
            ensure()
        return {"id": clean["id"], "shown": True, "kind": clean["kind"], "title": clean["title"]}

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
            raise ValueError("mail card unavailable")
        fields = {}
        for name in ("key", "sender", "subject", "date", "account", "summary", "body", "draft"):
            value = result.get(name)
            fields[name] = value if isinstance(value, str) else ""
        if not fields["subject"] and not fields["sender"]:
            raise ValueError("mail card unavailable")
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
        """An explicit owner action on a card. Opening a reply never sends and never approves anything."""
        with self.lock:
            card = self._card(card_id)
            allowed = {a["id"] for a in card.get("actions", [])}
        if action_id not in allowed:
            raise ValueError("action not available for this card")
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
        voice = getattr(self.service, "voice", None)
        if voice is not None and callable(getattr(voice, "_debug", None)):
            voice._debug("display", status)
        return {"status": status}

    def clear(self, reason):
        """The conversation ended (session hard close) or the service restarts: the display history goes with it."""
        with self.lock:
            had = len(self.history)
            self.history.clear()
            self.current = None
            self.cleared_reason = str(reason)[:40]
        if had:
            self.counts["cleared"] = self.counts.get("cleared", 0) + 1

    # -- what the panel sees -----------------------------------------------------------------

    def snapshot(self, *, dialogue_allowed, recording_allowed):
        with self.lock:
            if not dialogue_allowed:
                return {"cards": [], "current": None, "raise_serial": self.raise_serial, "hidden": "private",
                        "cleared_reason": self.cleared_reason, "counts": dict(self.counts)}
            cards = []
            for card in self.history:
                copy = {k: v for k, v in card.items() if k != "outgoing_id"}
                for field in ("image", "video"):
                    path = copy.get(field)
                    if path is None:
                        continue
                    if not recording_allowed:
                        copy.pop(field)
                        copy["unavailable"] = "paused"
                    elif not Path(path).is_file():
                        copy.pop(field)
                        copy["unavailable"] = "missing"
                cards.append(copy)
            return {"cards": cards, "current": self.current, "raise_serial": self.raise_serial, "hidden": None,
                    "cleared_reason": self.cleared_reason, "counts": dict(self.counts)}
