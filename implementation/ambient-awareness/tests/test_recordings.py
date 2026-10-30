"""Specification regressions on private temporary stores, never the owner's recordings."""
import datetime as dt
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient.recordings import list_recordings, control, playback, mark_meeting, tag_capture, voice_target, navigate_voice, RETENTION
from butler_ambient.store import Store
from butler_ambient.service import Service, Handler
from butler_ambient.policy import Evidence
from butler_ambient.event_frames import ZONE
from butler_ambient.listening import Listening
from butler_ambient.conversation import Conversation
from butler_ambient.local_ui import snapshot


class RecordingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)); self.addCleanup(self.store.close)
        self.now = dt.datetime(2026, 10, 27, 12, tzinfo=ZONE).timestamp()
        self.s = Service(self.store, Mock(audio_status={}, playback={'state':'idle'}), Mock(), Mock(),
                         clock=lambda: self.now, transport=Mock())
        self.addCleanup(self.s.stop)
        self.authorize()
        self.s.latest_evidence = Evidence(self.now, True, True)
        self.store.execute('DELETE FROM events')

    def authorize(self):
        self.store.execute("INSERT OR REPLACE INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.s.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .01
            evidence = Evidence(self.now, True, True, frozenset({'owner'}), True,
                camera_ids=frozenset({'owner'}), camera_certain=True, voice_ids=frozenset({'owner'}), voice_certain=True, speech_detected=True)
            self.s.gate.check(evidence, self.now)
            self.s.dialogue_gate.check(evidence, self.now, True)

    def observe(self, start=None, *, activity='work', source='microphone', temporary=False):
        start = self.now if start is None else start
        mid = self.store.media(source, start, start+1, b'original', 'wav', temporary=temporary)
        oid = self.store.save_observation(mid, [{'kind':'activity','activity':activity,'detail':activity,'basis':'model-inference','confidence':.8}])
        return oid, mid

    def children(self, **args):
        return [o for item in list_recordings(self.s, args)['items'] if item['type']=='session'
                for o in list_recordings(self.s, {**args,'session':item['id']})['items']]

    def current_meeting(self):
        for _ in range(3):
            self.now += .01
            self.s.inputs.audio_status = {'meeting_at':self.now, 'meeting':{'available':True, 'input_apps':['Meet'], 'output_apps':['Meet']}}
            self.s.meeting.update()
        return self.s.meeting.recording_id

    def test_owner_session_required_for_all_reads_and_actions(self):
        oid, mid = self.observe()
        self.s.dialogue_gate.invalidate('company')
        self.assertEqual(list_recordings(self.s, {})['items'], [])
        self.assertFalse(control(self.s, {'action':'delete','observation':oid,'media':mid})['ok'])
        self.assertEqual(playback(self.s, {'observation':oid,'media':mid}), {'hidden':True})
        self.assertFalse(mark_meeting(self.s, {'action':'mark'})['ok'])
        ui = snapshot(self.s)
        self.assertEqual(ui['recent'], [])
        self.assertFalse(ui['recordings_allowed'])
        self.assertIsNone(ui['recordings_navigation'])
        self.authorize(); self.s.gate.invalidate(self.now)
        self.assertTrue(list_recordings(self.s, {})['hidden'])

    def test_session_open_survives_pending_media_becoming_an_observation(self):
        mid = self.store.media('camera', self.now - 20, self.now - 19, b'synthetic', 'jpg', temporary=True)
        before = list_recordings(self.s, {'source': 'camera', 'empty': True})
        session = next(row for row in before['items'] if row['type'] == 'session')
        self.assertEqual(session['id'], 'session:media:' + mid)
        oid = self.store.save_observation(mid, [{'kind': 'activity', 'activity': 'synthetic', 'detail': 'synthetic'}])
        opened = list_recordings(self.s, {'source': 'camera', 'empty': True, 'session': session['id']})
        self.assertEqual([row['id'] for row in opened['items']], [oid])

    def test_session_open_survives_an_earlier_observation_joining_it(self):
        mid = self.store.media('camera', self.now - 20, self.now - 19, b'synthetic', 'jpg', temporary=True)
        oid = self.store.save_observation(mid, [{'kind': 'activity', 'activity': 'synthetic'}])
        session = next(row for row in list_recordings(self.s, {'source': 'camera', 'empty': True})['items'] if row['type'] == 'session')
        earlier = self.store.media('camera', self.now - 30, self.now - 29, b'synthetic', 'jpg', temporary=True)
        earlier_id = self.store.save_observation(earlier, [{'kind': 'activity', 'activity': 'synthetic'}])
        opened = list_recordings(self.s, {'source': 'camera', 'empty': True, 'session': session['id']})
        self.assertEqual({row['id'] for row in opened['items']}, {earlier_id, oid})

    def test_session_open_survives_transcript_becoming_linked_to_observation(self):
        self.store.retain_transcript('synthetic-t', 'microphone', self.now - 20, self.now - 19, 'synthetic text', now=self.now)
        session = next(row for row in list_recordings(self.s, {'source': 'microphone', 'empty': True})['items'] if row['type'] == 'session')
        self.assertEqual(session['id'], 'session:transcript:synthetic-t')
        mid = self.store.media('microphone', self.now - 20, self.now - 19, b'synthetic', 'wav', temporary=True)
        oid = self.store.save_observation(mid, [{'kind': 'activity', 'activity': 'synthetic'}])
        opened = list_recordings(self.s, {'source': 'microphone', 'empty': True, 'session': session['id']})
        self.assertEqual([row['id'] for row in opened['items']], [oid])

    def test_results_only_filter_keeps_facts_and_transcripts_but_hides_pending(self):
        from butler_ambient.recordings import _media
        pending = self.store.media('microphone', self.now - 5, self.now - 4, b'synthetic', 'wav', temporary=True)
        fact_media = self.store.media('camera', self.now - 30, self.now - 29, b'synthetic', 'jpg', temporary=True)
        fact = self.store.save_observation(fact_media, [{'kind': 'activity', 'activity': 'synthetic'}])
        self.store.retain_transcript('synthetic-t', 'system-audio', self.now - 60, self.now - 59, 'synthetic text', now=self.now)
        with patch('butler_ambient.recordings._media', wraps=_media) as media_lookup:
            visible = list_recordings(self.s, {'results_only': True, 'empty': True})
        self.assertNotIn(pending, [call.args[0]['id'] for call in media_lookup.call_args_list])
        anchors = {row['id'] for row in visible['items'] if row['type'] == 'session'}
        self.assertIn('session:' + fact, anchors)
        self.assertIn('session:transcript:synthetic-t', anchors)
        self.assertNotIn('session:media:' + pending, anchors)
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        local = list_recordings(self.s, {'results_only': True, 'empty': True, 'session': 'session:' + fact})
        self.assertEqual([row['id'] for row in local['items']], [fact])
        self.assertTrue(all(file['path'] is None for row in local['items'] for file in row['media']))
        all_rows = list_recordings(self.s, {'results_only': False, 'empty': True})
        self.assertIn('session:media:' + pending, {row.get('id') for row in all_rows['items']})
        with self.assertRaises(ValueError): list_recordings(self.s, {'results_only': 1})

    def test_single_user_history_does_not_grant_raw_access_and_pause_or_revocation_hide_it(self):
        oid, mid = self.observe()
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence')
        self.s.gate.invalidate(self.now)
        ui = snapshot(self.s)
        self.assertTrue(ui['history_visible'])
        self.assertTrue(ui['recordings_allowed'])
        self.assertFalse(ui['recordings_raw_allowed'])
        self.assertTrue(ui['recent'])
        items = self.children()
        self.assertTrue(any(item['id'] == oid for item in items))
        self.assertTrue(all(media['path'] is None for item in items for media in item['media']))
        self.assertEqual(playback(self.s, {'observation': oid, 'media': mid}), {'hidden': True})
        self.s.control('preferences', {'continuous_archive': False})
        self.assertTrue(list_recordings(self.s, {})['hidden'])
        self.s.control('preferences', {'continuous_archive': True})
        self.s.control('enabled', {'value': False})
        self.assertTrue(list_recordings(self.s, {})['hidden'])
        self.s.control('enabled', {'value': True})
        self.s.control('revoke', {'id': 'owner'})
        self.assertTrue(list_recordings(self.s, {})['hidden'])
        self.assertFalse(self.store.get('single_user_local'))

    def test_single_user_history_does_not_expose_gallery_paths(self):
        self.observe()
        self.s.visual = Mock()
        self.s.visual.snapshot.return_value = {'artifacts': [{'path': '/synthetic/private.jpg'}], 'roots': ['/synthetic'], 'status': 'ready'}
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence')
        self.s.gate.invalidate(self.now)
        value = snapshot(self.s)
        self.assertTrue(value['recordings_allowed'])
        self.assertFalse(value['recordings_raw_allowed'])
        self.assertEqual(value['visual'], {'artifacts': [], 'roots': [], 'status': 'hidden'})

    def test_ui_snapshot_rechecks_expired_raw_access_before_returning(self):
        self.observe()
        self.store.set('single_user_local', True)
        self.s.visual = Mock()
        def slow_gallery():
            self.now += 3
            return {'artifacts': [{'id': 'synthetic', 'path': '/synthetic/private.jpg'}], 'roots': ['/synthetic'], 'status': 'ready'}
        self.s.visual.snapshot.side_effect = slow_gallery
        value = snapshot(self.s)
        self.assertTrue(value['history_visible'])
        self.assertFalse(value['recordings_raw_allowed'])
        self.assertEqual(value['visual'], {'artifacts': [], 'roots': [], 'status': 'hidden'})
        self.assertFalse(any(entry.get('image') for entry in value['feed']))

    def test_ui_snapshot_hides_history_when_recording_is_switched_off_during_assembly(self):
        self.observe()
        self.store.set('single_user_local', True)
        self.s.visual = Mock()
        def archive_off():
            self.store.set('continuous_archive', False)
            return {'artifacts': [{'id': 'synthetic', 'path': '/synthetic/private.jpg'}], 'roots': ['/synthetic'], 'status': 'ready'}
        self.s.visual.snapshot.side_effect = archive_off
        value = snapshot(self.s)
        self.assertFalse(value['history_visible'])
        self.assertFalse(value['recordings_allowed'])
        self.assertFalse(value['recordings_raw_allowed'])
        self.assertEqual(value['recent'], [])
        self.assertEqual(value['feedback']['episodes'], [])
        self.assertFalse(any(entry.get('image') for entry in value['feed']))

    def test_ui_snapshot_suppresses_live_preview_when_service_stops_during_assembly(self):
        self.store.set('single_user_local', True)
        self.store.set('continuous_archive', True)
        self.s.preview_frame = (b'synthetic preview bytes', self.now)
        self.s.visual = Mock()
        def stop_during_snapshot():
            self.s.stop_event.set()
            return {'artifacts': [], 'roots': [], 'status': 'ready'}
        self.s.visual.snapshot.side_effect = stop_during_snapshot
        value = snapshot(self.s, want_preview=True)
        self.assertFalse(value['history_visible'])
        self.assertFalse(value['recordings_raw_allowed'])
        self.assertFalse(value['recording_allowed'])
        self.assertIsNone(value['preview'])

    def test_single_user_local_history_is_not_hidden_by_identity_detection_but_audio_is_blocked(self):
        self.observe()
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('company')
        self.s.gate.invalidate(self.now)
        self.s.latest_evidence = Evidence(self.now, True, True, frozenset({'guest'}), True,
            camera_ids=frozenset({'guest'}), camera_certain=True)
        self.assertFalse(list_recordings(self.s, {})['hidden'])
        from butler_ambient.recordings import local_voice_history_allowed
        self.assertFalse(local_voice_history_allowed(self.s))
        self.assertFalse(snapshot(self.s)['recordings_raw_allowed'])

    def test_single_user_history_survives_transient_multiple_face_false_detection(self):
        from butler_ambient.dialogue_state import Scene
        from butler_ambient.recordings import local_voice_history_allowed
        self.observe(); self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('company'); self.s.gate.invalidate(self.now)
        self.s.latest_evidence = Evidence(self.now, True, True, scene=Scene(available=True, faces=2))
        self.assertFalse(list_recordings(self.s, {})['hidden'])
        self.assertFalse(local_voice_history_allowed(self.s))
        self.assertFalse(snapshot(self.s)['recordings_raw_allowed'])

    def test_revoking_guest_consent_disables_single_user_mode(self):
        self.store.execute("INSERT OR REPLACE INTO agreements VALUES ('guest','Guest',1,'{}')")
        self.store.set('single_user_local', True)
        self.s.control('revoke', {'id': 'guest'})
        self.assertFalse(self.store.get('single_user_local'))
        self.assertTrue(list_recordings(self.s, {})['hidden'])

    def test_single_user_control_is_local_explicit_and_requires_owner_consent(self):
        def call(args):
            handler = Handler.__new__(Handler)
            handler.connection = Mock()
            handler.rfile = io.BytesIO(json.dumps({'command': 'ui-single-user', 'args': args}).encode() + b'\n')
            handler.wfile = io.BytesIO()
            handler.server = SimpleNamespace(service=self.s)
            handler.handle()
            return json.loads(handler.wfile.getvalue())
        before = self.s.local_history_epoch
        self.assertEqual(call({'enabled': True})['result'], {'single_user_local': True})
        self.assertGreater(self.s.local_history_epoch, before)
        self.assertIn('error', call({'enabled': 1}))
        before = self.s.local_history_epoch
        self.s.control('revoke', {'id': 'owner'})
        self.assertGreater(self.s.local_history_epoch, before)
        self.assertIn('error', call({'enabled': True}))

    def test_local_access_status_contains_only_permission_booleans(self):
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        def call(args):
            handler = Handler.__new__(Handler); handler.connection = Mock()
            handler.rfile = io.BytesIO(json.dumps({'command': 'ui-local-access-status', 'args': args}).encode() + b'\n')
            handler.wfile = io.BytesIO(); handler.server = SimpleNamespace(service=self.s); handler.handle()
            return json.loads(handler.wfile.getvalue())
        value = call({})['result']
        self.assertEqual(value, {'single_user_local': True, 'history_visible': True, 'raw_allowed': False,
                                 'local_feedback_restored': False,
                                 'voice_sensing_ready': True, 'call_safe': True,
                                 'voice_output_ready': False, 'local_summary_ready': False,
                                 'voice_enabled': False, 'voice_profile_available': False,
                                 'echo_cancellation_ready': False, 'voice_worker_warm': False,
                                 'sensing_fresh': True, 'camera_healthy': True, 'microphone_healthy': True,
                                 'other_face_detected': False, 'other_voice_detected': False, 'multiple_faces_detected': False})
        self.assertIn('error', call({'unexpected': True}))
        self.s.control('revoke', {'id': 'owner'})
        self.assertFalse(call({})['result']['history_visible'])

    def test_single_user_feedback_keeps_local_rows_but_cancels_private_work(self):
        self.store.set('single_user_local', True)
        feedback = self.s.feedback
        feedback.rows.append({'id': 'synthetic', 'turn': 'turn', 'session': 'session', 'at': self.now,
            'mode': 'private', 'text': 'Synthetic local episode', 'original': 'Synthetic local episode',
            'revision': 1, 'decision': {'mode': 'private'}, 'revisions': [], 'annotations': [], 'memories': [], 'saved': False})
        feedback.contexts['synthetic'] = {'private': 'synthetic'}
        export = feedback.export_cancel
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.s._invalidate('permission-paused')
        self.assertTrue(export.is_set()); self.assertEqual(feedback.contexts, {})
        self.assertEqual(len(snapshot(self.s)['feedback']['episodes']), 1)
        handler = Handler.__new__(Handler); handler.connection = Mock()
        handler.rfile = io.BytesIO(b'{"command":"ui-feedback","args":{}}\n')
        handler.wfile = io.BytesIO(); handler.server = SimpleNamespace(service=self.s); handler.handle()
        self.assertEqual(len(json.loads(handler.wfile.getvalue())['result']['episodes']), 1)
        with self.assertRaises(ValueError): feedback.correct({'kind': 'label_other', 'event': 'synthetic'})
        self.s.control('enabled', {'value': False})
        self.assertEqual(snapshot(self.s)['feedback']['episodes'], [])
        self.assertEqual(list(feedback.rows), [])

    def test_single_user_restores_feedback_for_local_view_without_owner_identity_or_retention(self):
        feedback = self.s.feedback
        feedback._open()
        feedback.storage.episode('synthetic', 'turn', 'session', self.now, 'Synthetic saved episode', scope='private')
        feedback.storage.decide('synthetic', {'outcome': 'replied'}, revision=1)
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        with patch.object(feedback.storage, 'expire', side_effect=AssertionError('read must not expire data')):
            feedback._restore_local_history()
        rows = feedback.snapshot(True)['episodes']
        self.assertEqual([(r['id'], r['text'], r['revision']) for r in rows], [('synthetic', 'Synthetic saved episode', 0)])
        self.assertEqual(feedback.contexts, {})
        with self.assertRaises(ValueError): feedback.correct({'kind': 'label_other', 'event': 'synthetic'})
        self.now += 86401
        self.assertEqual(feedback.snapshot(True)['episodes'][0]['text'], '')
        self.assertEqual(feedback.local_rows[0]['text'], '')
        self.s.control('enabled', {'value': False})
        self.assertEqual(feedback.local_rows, [])
        self.assertEqual(feedback.snapshot(False)['episodes'], [])
        feedback.storage.close(); feedback.storage = None

    def test_single_user_feedback_restore_requires_active_consent_and_archive(self):
        feedback = self.s.feedback
        feedback._open()
        feedback.storage.episode('synthetic', 'turn', 'session', self.now, 'Synthetic', scope='private')
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.store.set('continuous_archive', False)
        feedback._restore_local_history()
        self.assertEqual(feedback.local_rows, [])
        self.store.set('continuous_archive', True)
        self.s.control('revoke', {'id': 'owner'})
        feedback._restore_local_history()
        self.assertEqual(feedback.local_rows, [])
        feedback.storage.close(); feedback.storage = None

    def test_feedback_worker_restores_saved_local_rows_after_restart(self):
        feedback = self.s.feedback
        feedback._open()
        feedback.storage.episode('synthetic', 'turn', 'session', self.now, 'Synthetic saved episode', scope='private')
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        feedback.start()
        deadline = time.monotonic() + 3
        while not feedback.local_restored and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(feedback.local_restored, feedback.error)
        self.assertEqual([row['id'] for row in feedback.snapshot(True)['episodes']], ['synthetic'])

    def test_merged_feedback_keeps_restricted_marker_for_panel_clearing(self):
        from butler_ambient.local_ui import merge_heard
        event = {'id': 'voice:synthetic', 'at': self.now, 'who': 'Голос', 'kind': 'heard', 'text': 'Synthetic'}
        episode = {'id': 'episode:synthetic', 'at': self.now + 1, 'who': 'Владелец', 'kind': 'heard',
                   'text': 'Synthetic', 'episode': 'synthetic', 'revision': 1, 'memories': []}
        rows = merge_heard([event, episode])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['episode'], 'synthetic')
        self.assertEqual(rows[0]['revision'], 1)

    def test_single_user_observation_command_returns_facts_without_frame(self):
        self.observe()
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence')
        self.s.gate.invalidate(self.now)
        handler = Handler.__new__(Handler)
        handler.connection = Mock()
        handler.rfile = io.BytesIO(b'{"command":"ui-observations","args":{}}\n')
        handler.wfile = io.BytesIO()
        handler.server = SimpleNamespace(service=self.s)
        handler.handle()
        result = json.loads(handler.wfile.getvalue())['result']
        self.assertTrue(any(item.get('facts') for item in result['items']))
        self.assertTrue(all(not item.get('frame') for item in result['items']))

    def test_participant_lease_expiring_during_browse_withholds_the_entire_result(self):
        self.observe()
        from butler_ambient.recordings import _sessions
        def slow(items,gaps):
            result=_sessions(items,gaps)
            self.now += 3
            return result
        with patch('butler_ambient.recordings._sessions',side_effect=slow):
            self.assertEqual(list_recordings(self.s,{}),{'hidden':True,'items':[],'days':[],'total':0})

    def test_media_file_actions_leave_shared_facts_transcripts_and_other_files(self):
        oid, mid = self.observe()
        self.store.execute('INSERT INTO observation_media VALUES (?,?)', ('other', mid))
        other = self.store.media('microphone', self.now, self.now+1, b'other', 'wav')
        self.store.retain_transcript('t', 'microphone', self.now, self.now+1, 'private words', media_id=mid, now=self.now)
        item = next(i for i in self.children() if i['id']==oid)
        self.assertEqual(item['media'][0]['id'], mid)
        self.assertEqual(item['transcripts'][0]['text'], 'private words')
        args = {'action':'preserve','observation':oid,'media':mid}
        self.assertTrue(control(self.s, args)['ok'])
        saved = self.store.execute('SELECT preserved FROM media WHERE id=?', (mid,))[0]['preserved']
        self.assertTrue(Path(saved).is_file())
        self.assertFalse(control(self.s, {**args,'observation':'unrelated','action':'delete'})['ok'])
        self.assertTrue(control(self.s, {**args,'action':'delete'})['ok'])
        self.assertFalse(Path(saved).exists())
        self.assertEqual(len(self.store.execute('SELECT * FROM observations')), 1)
        self.assertEqual(len(self.store.execute('SELECT * FROM recording_transcripts')), 1)
        self.assertTrue(Path(self.store.execute('SELECT path FROM media WHERE id=?',(other,))[0]['path']).exists())

    def test_paths_outside_store_or_symlink_escape_are_never_shown_or_modified(self):
        oid, mid = self.observe()
        outside = Path(self.tmp.name).parent / ('outside-' + Path(self.tmp.name).name)
        outside.write_bytes(b'private'); self.addCleanup(outside.unlink, missing_ok=True)
        link = self.store.root/'raw/link.wav';link.symlink_to(outside)
        for value in (str(outside), str(link)):
            self.store.execute('UPDATE media SET path=? WHERE id=?', (value, mid))
            item = self.children()[0]
            self.assertIsNone(item['media'][0]['path'])
            self.assertFalse(control(self.s, {'action':'delete','observation':oid,'media':mid})['ok'])
            self.assertTrue(outside.exists())

    def test_playback_rejects_permission_generation_changed_during_conversion(self):
        # The converters and media below are synthetic; no user media is read.
        from butler_ambient import processes
        for changed in ('capture', 'dialogue'):
            with self.subTest(changed=changed):
                self.authorize()
                mid = self.store.media('camera', self.now, self.now + 1, b'\xff\xd8synthetic\xff\xd9', 'mjpeg', temporary=True)
                def convert(command, **kwargs):
                    if changed == 'capture': self.s.gate.invalidate(self.now, 'owner-paused')
                    else: self.s.dialogue_gate.invalidate('owner-paused')
                    for _ in range(3):
                        self.now += .01
                        evidence = Evidence(self.now, True, True, frozenset({'owner'}), True,
                            camera_ids=frozenset({'owner'}), camera_certain=True,
                            voice_ids=frozenset({'owner'}), voice_certain=True, speech_detected=True)
                        if changed == 'capture': self.s.gate.check(evidence, self.now)
                        else: self.s.dialogue_gate.check(evidence, self.now, True)
                    self.assertTrue(self.s.gate.permits(self.now) and self.s.dialogue_gate.permits(self.now))
                    Path(command[-1]).write_bytes(b'synthetic converted file')
                    return SimpleNamespace(returncode=0)
                with patch('shutil.which', return_value='/synthetic/ffmpeg'), patch.object(processes, 'run', side_effect=convert):
                    result = playback(self.s, {'observation': 'media:' + mid, 'media': mid})
                self.assertEqual(result, {'hidden': True})

    def test_playback_rejects_stop_started_during_conversion(self):
        from butler_ambient import processes
        mid = self.store.media('camera', self.now, self.now + 1, b'\xff\xd8synthetic\xff\xd9', 'mjpeg', temporary=True)
        def convert(command, **kwargs):
            self.s.stop_event.set()  # stop() sets this before it can take service.lock
            self.assertTrue(self.s.gate.permits(self.now) and self.s.dialogue_gate.permits(self.now))
            Path(command[-1]).write_bytes(b'synthetic converted file')
            return SimpleNamespace(returncode=0)
        with patch('shutil.which', return_value='/synthetic/ffmpeg'), patch.object(processes, 'run', side_effect=convert):
            result = playback(self.s, {'observation': 'media:' + mid, 'media': mid})
        self.assertEqual(result, {'hidden': True})

    def test_browsing_a_pending_backlog_has_bounded_database_work(self):
        # VM instructions, rather than a wall-clock deadline, keep this stable
        # across runners while catching a correlated scan of all observations
        # for every pending original.
        path=self.store.root/'raw/backlog.wav';path.write_bytes(b'audio')
        with self.store.db:
            self.store.db.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?)',
                [(f'old-{i}',f'gone-{i}','microphone',self.now-60,self.now-59,'[]') for i in range(2300)])
            self.store.db.executemany('INSERT INTO media(id,source,start,end,path) VALUES (?,?,?,?,?)',
                [(f'pending-{i}','microphone',self.now-10,self.now-9,str(path)) for i in range(2000)])
        steps=[0]
        def bounded():
            steps[0]+=1
            return int(steps[0]>5000)
        self.store.db.set_progress_handler(bounded,1000)
        try:
            with patch.object(Path,'resolve',autospec=True,side_effect=Path.resolve) as resolved:
                result=list_recordings(self.s,{})
        finally:
            self.store.db.set_progress_handler(None,0)
        self.assertFalse(result['hidden'])
        self.assertTrue(any(row['type']=='session' for row in result['items']))
        roots={self.store.root/'raw',self.store.root/'preserved'}
        self.assertEqual(sum(call.args[0] in roots for call in resolved.call_args_list),2)

    def test_day_picker_includes_originals_when_no_analysis_or_events_exist_for_that_day(self):
        yesterday = self.now - 18*3600  # previous evening, still inside the raw 24-hour retention
        mid = self.store.media('microphone', yesterday, yesterday+1, b'pending audio', 'wav')
        result = list_recordings(self.s,{})
        day = dt.datetime.fromtimestamp(yesterday,ZONE).date().isoformat()
        self.assertIn(day, [row['day'] for row in result['days']])
        selected = list_recordings(self.s,{'day':day})
        session = next(row for row in selected['items'] if row['type']=='session')
        detail = list_recordings(self.s,{'day':day,'session':session['id']})['items'][0]
        self.assertEqual(detail['media'][0]['id'],mid)
        self.assertTrue(Path(detail['media'][0]['path']).is_file())

    def test_sessions_split_on_activity_source_gap_and_day_with_child_paging(self):
        for offset in range(105):self.observe(self.now+offset*60)
        self.observe(self.now+105*60, activity='read')
        self.observe(self.now, source='screen')
        self.store.event('microphone','owner-paused',self.now+20,self.now+40)
        result = list_recordings(self.s, {})
        sessions = [i for i in result['items'] if i['type']=='session']
        large = next(i for i in sessions if i['count']==104)
        self.assertEqual(len(large['observations']),0)
        self.assertEqual(len(list_recordings(self.s, {'session':large['id']})['items']),100)
        self.assertEqual(large['content'],['медиа','факты'])
        page = list_recordings(self.s, {'session':large['id'],'offset':100})
        self.assertEqual((page['total'],len(page['items'])),(104,4))
        self.assertEqual(len(sessions),4)
        mic = list_recordings(self.s, {'source':'microphone'})
        self.assertTrue(all(i['source']=='microphone' for i in mic['items']))
        self.assertEqual(next(i for i in mic['items'] if i['type']=='gap')['label'],'пауза')
        self.observe(self.now-86400)
        self.assertEqual(len(list_recordings(self.s,{})['days']),2)

    def test_transcripts_expire_at_30_days_even_after_media_preserve_delete(self):
        self.store.retain_transcript('t','microphone',self.now,self.now+1,'secret transcript',now=self.now)
        self.now += RETENTION-1
        self.authorize()
        # authorize adds .03 seconds, still just below expiry
        self.assertEqual(self.children(day=dt.datetime.fromtimestamp(self.now-RETENTION+1,ZONE).date().isoformat())[0]['transcripts'][0]['text'],'secret transcript')
        self.now += 2
        self.authorize()
        self.assertEqual(self.children(),[])
        self.store.expire(self.now)
        self.assertEqual(self.store.execute('SELECT * FROM recording_transcripts'),[])
        self.assertNotIn(b'secret transcript',(self.store.root/'ambient.sqlite3').read_bytes())
        self.assertEqual((self.store.root/'ambient.sqlite3').stat().st_mode & 0o777,0o600)

    def test_only_current_explicitly_marked_meeting_retains_audio_and_video(self):
        self.assertFalse(mark_meeting(self.s,{'action':'mark'})['ok'])
        meeting = self.current_meeting()
        before = self.store.media('microphone',self.now,self.now+1,b'before','wav',temporary=True)
        tag_capture(self.s,before)
        self.assertIsNone(self.store.execute('SELECT retained_until FROM media WHERE id=?',(before,))[0]['retained_until'])
        self.assertTrue(mark_meeting(self.s,{'action':'mark'})['ok'])
        files = []
        for source,suffix in (('microphone','wav'),('system-audio','wav'),('camera','mjpeg')):
            mid = self.store.media(source,self.now,self.now+1,b'original',suffix,temporary=True)
            tag_capture(self.s,mid); files.append(mid)
            self.store.save_observation(mid,[])
            self.store.preserve(mid,self.now)
        self.store.expire(self.now+86401)
        rows = [self.store.execute('SELECT * FROM media WHERE id=?',(mid,))[0] for mid in files]
        self.assertTrue(all(Path(r['path']).is_file() for r in rows))
        self.assertTrue(all(r['meeting_id']==meeting and r['retained_until']==self.now+RETENTION for r in rows))
        self.store.expire(self.now+RETENTION)
        self.assertTrue(all(not Path(r['path']).exists() and not Path(r['preserved']).exists() for r in rows))
        self.assertEqual(len(self.store.execute('SELECT * FROM observations')),3)
        self.now += 20
        self.s.inputs.audio_status = {'meeting_at':self.now,'meeting':{'available':True,'input_apps':[],'output_apps':[]}}
        self.s.meeting.update()
        self.assertIsNone(self.s.meeting.recording_id)
        self.authorize()
        self.assertNotEqual(self.current_meeting(),meeting)
        self.assertFalse(list_recordings(self.s,{})['meeting']['marked'])

    def test_restart_does_not_reuse_a_mark_and_unverified_capture_does_not_gain_retention(self):
        self.current_meeting();mark_meeting(self.s,{'action':'mark'})
        self.s.gate.invalidate(self.now)
        mid = self.store.media('microphone',self.now,self.now+1,b'a','wav',temporary=True)
        tag_capture(self.s,mid)
        self.assertIsNone(self.store.execute('SELECT retained_until FROM media WHERE id=?',(mid,))[0]['retained_until'])
        from butler_ambient.meeting import Meeting
        replacement = Meeting(self.s)
        self.assertIsNone(replacement.recording_id)
        self.assertEqual(self.store.execute('SELECT id FROM recording_meetings WHERE end IS NULL'),[])

    def test_call_asr_survives_a_fact_model_failure_and_has_no_cloud_export(self):
        self.current_meeting()
        audio = io.BytesIO()
        with wave.open(audio,'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\0\0'*16000)
        mid = self.store.media('system-audio',self.now-1,self.now,audio.getvalue(),'wav',temporary=True)
        tag_capture(self.s,mid)
        self.store.set('continuous_archive',True)
        self.s.analyzer.transcribe.return_value='private call transcript'
        self.s.analyzer.analyze.side_effect=RuntimeError('model failed')
        self.assertFalse(self.s.analyze_group([mid],self.s.archive.generation,archive=True))
        self.assertEqual(self.store.execute('SELECT text FROM recording_transcripts')[0]['text'],'private call transcript')
        self.assertEqual(self.store.execute('SELECT * FROM observations'),[])
        detail=next(row for row in self.children() if row['id']=='media:'+mid)
        self.assertEqual(detail['transcripts'][0]['text'],'private call transcript')
        from butler_ambient import mcp
        self.assertNotIn('recordings',json.dumps(mcp.TOOLS))

    def test_local_voice_navigation_intercepts_before_agent_and_denies_unverified(self):
        text='Батлер, покажи, что было на встрече в 8 вечера'
        target = voice_target(text,self.now)
        self.assertEqual(dt.datetime.fromtimestamp(target['at'],ZONE).hour,20)
        self.assertIsNone(voice_target('открой сайт',self.now))
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.store.set('voice_enabled',True)
        self.s.listening.model=Mock(transcribe=Mock(return_value=text))
        self.s.listening._process(b'\1\0'*16000,self.s.listening.token,self.now,True,time.monotonic())
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertEqual(self.s.recordings_navigation['day'],target['day'])
        self.assertEqual(self.s.recordings_navigation['at'],target['at'])
        self.assertEqual(len(self.store.execute('SELECT * FROM recording_transcripts')),1)
        serial=self.s.recordings_navigation['serial']
        self.assertTrue(navigate_voice(self.s,text,authorized=False))
        self.assertEqual(self.s.recordings_navigation['serial'],serial)
        self.s.gate.invalidate(self.now)
        self.assertTrue(navigate_voice(self.s,text,authorized=True))
        self.assertEqual(self.s.recordings_navigation['serial'],serial)
        self.assertIsNone(snapshot(self.s)['recordings_navigation'])

    def test_single_user_voice_navigation_is_local_without_forging_identity(self):
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        self.store.set('single_user_local', True)
        self.store.set('voice_enabled', True)
        self.store.set('continuous_archive', True)
        self.s.dialogue_gate.invalidate('absence')
        self.s.gate.invalidate(self.now)
        self.s.voice = Conversation(self.s)
        self.s.listening = Listening(self.s)
        text = 'Батлер, покажи записи'
        self.s.listening.model = Mock(transcribe=Mock(return_value=text))
        evidence = Evidence(self.now, True, True, speech_detected=True)
        receipt = MicrophoneCheck(self.now)
        receipt.resolve(evidence, now=self.now)
        checks = UtteranceChecks(); checks.items.append(receipt)
        self.assertFalse(bool(checks))
        self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
        self.assertIsNotNone(self.s.recordings_navigation)
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertFalse(self.s.dialogue_gate.allowed)
        serial = self.s.recordings_navigation['serial']
        other = MicrophoneCheck(self.now)
        other.resolve(Evidence(self.now, True, True, voice_ids=frozenset({'guest'}),
            voice_certain=True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(other)
        self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
        self.assertEqual(self.s.recordings_navigation['serial'], serial)
        self.assertTrue(self.s.voice.jobs.empty())

    def test_single_user_voice_summary_stays_local_and_stops_when_mode_changes(self):
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        from butler_ambient.conversation import Canceled
        self.store.set('single_user_local', True)
        self.store.set('voice_enabled', True)
        self.store.set('continuous_archive', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.listening.model = Mock(transcribe=Mock(return_value='Батлер, дай сводку наблюдений за последний час'))
        self.s.voice.model = Mock(); self.s.brain.ask = Mock()
        self.s.commands.memory.read = Mock(return_value='Синтетическая локальная сводка.')
        receipt = MicrophoneCheck(self.now); receipt.resolve(Evidence(self.now, True, True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(receipt)
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True):
            with patch.object(self.s.feedback, 'capture') as capture:
                self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
            capture.assert_not_called()
            task = self.s.voice.jobs.get_nowait()
            self.assertTrue(task[4].startswith('single-user-history:'))
            self.s.voice.speak = Mock()
            self.s.voice._general_turn(*task)
            self.s.commands.memory.read.assert_called_once_with('summary', {'minutes': 60}, read_only=True)
            self.s.voice.speak.assert_called_once()
            self.s.brain.ask.assert_not_called(); self.s.voice.model.generate_json.assert_not_called()
            self.assertFalse(self.s.dialogue_gate.allowed)
            forged = list(task); forged[7] = 'Батлер, запусти внешнюю задачу'
            with self.assertRaises(Canceled): self.s.voice._general_turn(*forged)
            expired = list(task); expired[5] = time.monotonic() - 9
            with self.assertRaises(Canceled): self.s.voice._general_turn(*expired)
            self.s.commands.memory.read.assert_called_once()
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
            task = self.s.voice.jobs.get_nowait()
            self.store.set('single_user_local', False)
            with self.assertRaises(Canceled): self.s.voice._general_turn(*task)
            self.assertEqual(self.s.voice.speak.call_count, 1)
            self.store.set('single_user_local', True)
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
            stale = self.s.voice.jobs.get_nowait()
            def switch(enabled):
                handler = Handler.__new__(Handler); handler.connection = Mock()
                handler.rfile = io.BytesIO(json.dumps({'command': 'ui-single-user', 'args': {'enabled': enabled}}).encode() + b'\n')
                handler.wfile = io.BytesIO(); handler.server = SimpleNamespace(service=self.s); handler.handle()
                self.assertEqual(json.loads(handler.wfile.getvalue())['result']['single_user_local'], enabled)
            switch(False); switch(True)
            with self.assertRaises(Canceled): self.s.voice._general_turn(*stale)
            self.assertEqual(self.s.voice.speak.call_count, 1)
            self.store.set('single_user_local', True)
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
            task = self.s.voice.jobs.get_nowait()
            def mode_lost_during_speech(_reply, token, generation):
                self.store.set('single_user_local', False)
                self.s.voice._permitted(token, generation)
            self.s.voice.speak.side_effect = mode_lost_during_speech
            with self.assertRaises(Canceled): self.s.voice._general_turn(*task)
            self.s.inputs.cancel_playback.assert_called()
            self.s.brain.ask.assert_not_called()

    def test_single_user_voice_summary_never_routes_known_other_speaker(self):
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        self.store.set('single_user_local', True); self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.listening.model = Mock(transcribe=Mock(return_value='Батлер, дай сводку наблюдений за последний час'))
        receipt = MicrophoneCheck(self.now)
        receipt.resolve(Evidence(self.now, True, True, voice_ids=frozenset({'guest'}), voice_certain=True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(receipt)
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True):
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
        self.assertFalse(self.s.voice.jobs.get_nowait()[4].startswith('single-user-history:'))

    def test_single_user_voice_summary_is_not_spoken_in_quiet_meeting(self):
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        self.store.set('single_user_local', True); self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True); self.store.set('meeting_mode', 'quiet')
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.listening.model = Mock(transcribe=Mock(return_value='Батлер, дай сводку наблюдений за последний час'))
        receipt = MicrophoneCheck(self.now); receipt.resolve(Evidence(self.now, True, True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(receipt)
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True):
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertEqual(self.s.listening.counts.get('local-summary-unavailable'), 1)

    def test_single_user_voice_summary_is_not_spoken_when_call_detection_is_unavailable(self):
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        self.store.set('single_user_local', True); self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True); self.s.config['audio_helper'] = 'synthetic-helper'
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.listening.model = Mock(transcribe=Mock(return_value='Батлер, дай сводку наблюдений за последний час'))
        receipt = MicrophoneCheck(self.now); receipt.resolve(Evidence(self.now, True, True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(receipt)
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True):
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertEqual(self.s.listening.counts.get('local-summary-unavailable'), 1)

    def test_single_user_summary_runs_through_real_voice_queue_with_gate_denied(self):
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        self.store.set('single_user_local', True); self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True); self.store.set('general_dialogue', False)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.listening.model = Mock(transcribe=Mock(return_value='Батлер, дай сводку наблюдений за последний час'))
        self.s.voice.model = Mock(); self.s.brain.ask = Mock()
        self.s.commands.memory.read = Mock(return_value='Синтетическая сводка.')
        self.s.voice.speak = Mock()
        receipt = MicrophoneCheck(self.now); receipt.resolve(Evidence(self.now, True, True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(receipt)
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True), patch.object(self.s.voice, '_deliver_dialogue'):
            self.assertFalse(self.s.voice.general_permitted())
            self.s.listening._process(b'\1\0'*16000, self.s.listening.token, self.now, checks, time.monotonic())
            self.s.voice._cycle()
        self.s.voice.speak.assert_called_once()
        self.s.brain.ask.assert_not_called()

    def test_single_user_summary_reads_without_running_retention(self):
        self.observe(start=self.now - 60)
        with patch.object(self.store, 'expire', side_effect=AssertionError('unexpected retention')) as expire:
            reply = self.s.commands.memory.read('summary', {'minutes': 60}, read_only=True)
        expire.assert_not_called()
        self.assertIn('Сводка', reply)

    def test_local_summary_speech_rechecks_hard_closes_and_detected_company(self):
        from butler_ambient.conversation import Canceled, GENERAL, Tokens
        self.store.set('single_user_local', True); self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        def permit():
            event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
            self.s.voice.local_history_epoch = self.s.local_history_epoch
            return Tokens(event, self.s.voice.ended)
        token = permit(); self.s.voice._permitted(token, GENERAL)
        self.s.latest_evidence = Evidence(self.now, True, True, camera_ids=frozenset({'guest'}), camera_certain=True)
        with self.assertRaises(Canceled): self.s.voice._permitted(token, GENERAL)
        self.s.inputs.cancel_playback.assert_called()
        self.s.latest_evidence = Evidence(self.now, True, True)
        token = permit(); self.s.latest_evidence = Evidence(self.now - 3, True, True)
        with self.assertRaises(Canceled): self.s.voice._permitted(token, GENERAL)
        token = permit(); self.s.latest_evidence = Evidence(self.now, False, True)
        with self.assertRaises(Canceled): self.s.voice._permitted(token, GENERAL)
        self.s.latest_evidence = Evidence(self.now, True, True)
        for kind in ('mode', 'recording', 'pause', 'consent'):
            with self.subTest(kind=kind):
                self.store.set('single_user_local', True); self.store.set('continuous_archive', True)
                self.store.execute("UPDATE agreements SET active=1 WHERE id='owner'")
                self.s.gate.enable(True, self.now)
                token = permit()
                if kind == 'mode': self.store.set('single_user_local', False)
                elif kind == 'recording': self.store.set('continuous_archive', False)
                elif kind == 'pause': self.s.gate.enable(False, self.now)
                else: self.store.execute("UPDATE agreements SET active=0 WHERE id='owner'")
                with self.assertRaises(Canceled): self.s.voice._permitted(token, GENERAL)

    def test_mode_change_immediately_stops_only_local_summary_playback(self):
        self.s.voice = Conversation(self.s)
        event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
        self.s._end_local_history_scope()
        self.assertTrue(event.is_set()); self.s.inputs.cancel_playback.assert_called_once()
        self.s.inputs.cancel_playback.reset_mock()
        other = threading.Event(); self.s.voice.token = other; self.s.voice.local_history_token = None
        self.s._end_local_history_scope()
        self.assertFalse(other.is_set()); self.s.inputs.cancel_playback.assert_not_called()
        event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
        self.s.disclosures.active_playback = 'separate-fixed-disclosure'
        self.s._end_local_history_scope()
        self.assertTrue(event.is_set()); self.s.inputs.cancel_playback.assert_not_called()
        self.s.disclosures.active_playback = None

    def test_local_summary_denial_preserves_unrelated_voice_queue(self):
        from butler_ambient.conversation import Canceled, GENERAL, Tokens
        self.store.set('single_user_local', False); self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        local = threading.Event(); other = threading.Event()
        self.s.voice.token = local; self.s.voice.local_history_token = local
        self.s.voice.local_history_epoch = self.s.local_history_epoch
        self.s.voice.jobs.put_nowait((b'', other, GENERAL, False, 'microphone', time.monotonic(), False, 'synthetic'))
        with self.assertRaises(Canceled): self.s.voice._permitted(Tokens(local, self.s.voice.ended), GENERAL)
        self.assertTrue(local.is_set()); self.assertFalse(other.is_set())
        self.assertEqual(self.s.voice.jobs.qsize(), 1)

    def test_quiet_meeting_invalidates_local_summary_playback(self):
        self.s.voice = Conversation(self.s)
        event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
        before = self.s.local_history_epoch
        self.store.set('meeting_mode', 'quiet'); self.s.meeting.update()
        self.assertGreater(self.s.local_history_epoch, before)
        self.assertTrue(event.is_set()); self.s.inputs.cancel_playback.assert_called()

    def test_lost_call_detection_invalidates_local_summary_playback(self):
        self.s.config['audio_helper'] = 'synthetic-helper'
        self.s.inputs.audio_status = {'meeting_at': self.now, 'meeting': {'available': True, 'input_apps': [], 'output_apps': []}}
        self.s.meeting.update(); self.assertTrue(self.s.meeting.local_summary_safe())
        self.s.voice = Conversation(self.s)
        event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
        before = self.s.local_history_epoch
        self.s.inputs.audio_status = {}; self.s.meeting.update()
        self.assertGreater(self.s.local_history_epoch, before)
        self.assertTrue(event.is_set()); self.s.inputs.cancel_playback.assert_called()

    def test_detected_call_stops_local_summary_even_with_manual_free_override(self):
        self.s.config['audio_helper'] = 'synthetic-helper'
        self.s.attention.manual_mode = 'free'
        self.s.voice = Conversation(self.s)
        event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
        for _ in range(3):
            self.now += .1
            self.s.inputs.audio_status = {'meeting_at': self.now, 'meeting': {'available': True, 'input_apps': ['Synthetic Call'], 'output_apps': []}}
            self.s.meeting.update()
        self.assertFalse(self.s.meeting.quiet)  # owner's general Free override remains effective
        self.assertFalse(self.s.meeting.local_summary_safe())
        self.assertTrue(event.is_set()); self.s.inputs.cancel_playback.assert_called()

    def test_detected_company_invalidates_queued_local_history_epoch(self):
        before = self.s.local_history_epoch
        evidence = Evidence(self.now, True, True, camera_ids=frozenset({'guest'}), camera_certain=True)
        batch = SimpleNamespace(gaps=[], start=self.now)
        self.s._apply_evidence(batch, evidence, {'owner': {}}, False)
        self.assertGreater(self.s.local_history_epoch, before)

    def test_failed_participant_sensing_stops_local_summary_immediately(self):
        self.s.voice = Conversation(self.s)
        event = threading.Event(); self.s.voice.token = event; self.s.voice.local_history_token = event
        evidence = Evidence(self.now, False, True)
        batch = SimpleNamespace(gaps=[], start=self.now)
        self.s._apply_evidence(batch, evidence, {'owner': {}}, False)
        self.assertTrue(event.is_set()); self.s.inputs.cancel_playback.assert_called()

    def test_labels_and_gap_rows_and_request_validation(self):
        for reason in ('analysis-buffer-full','service-stopped','analysis-missed','analysis-expired'):
            self.store.event('all',reason,self.now-60,self.now)
        rows = list_recordings(self.s,{})['items']
        self.assertEqual({r['label'] for r in rows},{'буфер заполнен','перезапуск','не проанализировано'})
        self.assertTrue(all('media' not in r and 'facts' not in r for r in rows))
        for args in ({'unknown':True},{'source':'other'},{'offset':-1},{'day':'bad'},{'session':1}):
            with self.assertRaises(ValueError):list_recordings(self.s,args)

    def test_generated_images_and_display_history_are_in_day_timeline_and_owner_only(self):
        from butler_ambient.visual import Visual
        self.s.visual = Visual(self.s)
        picture = self.store.root/'images/generated/fixture.png'
        picture.parent.mkdir(parents=True,exist_ok=True);picture.write_bytes(b'png')
        key='generated-fixture'
        self.store.execute('INSERT INTO visual_requests VALUES (?,?,?,?,?)',(key,'verified-owner-voice','ready',self.now,0))
        aid=self.s.visual.artifact('generated',picture,key,at=self.now)
        self.s.display.present({'kind':'image','image':str(picture),'title':'Local image'})
        generated=list_recordings(self.s,{'source':'generated'})['items']
        self.assertEqual(generated[0]['artifact_id'],aid)
        self.assertEqual(generated[0]['image'],str(picture))
        self.assertEqual(list_recordings(self.s,{'source':'display'})['items'][0]['card']['title'],'Local image')
        self.s.dialogue_gate.invalidate('company')
        ui=snapshot(self.s)
        self.assertEqual(len(ui['display']['cards']),1)
        self.assertEqual(ui['visual']['artifacts'],[])
        self.assertEqual(list_recordings(self.s,{})['items'],[])
        self.authorize()
        self.assertEqual(len(self.s.display.snapshot()['cards']),1)

    def test_old_mcp_observation_tools_cannot_export_recording_facts_or_transcripts(self):
        from butler_ambient.mcp import serve
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'butler_activity_summary','arguments':{'start':1,'end':2}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'butler_observe','arguments':{'source':'camera'}}}]
        def call(command,args):
            return {'status':'ok','start':1,'end':2,'covered_seconds':1,'coverage_fraction':1,'evidence':['SECRET'],
                    'work_topics':['SECRET'],'approximate_seconds':{'SECRET':1},'observations':[{'facts':'SECRET'}],'transcript':'SECRET'}
        writer=io.StringIO();serve(self.store.root,io.StringIO('\n'.join(json.dumps(r) for r in requests)),writer,call)
        self.assertNotIn('SECRET',writer.getvalue())
        self.assertIn('owner-local',writer.getvalue())

    @unittest.skipUnless(shutil.which('ffmpeg'),'requires local FFmpeg')
    def test_camera_original_becomes_playable_without_altering_original_and_cache_expires(self):
        # A real JPEG fixture, repeated like the native continuous camera stream.
        import base64
        jpeg=Path(__file__).with_name('fixtures')/'scene.jpg'
        candidates=list((Path(__file__).parent/'fixtures').glob('*.jpg'))
        if candidates:frame=candidates[0].read_bytes()
        else:
            from PIL import Image
            data=io.BytesIO();Image.new('RGB',(64,64),'blue').save(data,format='JPEG');frame=data.getvalue()
        mid=self.store.media('camera',self.now,self.now+2,frame*3,'mjpeg',temporary=True)
        result=playback(self.s,{'observation':'media:'+mid,'media':mid})
        self.assertTrue(Path(result['path']).is_file())
        self.assertEqual(Path(self.store.execute('SELECT path FROM media WHERE id=?',(mid,))[0]['path']).read_bytes(),frame*3)
        self.store.delete(mid)
        self.assertFalse(Path(result['path']).exists())
