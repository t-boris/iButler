"""BUG-025: three bridge timeouts ("core did not answer within 1200 ms") coincided with the core's 6-hourly backups, but nothing
on the core side showed whether it was slow. Every request starts with a /bin/ps lookup of the caller; a request that spends
too long there, or a bridge call that spends too long being handled, is now recorded so the next occurrence explains itself.
"""
import json
import shutil
import time
import unittest

from butler_core.client import Client
from butler_core.server import Server
from support import CoreCase
from test_runtime import short_dir


class SlowRpcTest(CoreCase):
    def serve(self, classify):
        sockdir = short_dir()
        self.addCleanup(shutil.rmtree, sockdir, ignore_errors=True)
        server = Server(self.core, sockdir / "core.sock", classify=classify)
        server.slow = 0.1                                    # seconds; the production threshold is half a second
        server.start()
        self.addCleanup(server.stop)
        return Client(sockdir / "core.sock", timeout=10)

    def slow_events(self):
        return [r["detail"] for r in self.core.store.query("SELECT detail FROM diagnostics WHERE kind='rpc.slow'")]

    def test_a_slow_caller_lookup_is_recorded_with_its_method(self):
        def classify(pid):
            time.sleep(0.3)
            return "gateway"
        client = self.serve(classify)
        client.call("ping")
        (event,) = [json.loads(e) for e in self.slow_events()]
        self.assertEqual((event["method"], event["caller"]), ("ping", "gateway"))
        self.assertGreaterEqual(event["classify_ms"], 250)

    def test_fast_requests_leave_no_trace(self):
        client = self.serve(lambda pid: "gateway")
        for _ in range(5):
            client.call("ping")
        self.assertEqual(self.slow_events(), [])

    def test_a_slow_bridge_call_is_recorded_but_a_long_operation_is_not(self):
        client = self.serve(lambda pid: "gateway")
        real = self.core.bridge_poll
        self.core.bridge_poll = lambda: (time.sleep(0.3), real())[1]
        client.call("bridge.config")
        self.assertEqual(len(self.slow_events()), 1)
        real_doctor = self.core.doctor
        self.core.doctor = lambda: (time.sleep(0.3), real_doctor())[1]     # slow, but not on the bridge's 1.2 s budget
        client.call("doctor")
        self.assertEqual(len(self.slow_events()), 1)


if __name__ == "__main__":
    unittest.main()
