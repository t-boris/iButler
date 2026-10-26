"""Direct Telegram Bot API calls for what the gateway cannot send (DEC-063).

The gateway refuses local documents other than validated CSV or Markdown, so
a delivery with a file attachment goes to ``sendDocument`` here. The bot token
is resolved at send time through the account's configured reference, exactly
as the gateway resolves it, and is never stored, logged or returned.
"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .base import Ambiguous, NotDispatched, Rejected

OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
CAPTION_LIMIT = 1024
API = "https://api.telegram.org"


class TokenUnavailable(RuntimeError):
    """The account has no resolvable bot token; the message says where it was looked for, never a value."""


def bot_token(account, home=OPENCLAW_HOME):
    """Resolve the bot token of a Telegram account from openclaw.json; a {source: env, id} reference reads .env."""
    try:
        config = json.loads((home / "openclaw.json").read_text())
    except (OSError, ValueError) as error:
        raise TokenUnavailable(f"openclaw.json unreadable: {type(error).__name__}") from None
    entry = config.get("channels", {}).get("telegram", {}).get("accounts", {}).get(account, {})
    reference = entry.get("botToken") or entry.get("token")
    if isinstance(reference, str) and reference:
        return reference
    if not isinstance(reference, dict) or reference.get("source") != "env" or not reference.get("id"):
        raise TokenUnavailable(f"account {account!r} has no token reference")
    name = str(reference["id"])
    value = os.environ.get(name)
    if not value:
        value = _env_file_value(home / ".env", name)
    if not value:
        raise TokenUnavailable(f"{name} is not set in the environment or {home / '.env'}")
    return value


def _env_file_value(path, name):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


def _multipart(fields, file_field, path, name, mime):
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{name}\"\r\n"
             f"Content-Type: {mime}\r\n\r\n").encode()
    body += Path(path).read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def send_document(token, chat, path, *, name=None, mime=None, caption="", silent=False, timeout=60,
                  opener=urllib.request.urlopen):
    """Send a local file as a Telegram document; return the message id as the receipt.

    Failure classes follow the adapter contract: a connection that never opened
    is NotDispatched, a Telegram refusal is Rejected, anything else is Ambiguous."""
    path = Path(path)
    name = name or path.name
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    fields = {"chat_id": str(chat), "disable_notification": "true" if silent else "false"}
    if caption:
        fields["caption"] = caption[:CAPTION_LIMIT]
    body, content_type = _multipart(fields, "document", path, name, mime)
    request = urllib.request.Request(f"{API}/bot{token}/sendDocument", data=body, method="POST",
                                     headers={"Content-Type": content_type, "Content-Length": str(len(body))})
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        try:
            description = json.loads(error.read().decode("utf-8", "replace")).get("description", "")
        except (ValueError, OSError):
            description = ""
        if 400 <= error.code < 500 and error.code != 429:
            raise Rejected(f"telegram {error.code}: {description[:120]}") from None
        raise Ambiguous(f"telegram {error.code}") from None
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            raise NotDispatched(f"telegram unreachable: {type(reason).__name__}") from None
        raise Ambiguous(f"telegram request failed: {type(reason).__name__}") from None
    except (TimeoutError, socket.timeout, OSError) as error:
        raise Ambiguous(f"telegram request failed: {type(error).__name__}") from None
    if not payload.get("ok"):
        raise Ambiguous("telegram answered without ok")
    message_id = payload.get("result", {}).get("message_id")
    if message_id is None:
        raise Ambiguous("no message id in the telegram response")
    return str(message_id)
