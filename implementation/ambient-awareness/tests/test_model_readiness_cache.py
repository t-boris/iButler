"""BUG-027: the panel's component list asked Ollama about every local model on every poll.

The panel reloads the component list about every 10 s while its window is visible. Each reload ran `LocalAnalyzer.check_local`
(an `/api/show` request) for the dialogue and the vision model, so the dialogue Ollama log held about 360 probes an hour,
99 % of its lines. The list only shows whether a model is installed, local and capable, which changes when the owner pulls
or removes a model, so a short cache is enough. The gate that runs before an observation is sent to a model
(`LocalAnalyzer.check_local` in `generate_json`) is not cached and is covered below.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from butler_ambient import components
from butler_ambient.analysis import LocalAnalyzer


class ModelReadinessCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        original = components.CLOCK
        components.CLOCK = lambda: self.now
        self.addCleanup(setattr, components, "CLOCK", original)
        components.forget_cached_reads()
        self.addCleanup(components.forget_cached_reads)
        self.probes, self.failing = [], False

        def check(analyzer, require_vision=False):
            self.probes.append((analyzer.model, require_vision, analyzer.port))
            if self.failing: raise RuntimeError("required local model unavailable; no external fallback")
        patcher = patch.object(LocalAnalyzer, "check_local", check)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_polls_inside_the_lifetime_probe_a_model_once(self):
        for _ in range(30):
            self.now += 10                                              # the panel's poll interval
            if self.now - 1000 > components.READY_TTL - 11: break
            self.assertTrue(components.local_model_ready("gemma4-brain", False, True))
        self.assertEqual(self.probes, [("gemma4-brain", False, 11435)])

    def test_a_ready_model_is_probed_again_after_its_lifetime(self):
        components.local_model_ready("gemma3:4b", True, False)
        self.now += components.READY_TTL + 1
        components.local_model_ready("gemma3:4b", True, False)
        self.assertEqual(len(self.probes), 2)

    def test_a_failure_is_looked_at_again_much_sooner(self):
        self.failing = True
        self.assertFalse(components.local_model_ready("gemma3:4b", True, False))
        self.now += components.NOT_READY_TTL - 1
        self.assertFalse(components.local_model_ready("gemma3:4b", True, False))
        self.assertEqual(len(self.probes), 1)
        self.failing = False                                            # the model finished downloading
        self.now += 2
        self.assertTrue(components.local_model_ready("gemma3:4b", True, False))
        self.assertEqual(len(self.probes), 2)
        self.assertLess(components.NOT_READY_TTL, components.READY_TTL)

    def test_each_model_role_has_its_own_entry(self):
        components.local_model_ready("gemma3:4b", True, False)          # vision, background port
        components.local_model_ready("gemma3:4b", False, True)          # same name, dialogue port
        components.local_model_ready("gemma3:4b", False, False)
        self.assertEqual(sorted(self.probes), sorted([("gemma3:4b", True, 11434), ("gemma3:4b", False, 11435),
                                                      ("gemma3:4b", False, 11434)]))

    def test_a_control_action_drops_the_answer(self):
        components.local_model_ready("gemma3:4b", True, False)
        components.forget_cached_reads()                                # what every write through `command` does
        components.local_model_ready("gemma3:4b", True, False)
        self.assertEqual(len(self.probes), 2)

    def test_an_error_other_than_unavailable_is_not_hidden(self):
        with patch.object(LocalAnalyzer, "check_local", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError): components.local_model_ready("gemma3:4b")

    def test_the_component_list_reuses_the_answers_between_polls(self):
        service = SimpleNamespace(config={}, store=SimpleNamespace(root=components.Path("/nonexistent-butler-state")),
                                  input_running=False, verifier=None, voice=None, visual=None, analyzer=SimpleNamespace(operations=[]),
                                  catalog=None)
        first = components.snapshot(service)["components"]
        self.now += 10
        second = components.snapshot(service)["components"]
        by_id = {row["id"]: row["ready"] for row in first}
        self.assertTrue(by_id["conversation_model"] and by_id["vision_model"])
        self.assertEqual([row["ready"] for row in first], [row["ready"] for row in second])
        self.assertEqual(len(self.probes), 2)                           # one per model for both polls


class AdmissionGateIsNotCachedTests(unittest.TestCase):
    """The check before an observation is included in a request must look at the server every time."""

    def test_every_check_asks_the_server(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
                seen.append(handler.path)
                body = json.dumps({"model_info": {"general.architecture": "x"}, "capabilities": ["completion"]}).encode()
                handler.send_response(200); handler.send_header("Content-Length", str(len(body))); handler.end_headers()
                handler.wfile.write(body)

            def log_message(handler, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        analyzer = LocalAnalyzer("gemma3:4b")
        analyzer.port = server.server_port
        for _ in range(3): analyzer.check_local()
        self.assertEqual(seen, ["/api/show"] * 3)


if __name__ == "__main__":
    unittest.main()
