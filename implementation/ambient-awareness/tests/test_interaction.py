"""Owner UI and voice selection boundaries; no camera, audio output or models."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.local_ui import snapshot, observe
from butler_ambient.store import Store
from butler_ambient.service import Service
from butler_ambient.voice_catalog import profiles, select, preview
from butler_ambient.policy import Evidence


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.service = Service(self.store, Mock(), Mock(), Mock(), transport=Mock())

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def profile(self):
        root = self.store.root / "voice"
        (root / "previews").mkdir(parents=True)
        (root / "previews/calm.wav").write_bytes(b"synthetic preview")
        (root / "profiles.json").write_text(json.dumps({"synthetic": True, "profiles": [{"id": "calm", "name": "Calm"}]}))

    def test_local_state_labels_historical_data_without_new_sensing(self):
        mid = self.store.media("camera", 10, 11, b"old image", "jpg")
        self.store.save_observation(mid, [{"detail": "local-only detail"}])
        value = snapshot(self.service)
        self.assertFalse(value["recording_allowed"])
        self.assertEqual(value["last_capture"], {"camera": 11})
        self.assertEqual(value["recent"], [])
        self.assertNotIn("agreements", value)
        self.service.inputs.start.assert_not_called()
        self.service.analyzer.analyze.assert_not_called()

    def test_paused_local_preview_cannot_capture_or_return_image(self):
        self.assertEqual(observe(self.service, "camera")["status"], "paused")
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])
        self.service.inputs.start.assert_not_called()

    def test_live_panel_only_reuses_recorded_permitted_frames_and_removes_preview_on_pause(self):
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        now = self.service.clock()
        self.service.gate.enable(True, now-3)
        for offset in (-2, -1, 0):
            self.service.gate.check(Evidence(now + offset, True, True, frozenset({'owner'}), True), now + offset)
        self.store.media("camera", now - 1, now, b'\xff\xd8verified\xff\xd9', 'mjpeg')
        value = snapshot(self.service, want_preview=True)
        self.assertEqual(Path(value['preview']['path']).read_bytes(), b'\xff\xd8verified\xff\xd9')
        self.service.control('enabled', {'value': False})
        self.assertIsNone(snapshot(self.service, want_preview=True)['preview'])
        self.assertFalse((self.store.root/'raw/ui-preview.jpg').exists())

    def test_synthetic_preview_does_not_activate_voice_and_requires_known_profile(self):
        self.profile()
        with patch("butler_ambient.voice_catalog.subprocess.run") as run:
            self.assertFalse(preview(self.store, "calm")["voice_activated"])
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/afplay")
        self.assertFalse(self.store.get("voice_enabled", False))
        with self.assertRaises(ValueError): select(self.store, "../owner-reference")
        self.assertFalse(self.store.get("voice_enabled", False))
        self.assertEqual(select(self.store, "calm")["selected"], "calm")
        self.assertTrue(self.store.get("voice_enabled"))

    def test_arbitrary_or_nonsynthetic_voice_references_are_not_offered(self):
        self.profile()
        root = self.store.root / "voice"
        (root / "profiles.json").write_text(json.dumps({"synthetic": False, "profiles": [{"id": "calm", "name": "Calm"}]}))
        self.assertEqual(profiles(self.store), [])
        with self.assertRaises(ValueError): select(self.store, "calm")


if __name__ == "__main__":
    unittest.main()
