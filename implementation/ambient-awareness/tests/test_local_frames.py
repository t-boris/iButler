"""Stored-frame scope uses only synthetic files in isolated temporary stores."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from butler_ambient.event_frames import ZONE
from butler_ambient.local_ui import snapshot, stored_frames
from butler_ambient.service import Service
from butler_ambient.store import Store

class StoredFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)); self.addCleanup(self.store.close)
        self.now = dt.datetime(2026, 10, 28, 12, tzinfo=ZONE).timestamp()
        self.s = Service(self.store, Mock(audio_status={}, playback={'state':'idle'}), Mock(), Mock(), clock=lambda:self.now, transport=Mock())
        self.addCleanup(self.s.stop)
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.store.set('continuous_archive', True); self.store.set('single_user_local', True)
        self.s.gate.enable(True, self.now)

    def media(self, source, suffix, *, preserved):
        mid = self.store.media(source, self.now-3, self.now-2, b'synthetic image', suffix)
        if preserved: self.store.preserve(mid, self.now)
        return mid

    def test_only_preserved_stills_show_when_identity_gate_is_closed(self):
        camera = self.media('camera', 'jpg', preserved=True)
        screen = self.media('screen', 'png', preserved=True)
        self.media('camera', 'jpg', preserved=False)
        self.media('camera', 'mp4', preserved=True)
        self.media('microphone', 'jpg', preserved=True)
        result = snapshot(self.s)
        self.assertTrue(result['history_visible']); self.assertFalse(result['recordings_raw_allowed'])
        self.assertTrue(result['stored_frames_allowed'])
        self.assertEqual({x['id'] for x in result['stored_frames']}, {'stored-frame:'+camera, 'stored-frame:'+screen})
        self.assertTrue(all(Path(x['path']).is_relative_to(self.store.root/'preserved') for x in result['stored_frames']))

    def test_pause_archive_off_and_revocation_hide_frames(self):
        self.media('camera', 'jpg', preserved=True)
        self.s.control('enabled', {'value':False})
        self.assertEqual(snapshot(self.s)['stored_frames'], [])
        self.s.control('enabled', {'value':True})
        self.s.control('preferences', {'continuous_archive':False})
        self.assertEqual(snapshot(self.s)['stored_frames'], [])
        self.s.control('preferences', {'continuous_archive':True})
        self.s.control('revoke', {'id':'owner'})
        self.assertFalse(snapshot(self.s)['stored_frames_allowed'])
        self.assertEqual(snapshot(self.s)['stored_frames'], [])

    def test_symlink_and_outside_paths_are_excluded(self):
        mid = self.media('camera','jpg',preserved=True)
        outside = self.store.root/'outside.jpg'; outside.write_bytes(b'synthetic')
        link = self.store.root/'preserved'/'link.jpg'; link.symlink_to(outside)
        self.store.execute('UPDATE media SET preserved=? WHERE id=?',(str(link),mid))
        self.assertEqual(stored_frames(self.s), [])
        self.store.execute('UPDATE media SET preserved=? WHERE id=?',(str(outside),mid))
        self.assertEqual(stored_frames(self.s), [])

    def test_scope_loss_during_listing_drops_results(self):
        self.media('camera','jpg',preserved=True)
        real = Path.is_file
        def close_during_check(path):
            self.s.gate.enable(False,self.now)
            return real(path)
        with patch.object(Path,'is_file',close_during_check):
            self.assertEqual(stored_frames(self.s), [])

    def test_mode_off_and_stop_hide_when_strict_gate_is_closed(self):
        self.media('camera','jpg',preserved=True)
        self.store.set('single_user_local',False)
        self.assertEqual(stored_frames(self.s), [])
        self.store.set('single_user_local',True); self.s.stop_event.set()
        self.assertEqual(stored_frames(self.s), [])

    def test_guest_revocation_hides_frames_and_revokes_single_user_mode(self):
        self.media('camera','jpg',preserved=True)
        self.store.execute("INSERT INTO agreements VALUES ('guest','Guest',1,'{}')")
        self.s.control('revoke', {'id':'guest'})
        self.assertFalse(self.store.get('single_user_local'))
        self.assertEqual(stored_frames(self.s), [])

if __name__ == '__main__': unittest.main()
