"""Call metadata gates replies, never recording or computer audio volume."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.policy import Evidence
from butler_ambient.conversation import Conversation, Canceled


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = 100.
        self.inputs = Mock(); self.inputs.audio_status = {}
        self.s = Service(self.store,self.inputs,Mock(),Mock(),{'audio_helper':'/own/butler-audio'},clock=lambda:self.now,transport=Mock())
        self.m = self.s.meeting

    def tearDown(self):
        self.s.stop(); self.store.close(); self.temp.cleanup()

    def health(self, inputs=(), outputs=(), available=True):
        self.inputs.audio_status = {'meeting_at':self.now,'meeting':{'available':available,'input_apps':list(inputs),'output_apps':list(outputs)}}
        return self.m.snapshot()

    def test_actual_input_not_idle_browser_or_youtube_implies_quiet(self):
        self.assertFalse(self.health(outputs=['Safari / WebKit'])['quiet'])
        self.assertFalse(self.health(['Safari / WebKit'])['quiet'])          # one check is not a meeting (REQ-035)
        self.health(['Safari / WebKit'])
        self.assertTrue(self.health(['Safari / WebKit'])['quiet'])
        self.assertEqual(self.m.state,'external-microphone-active')
        self.inputs.play.assert_not_called()

    def test_muted_call_output_and_end_debounce(self):
        for _ in range(3): self.health(['Chrome'])
        self.now += 20
        self.assertTrue(self.health(outputs=['Chrome'])['quiet'])
        self.now += 1
        self.assertFalse(self.health()['quiet'])
        for _ in range(3): self.health(['Zoom'])
        self.now += 4
        self.assertTrue(self.health()['quiet'])
        self.now += 13
        self.assertFalse(self.health()['quiet'])

    def test_missing_or_stale_detector_is_not_a_meeting(self):
        # DEC-066: unknown is not busy. A dead detector used to mean «quiet» and silenced the owner alone at home.
        snapshot = self.m.snapshot()
        self.assertEqual((snapshot['quiet'], snapshot['state']), (False, 'detector-unavailable'))
        for _ in range(3): self.health(['Zoom'])
        self.assertTrue(self.m.snapshot()['quiet'])
        self.now += 30
        snapshot = self.m.snapshot()
        self.assertEqual((snapshot['quiet'], snapshot['state']), (False, 'detector-unavailable'))
        self.assertFalse(self.health(available=False)['quiet'])      # a failed detector is not a meeting (DEC-066)
        self.assertFalse(self.health()['quiet'])

    def test_meeting_entry_cancels_inflight_and_pending_operation_only(self):
        self.health(); self.s.voice = Mock(reply_in_progress=False)
        self.s.commands.pending = object()
        for _ in range(3): self.health(['Safari / WebKit'])
        self.s.voice.cancel.assert_called_once_with(clear_context=False,reason='meeting-started')
        self.assertIsNone(self.s.commands.pending)
        self.assertFalse(self.store.get('enabled',False))
        self.inputs.stop.assert_not_called()
        self.health(['Safari / WebKit']); self.s.voice.cancel.assert_called_once()

    def test_owner_native_override_and_mode_validation(self):
        self.s.control('preferences',{'meeting_mode':'quiet'})
        self.assertTrue(self.health()['quiet'])
        self.s.control('preferences',{'meeting_mode':'available'})
        self.assertFalse(self.health(['Zoom'])['quiet'])
        self.s.control('preferences',{'meeting_mode':'auto'})
        self.assertTrue(self.health(['Zoom'])['quiet'])
        with self.assertRaises(ValueError):self.s.control('preferences',{'meeting_mode':'invented'})


if __name__ == '__main__':unittest.main()
