"""Smart Turn shadow (attention REQ-006): numbers only, its own worker, off when the environment is absent."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from butler_ambient.endpointing import SmartTurnShadow

TURN_ENV = Path.home() / ".openclaw/tools/ambient-awareness/turn-env/bin/python"
FAKE_WORKER = '''#!/usr/bin/env python3
import sys, json, base64
for line in sys.stdin:
    pcm = base64.b64decode(json.loads(line)["pcm"])
    sys.stdout.write(json.dumps({"probability": 0.25 if len(pcm) < 64000 else 0.9, "prediction": 1, "ms": 7}) + "\\n"); sys.stdout.flush()
'''


class ShadowTests(unittest.TestCase):
    def test_absent_environment_means_shadow_off_not_an_error(self):
        shadow = SmartTurnShadow("/nonexistent/python")
        self.assertIsNone(shadow.judge(bytes(32000 * 2)))
        self.assertEqual(shadow.snapshot()["state"], "not-configured")

    def test_protocol_round_trip_with_a_stand_in_worker_keeps_only_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "python"
            script.write_text(FAKE_WORKER); script.chmod(0o700)
            shadow = SmartTurnShadow(script)
            # The shadow launches "<python> -m butler_ambient.endpointing worker"; the stand-in ignores its arguments.
            short, long = shadow.judge(bytes(32000)), shadow.judge(bytes(32000 * 4))
            shadow.stop()
        self.assertEqual((short["complete_p"], long["complete_p"], short["ms"]), (0.25, 0.9, 7))
        self.assertEqual(shadow.snapshot()["samples"], 2)
        self.assertNotIn("pcm", str(shadow.snapshot()))

    def test_too_little_audio_is_not_judged(self):
        shadow = SmartTurnShadow(sys.executable)
        self.assertIsNone(shadow.judge(bytes(100)))

    @unittest.skipUnless(TURN_ENV.is_file(), "turn-env with Smart Turn not installed")
    def test_installed_worker_answers_within_budget(self):
        shadow = SmartTurnShadow(TURN_ENV)
        try:
            first = shadow.judge(bytes(32000 * 3), timeout=20)
            second = shadow.judge(bytes(32000 * 3))
        finally:
            shadow.stop()
        self.assertIsNotNone(first); self.assertLessEqual(second["round_trip_ms"], 300)
        self.assertEqual(shadow.snapshot()["state"], "ready")


if __name__ == "__main__":
    unittest.main()
