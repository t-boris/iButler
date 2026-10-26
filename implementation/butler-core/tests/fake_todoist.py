"""A Todoist stand-in for tests: Sync commands run once per uuid, tasks are readable back.

``FakeTodoist.handle`` serves the adapter directly (as a urllib opener) and the
installed workspace library over a real local HTTP server, so both see one store.
"""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeTodoist:
    def __init__(self):
        self.tasks, self.comments, self.seen, self.requests, self.next_id = {}, [], set(), [], 100
        self.projects = [{"id": "p9", "name": "Demo"}]
        self.labels = []
        self.fail = None            # "timeout-after", "timeout-before", "http500", "http400", "refuse-command"
        self.lock = threading.Lock()

    # -- one request ------------------------------------------------------------------

    def handle(self, method, path, query, body):
        """Return ``(status, payload)``; raise TimeoutError to simulate a lost response."""
        with self.lock:
            self.requests.append((method, path, dict(query)))
            if path == "/sync":
                return 200, self._sync(json.loads(body["commands"]))
            if path == "/tasks/filter":
                text = query["query"].removeprefix("search: ")
                return 200, {"results": [t for t in self.tasks.values() if text in t["content"]]}
            if path == "/tasks/completed/by_completion_date":
                return 200, {"results": [t for t in self.tasks.values() if t["checked"]]}
            if path == "/tasks" and method == "POST":
                # The direct REST path of the legacy library: no uuid, so every call creates a task.
                args = body
                self.next_id += 1
                task = {"id": str(self.next_id), "content": args.get("content", ""), "description": "",
                        "project_id": args.get("project_id") or "inbox", "labels": args.get("labels") or [],
                        "priority": args.get("priority") or 1, "checked": False, "due": None}
                self.tasks[task["id"]] = task
                return 200, task
            if path == "/tasks":
                return 200, {"results": [t for t in self.tasks.values() if not t["checked"]]}
            if path.startswith("/tasks/"):
                task = self.tasks.get(path.split("/")[-1])
                return (404, None) if task is None else (200, task)
            if path == "/comments":
                return 200, {"results": [c for c in self.comments if c["task_id"] == query["task_id"]]}
            if path == "/projects":
                return 200, {"results": list(self.projects)}
            if path == "/labels":
                return 200, {"results": list(self.labels)}
            return 404, None

    def _sync(self, commands):
        fail, self.fail = self.fail, None
        if fail == "timeout-before":
            raise TimeoutError()
        if fail == "http500":
            raise HTTPFailure(500)
        if fail == "http400":
            raise HTTPFailure(400)
        status, mapping = {}, {}
        for command in commands:
            if fail == "refuse-command":
                status[command["uuid"]] = {"error_tag": "INVALID_ARGUMENT", "http_code": 400, "error": "bad args"}
                continue
            if command["uuid"] in self.seen:
                status[command["uuid"]] = "ok"           # already executed: no effect, no id mapping
                continue
            self.seen.add(command["uuid"])
            args = command["args"]
            if command["type"] == "item_add":
                self.next_id += 1
                task = {"id": str(self.next_id), "content": args["content"], "description": args.get("description", ""),
                        "project_id": args.get("project_id") or "inbox", "labels": args.get("labels") or [],
                        "priority": args.get("priority") or 1, "checked": False, "due": args.get("due")}
                self.tasks[task["id"]] = task
                mapping[command["temp_id"]] = task["id"]
            elif command["type"] == "item_close":
                self.tasks[args["id"]]["checked"] = True
            elif command["type"] == "item_uncomplete":
                self.tasks[args["id"]]["checked"] = False
            elif command["type"] == "item_delete":
                self.tasks.pop(args["id"], None)
            elif command["type"] == "item_update":
                self.tasks[args["id"]].update({k: v for k, v in args.items() if k != "id"})
            elif command["type"] == "note_add":
                self.comments.append({"task_id": args["item_id"], "content": args["content"]})
            status[command["uuid"]] = "ok"
        if fail == "timeout-after":
            raise TimeoutError()
        return {"sync_status": status, "temp_id_mapping": mapping}

    # -- as a urllib opener -----------------------------------------------------------------

    def __call__(self, request, timeout):
        parsed = urllib.parse.urlparse(request.full_url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        body = dict(urllib.parse.parse_qsl(request.data.decode())) if request.data else {}
        try:
            status, payload = self.handle(request.get_method(), parsed.path, query, body)
        except HTTPFailure as failure:
            raise urllib.error.HTTPError(request.full_url, failure.code, "error", {}, io.BytesIO(b'{"error":"x"}')) from None
        if status == 404:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b""))
        return Response(payload)

    # -- as a local HTTP server --------------------------------------------------------------

    def serve(self):
        """Start a loopback server; returns its base URL. Stop with ``shutdown``."""
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                parsed = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                if raw and "json" in (self.headers.get("Content-Type") or ""):
                    body = json.loads(raw)
                else:
                    body = dict(urllib.parse.parse_qsl(raw)) if raw else {}
                try:
                    status, payload = store.handle(self.command, parsed.path, query, body)
                except HTTPFailure as failure:
                    status, payload = failure.code, {"error": "x"}
                except TimeoutError:
                    self.connection.close()       # the client sees a dropped connection: outcome unknown
                    return
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def shutdown(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()


class HTTPFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Response:
    def __init__(self, payload):
        self.payload, self.status = payload, 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()
