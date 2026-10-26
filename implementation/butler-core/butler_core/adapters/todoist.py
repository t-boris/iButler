"""Todoist as idempotent core operations (W4; inventory 3 and 10).

Every write goes through the Sync API, whose command ``uuid`` Todoist refuses
to execute twice. The uuid is derived from the operation id, so a retry after
an unknown outcome cannot create a second task, close twice or delete twice.
Reconciliation reads the task back: a create is found by its exact content in
the right project, close/reopen/update by the task's state, delete by its
absence, a comment by its text.

The API token is read from ``~/.openclaw/.env`` when a request is made and is
never stored, logged or returned (DEC-053).
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .base import DONE, NOT_DONE, UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected
from .telegram import TokenUnavailable, _env_file_value

API = "https://api.todoist.com/api/v1"
NAMESPACE = uuid.UUID("5a7c1d6e-3b2f-4e8a-9c0d-1f2e3d4c5b6a")
ENV_FILE = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")) / ".env"
TOKEN_NAME = "TODOIST_API_TOKEN"
CREATE_FIELDS = ("content", "description", "project_id", "section_id", "parent_id", "priority", "labels")
UPDATE_FIELDS = ("content", "description", "priority", "labels", "due_string")
KINDS = {"todoist.create": "item_add", "todoist.update": "item_update", "todoist.close": "item_close",
         "todoist.reopen": "item_uncomplete", "todoist.delete": "item_delete", "todoist.comment": "note_add",
         "todoist.import": "item_add"}


def api_token(env_file=ENV_FILE):
    value = os.environ.get(TOKEN_NAME) or _env_file_value(env_file, TOKEN_NAME)
    if not value:
        raise TokenUnavailable(f"{TOKEN_NAME} is not set in the environment or {env_file}")
    return value


def command_uuid(operation_id, index=0):
    """Stable per operation (and per item of a bulk import): the same operation never runs a command twice."""
    return str(uuid.uuid5(NAMESPACE, f"{operation_id}:{index}"))


class TodoistClient:
    """The few HTTP calls the adapter needs; failures are classified for the adapter contract."""

    def __init__(self, token=api_token, opener=urllib.request.urlopen, timeout=30, base=API):
        self.token, self.opener, self.timeout, self.base = token, opener, timeout, base

    def _request(self, method, path, *, body=None, form=None, query=None):
        try:
            token = self.token()
        except TokenUnavailable as error:
            raise NotDispatched(str(error)) from None
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, None
            if 400 <= error.code < 500 and error.code != 429:
                raise Rejected(f"todoist {error.code} on {method} {path}") from None
            raise Ambiguous(f"todoist {error.code} on {method} {path}") from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise NotDispatched(f"todoist unreachable: {type(reason).__name__}") from None
            raise Ambiguous(f"todoist request failed: {type(reason).__name__}") from None
        except (TimeoutError, socket.timeout, OSError) as error:
            raise Ambiguous(f"todoist request failed: {type(error).__name__}") from None

    def sync(self, commands):
        """Run Sync commands; a per-command error is a refusal (4xx) or ambiguous (anything else)."""
        _, payload = self._request("POST", "/sync", form={"commands": json.dumps(commands)})
        status = (payload or {}).get("sync_status", {})
        for command in commands:
            outcome = status.get(command["uuid"])
            if outcome == "ok":
                continue
            if isinstance(outcome, dict):
                code = int(outcome.get("http_code") or 0)
                tag = str(outcome.get("error_tag") or outcome.get("error") or "error")[:80]
                if 400 <= code < 500 and code != 429:
                    raise Rejected(f"todoist refused {command['type']}: {tag}")
                raise Ambiguous(f"todoist {command['type']} failed: {tag}")
            raise Ambiguous(f"todoist gave no status for {command['type']}")
        return (payload or {}).get("temp_id_mapping", {})

    def task(self, task_id):
        status, payload = self._request("GET", f"/tasks/{urllib.parse.quote(str(task_id))}")
        return None if status == 404 else payload

    def search(self, text):
        _, payload = self._request("GET", "/tasks/filter", query={"query": "search: " + text, "limit": 50})
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        return [row for row in rows or [] if isinstance(row, dict)]

    def comments(self, task_id):
        _, payload = self._request("GET", "/comments", query={"task_id": str(task_id), "limit": 50})
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        return [row for row in rows or [] if isinstance(row, dict)]


def item_args(params):
    args = {k: params[k] for k in CREATE_FIELDS if params.get(k) is not None}
    if params.get("due_string"):
        args["due"] = {"string": params["due_string"]}
    return args


def same_task(task, params):
    """Whether a found task is the one a create asked for (content and placement)."""
    if task.get("content") != params.get("content"):
        return False
    for key in ("project_id", "section_id", "parent_id"):
        if params.get(key) is not None and str(task.get(key)) != str(params[key]):
            return False
    return True


class TodoistAdapter(Adapter):
    name = "todoist"
    tracked = False

    def __init__(self, client=None):
        self.client = client or TodoistClient()

    # -- execute ------------------------------------------------------------------------

    def execute(self, operation, cancel):
        kind, params = operation["kind"], operation["params"]
        if kind not in KINDS:
            raise Rejected(f"unknown todoist operation {kind}")
        if kind == "todoist.import":
            return self._import(operation)
        command = {"type": KINDS[kind], "uuid": command_uuid(operation["id"]), "args": self._args(kind, params)}
        if kind == "todoist.create":
            command["temp_id"] = command_uuid(operation["id"], "temp")
        mapping = self.client.sync([command])
        if kind == "todoist.create":
            task_id = mapping.get(command["temp_id"])
            if task_id is None:
                # The command ran on an earlier attempt; the task exists but only a search names it.
                outcome, evidence = self.reconcile(operation)
                if outcome != DONE:
                    raise Ambiguous("todoist accepted the create earlier but the task was not found")
                return {"receipt": evidence["receipt"], "result": evidence["result"]}
            return {"receipt": f"todoist:task:{task_id}", "result": {"id": str(task_id), "content": params["content"]}}
        if kind == "todoist.comment":
            return {"receipt": f"todoist:comment:{command['uuid']}", "result": {"task_id": str(params["task_id"])}}
        return {"receipt": f"todoist:{kind.split('.')[1]}:{params['task_id']}", "result": {"id": str(params["task_id"])}}

    def _args(self, kind, params):
        if kind == "todoist.create":
            if not params.get("content"):
                raise Rejected("create needs content")
            return item_args(params)
        if not params.get("task_id"):
            raise Rejected(f"{kind} needs task_id")
        task_id = str(params["task_id"])
        if kind == "todoist.update":
            fields = {k: params[k] for k in UPDATE_FIELDS if params.get(k) is not None}
            if "due_string" in fields:
                fields["due"] = {"string": fields.pop("due_string")}
            if not fields:
                raise Rejected("update needs at least one field")
            return {"id": task_id, **fields}
        if kind == "todoist.comment":
            if not params.get("content"):
                raise Rejected("comment needs content")
            return {"item_id": task_id, "content": str(params["content"])}
        return {"id": task_id}

    def _import(self, operation):
        items = operation["params"].get("items") or []
        if not items or not all(isinstance(item, dict) and item.get("content") for item in items):
            raise Rejected("import needs items with content")
        commands = [{"type": "item_add", "uuid": command_uuid(operation["id"], index),
                     "temp_id": command_uuid(operation["id"], f"temp{index}"), "args": item_args(item)}
                    for index, item in enumerate(items)]
        mapping = self.client.sync(commands)
        ids = [mapping.get(command["temp_id"]) for command in commands]
        if any(task_id is None for task_id in ids):
            raise Ambiguous("todoist accepted the import earlier; the task ids are only known by search")
        return {"receipt": f"todoist:import:{len(ids)}", "result": {"ids": [str(i) for i in ids]}}

    # -- reconcile ----------------------------------------------------------------------

    def reconcile(self, operation):
        kind, params = operation["kind"], operation["params"]
        if kind == "todoist.create":
            found = [t for t in self.client.search(params["content"][:120]) if same_task(t, params)]
            if not found:
                return NOT_DONE, {}
            if len(found) > 1:
                return UNKNOWN, {"reason": f"{len(found)} tasks match the content; owner must choose"}
            return DONE, {"receipt": f"todoist:task:{found[0]['id']}",
                          "result": {"id": str(found[0]["id"]), "content": params["content"]}}
        if kind == "todoist.import":
            return UNKNOWN, {"reason": "bulk import is reconciled per item by the owner"}
        task = self.client.task(params["task_id"])
        receipt = f"todoist:{kind.split('.')[1]}:{params['task_id']}"
        if kind == "todoist.delete":
            return (DONE, {"receipt": receipt}) if task is None else (NOT_DONE, {})
        if task is None:
            return UNKNOWN, {"reason": "task not found"}
        completed = bool(task.get("checked") or task.get("is_completed") or task.get("completed_at"))
        if kind == "todoist.close":
            return (DONE, {"receipt": receipt}) if completed else (NOT_DONE, {})
        if kind == "todoist.reopen":
            return (NOT_DONE, {}) if completed else (DONE, {"receipt": receipt})
        if kind == "todoist.update":
            for key in ("content", "description", "priority"):
                if params.get(key) is not None and task.get(key) != params[key]:
                    return NOT_DONE, {}
            if params.get("labels") is not None and sorted(task.get("labels") or []) != sorted(params["labels"]):
                return NOT_DONE, {}
            return DONE, {"receipt": receipt}
        if kind == "todoist.comment":
            if any(c.get("content") == params.get("content") for c in self.client.comments(params["task_id"])):
                return DONE, {"receipt": receipt}
            return NOT_DONE, {}
        return UNKNOWN, {"reason": "reconciliation not supported"}
