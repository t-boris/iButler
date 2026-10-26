"""Wiring: the real bridge JavaScript against the real core over the Unix socket (REQ-022, DEC-045, DEC-051)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from butler_core.server import Server
from support import OWNER, CoreCase
from test_runtime import short_dir

NODE = Path.home() / ".nvm/versions/node/v22.22.3/bin/node"
DRIVER = Path(__file__).resolve().parents[1] / "bridge/test/e2e-driver.mjs"


@unittest.skipUnless(NODE.exists(), "node 22 not installed")
class BridgeWiringTest(CoreCase):
    def make_core(self, **kwargs):
        from butler_core.service import Core
        return Core(self.tmp / "state", adapters={"test": self.adapter}, transports={"telegram:butler": self.transport},
                    reserve=0, **kwargs)

    def test_message_task_turn_reply_and_command(self):
        sockdir = short_dir()
        server = Server(self.core, sockdir / "core.sock", classify=lambda pid: "gateway")
        server.start()
        try:
            out = subprocess.run([str(NODE), str(DRIVER), str(sockdir / "core.sock"), OWNER[9:]],
                                 capture_output=True, text=True, timeout=60)
        finally:
            server.stop()
            shutil.rmtree(sockdir, ignore_errors=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIsNone(result["first"])                                  # passed on to the agent
        self.assertEqual([r["messageId"] for r in result["reactions"]], ["501", "503"])
        self.assertTrue(result["command"]["handled"])
        self.assertIn("Сделай сводку", result["command"]["text"])
        chat = self.core.tasks.list(kinds=("chat",))[0]
        self.assertEqual(chat["state"], "succeeded")
        self.assertEqual(self.core.store.meta("gateway_instance"), "4242@1790000000")
        reply = self.core.store.one("SELECT state, receipt FROM deliveries WHERE kind='reply'")
        self.assertEqual(tuple(reply), ("observed", "502"))
        metric = self.core.store.one("SELECT COUNT(*) FROM metrics WHERE name='receipt.text'")[0]
        self.assertEqual(metric, 2)
        self.assertEqual(result["stats"]["unreachable"], 0)


if __name__ == "__main__":
    unittest.main()
