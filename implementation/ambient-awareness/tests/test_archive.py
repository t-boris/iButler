"""New passive recording scope does not authorize privileged owner actions."""
import tempfile
import threading
import time
import sqlite3
import io
import wave
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.policy import Evidence
from butler_ambient.sensing import Batch


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        disk=patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=10**10))
        disk.start();self.addCleanup(disk.stop)
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name));self.now=100.
        self.s=Service(self.store,Mock(),Mock(),Mock(),clock=lambda:self.now,transport=Mock())
        self.s.screen_worker.submit=Mock()
        self.store.set('continuous_archive',True)
        self.s.control('enabled',{'value':True})
        self.s.verifier.verify.side_effect=lambda b,r:Evidence(b.end,True,True)
        self.a=self.s.archive

    def tearDown(self):
        self.s.stop();self.store.close();self.temp.cleanup()

    def batch(self, **kwargs):
        self.now+=1
        return Batch(self.now-1,self.now,kwargs.get('frames',[b'controlled jpeg']),kwargs.get('pcm',b'\0'*32000),frame_at=self.now,gaps=kwargs.get('gaps',()))

    def wait_advice(self):
        deadline=time.monotonic()+2
        while self.s.schedules.manual.get('advice') == 'running' and time.monotonic()<deadline:
            time.sleep(.01)

    def test_advice_is_evaluated_after_a_successful_source_when_another_fails(self):
        self.store.media('camera', self.now-2, self.now-1, b'jpeg', 'mjpeg')
        self.store.media('microphone', self.now-2, self.now-1, b'wave', 'wav')
        self.s.analysis_error = 'synthetic-source-failure'
        self.s.analysis_diagnostic = {'source': 'camera'}
        self.s.delivery.send = Mock(return_value='sent')
        candidate = {'key': 'synthetic', 'topic': 'work', 'created_at': self.now, 'text': 'synthetic advice'}
        with patch.object(self.s, 'analyze_group', side_effect=[False, True]), \
             patch('butler_ambient.service.advice_candidate', return_value=candidate), \
             patch('butler_ambient.service.quiet', return_value=False), \
             patch('butler_ambient.daily_report.advice_report', return_value=('synthetic advice','work')):
            self.s.analysis_tick(self.a.generation, archive=True)
            self.wait_advice()
        self.assertEqual(self.s.delivery.send.call_count, 1)
        self.assertEqual(self.s.analysis_error, 'synthetic-source-failure')

    def test_advice_after_source_failure_is_shown_only_in_display(self):
        self.store.media('camera', self.now-2, self.now-1, b'jpeg', 'mjpeg')
        self.store.media('microphone', self.now-2, self.now-1, b'wave', 'wav')
        self.s.analysis_error = 'synthetic-source-failure'
        self.s.analysis_diagnostic = {'source': 'camera'}
        candidate = {'key': 'synthetic', 'topic': 'work', 'created_at': self.now, 'text': 'synthetic advice'}
        with patch.object(self.s, 'analyze_group', side_effect=[False, True]), \
             patch('butler_ambient.service.advice_candidate', return_value=candidate), \
             patch('butler_ambient.delivery.quiet', return_value=False), \
             patch('butler_ambient.service.quiet', return_value=False), \
             patch('butler_ambient.daily_report.advice_report', return_value=('synthetic advice','work')):
            self.s.analysis_tick(self.a.generation, archive=True)
            self.wait_advice()
        cards = self.s.display.snapshot()['cards']
        self.assertEqual([card['title'] for card in cards], ['Инициативный совет'])
        self.s.delivery.transport.send.assert_not_called()
        self.assertEqual(self.s.analysis_error, 'synthetic-source-failure')

    def test_cancel_during_generated_advice_prevents_delivery(self):
        entered, release, token = threading.Event(), threading.Event(), threading.Event()
        candidate = {'key':'synthetic-cancel', 'topic':'work', 'created_at':self.now, 'text':'unused'}
        def generate(*args, **kwargs):
            entered.set(); release.wait(2)
            return ('synthetic advice','work')
        with patch('butler_ambient.service.advice_candidate',return_value=candidate), \
             patch('butler_ambient.service.quiet',return_value=False), \
             patch('butler_ambient.daily_report.advice_report',side_effect=generate):
            self.s._offer_advice(self.a,self.a.generation,True,token)
            self.assertTrue(entered.wait(2))
            token.set(); release.set(); self.wait_advice()
        self.assertEqual(self.s.display.snapshot()['cards'], [])
        self.assertEqual(self.store.execute("SELECT id FROM deliveries"), [])

    def test_stopped_analysis_does_not_publish_advice_after_a_source_failure(self):
        self.store.media('camera', self.now-2, self.now-1, b'jpeg', 'mjpeg')
        self.s.analysis_error = 'synthetic-source-failure'
        self.s.analysis_diagnostic = {'source': 'camera'}
        token = threading.Event()
        def fail(*args, **kwargs):
            token.set()
            return False
        candidate = {'key': 'synthetic-cancelled', 'topic': 'work', 'created_at': self.now, 'text': 'synthetic advice'}
        with patch.object(self.s, 'analyze_group', side_effect=fail), \
             patch('butler_ambient.service.advice_candidate', return_value=candidate), \
             patch('butler_ambient.delivery.quiet', return_value=False):
            self.s.analysis_tick(self.a.generation, run_token=token, archive=True)
        self.assertEqual(self.s.display.snapshot()['cards'], [])
        self.s.delivery.transport.send.assert_not_called()

    def test_advice_telegram_is_optional_and_keeps_the_display_card(self):
        self.store.set('schedule_telegram_advice', True)
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        for _ in range(3):
            self.now += .1
            self.s.gate.check(Evidence(self.now, True, True, frozenset({'owner'}), True), self.now)
        self.s.analysis_token = threading.Event()
        self.s.delivery.transport.send.return_value = 'core:synthetic'
        candidate = {'key': 'synthetic-telegram', 'topic': 'work', 'created_at': self.now, 'text': 'synthetic advice'}
        with patch('butler_ambient.service.advice_candidate', return_value=candidate), \
             patch('butler_ambient.delivery.quiet', return_value=False), \
             patch('butler_ambient.service.quiet', return_value=False), \
             patch('butler_ambient.daily_report.advice_report', return_value=('synthetic advice','work')):
            self.s.analysis_tick(self.a.generation, archive=True)
            self.s.analysis_tick(self.a.generation, archive=True)
            self.wait_advice()
        cards = self.s.display.snapshot()['cards']
        self.assertEqual([card['title'] for card in cards], ['Инициативный совет'])
        self.assertEqual(cards[0]['status'], 'Передано в очередь Telegram.')
        self.s.delivery.transport.send.assert_called_once()

    def test_failed_media_database_insert_removes_partial_file_and_leaves_database_usable(self):
        self.store.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON media BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.media('microphone',1,2,b'controlled partial media','wav')
        self.assertEqual(list((self.store.root/'raw').iterdir()),[])
        self.assertEqual(self.store.execute('SELECT id FROM media'),[])
        self.store.execute('DROP TRIGGER injected_failure')
        mid=self.store.media('microphone',2,3,b'controlled recovered media','wav')
        self.assertEqual(self.store.execute('SELECT id FROM media')[0]['id'],mid)

    def test_unknown_participants_do_not_stop_passive_local_archive_or_authorize_images(self):
        self.assertTrue(self.s.accept_batch(self.batch()))
        rows=self.store.execute('SELECT source,path FROM media')
        self.assertEqual({r['source'] for r in rows},{'camera','microphone'})
        self.assertTrue(all(Path(r['path']).is_file() for r in rows))
        self.assertFalse(self.s.gate.permits(self.now))
        self.assertFalse(self.s.voice_gate.permits(self.now))
        self.assertEqual(self.s.request_fresh('camera')['status'],'paused')
        self.assertTrue(self.s.status()['recording_allowed'])
        self.assertFalse(self.s.status()['action_permission'])
        self.s.delivery.transport.send.assert_not_called()

    def test_meeting_quiet_does_not_pause_archive_and_source_gaps_are_independent(self):
        self.s.control('preferences',{'meeting_mode':'quiet'})
        self.s.accept_batch(self.batch(frames=[],gaps=('camera',)))
        self.assertEqual(self.a.snapshot()['sources']['microphone'],'active')
        self.assertEqual(self.a.snapshot()['sources']['camera'],'unavailable')
        self.assertTrue(self.s.meeting.snapshot()['quiet'])
        self.assertEqual([r['source'] for r in self.store.execute('SELECT source FROM media')],['microphone'])

    def test_full_pause_stops_all_sources_and_stale_or_pre_pause_system_pcm_cannot_commit(self):
        self.s.accept_batch(self.batch());generation=self.a.generation
        self.s.control('enabled',{'value':False})
        before=len(self.store.execute('SELECT id FROM media'))
        self.a._save_system(b'\0'*32000,generation)
        self.s.accept_batch(self.batch())
        self.assertEqual(len(self.store.execute('SELECT id FROM media')),before)
        self.assertFalse(self.a.snapshot()['enabled'])
        self.assertEqual(set(self.a.snapshot()['sources'].values()),{'paused'})
        self.s.control('enabled',{'value':True})
        self.a._save_system(b'\0'*32000,generation)
        self.assertEqual(len(self.store.execute('SELECT id FROM media')),before)

    def test_archive_analysis_survives_identity_pause_and_system_source_never_becomes_owner_command(self):
        self.s.accept_batch(self.batch())
        self.a._save_system(b'\0'*32000,self.a.generation)
        row=self.store.execute("SELECT id FROM media WHERE source='system-audio'")[0]
        self.s.analyzer.transcribe.return_value='Controlled remote conversation'
        self.s.analyzer.analyze.return_value=[{'kind':'activity','activity':'work','confidence':.9,'detail':'Controlled meeting'}]
        self.s.analyzer.cancel.reset_mock()
        self.s._invalidate('participant-unknown')
        self.s.analyzer.cancel.assert_not_called()
        self.assertFalse(self.a.token.is_set())
        self.assertTrue(self.s.analyze_group([row['id']],self.a.generation,archive=True))
        self.assertEqual(self.s.analyzer.analyze.call_args.args[0],'system-audio')
        observation=self.store.execute('SELECT source FROM observations')[0]
        self.assertEqual(observation['source'],'system-audio')
        self.assertIsNone(self.s.commands.pending)
        self.assertEqual(self.store.execute('SELECT * FROM deliveries'),[])

    def test_old_retained_backlog_is_analyzed_after_one_minute_and_failures_can_retry(self):
        self.s.accept_batch(self.batch())
        self.now+=300
        self.s.analyzer.transcribe.return_value='Synthetic backlog'
        self.s.analyzer.analyze.side_effect=RuntimeError('controlled model failure')
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual({r['analyzed'] for r in self.store.execute('SELECT analyzed FROM media')},{0})
        self.s.analyzer.analyze.side_effect=None
        self.s.analyzer.analyze.return_value=[{'kind':'activity','activity':'work','confidence':.9,'detail':'Synthetic backlog'}]
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual({r['analyzed'] for r in self.store.execute('SELECT analyzed FROM media')},{1})
        self.assertEqual({r['source'] for r in self.store.execute('SELECT source FROM observations')},{'camera','microphone'})

    def test_only_camera_recordings_actually_examined_are_marked_and_reclaimed(self):
        ids=[self.store.media('camera',90+i,91+i,b'\xff\xd8'+bytes([65+i])+b'\xff\xd9','mjpeg',temporary=True) for i in range(7)]
        self.s.analyzer.analyze.return_value=[]
        self.assertTrue(self.s.analyze_group(ids,self.a.generation,archive=True))
        self.assertEqual(len(self.s.analyzer.analyze.call_args.kwargs['images']),5)
        self.assertEqual(len(self.store.execute('SELECT id FROM media WHERE analyzed=1 AND path IS NULL')),5)
        self.assertEqual(len(self.store.execute('SELECT id FROM media WHERE analyzed=0 AND path IS NOT NULL')),2)
        links=self.store.execute('SELECT media_id FROM observation_media')
        self.assertEqual({r['media_id'] for r in links},set(ids[:5]))
        self.assertTrue(self.s.analyze_group(ids[5:],self.a.generation,archive=True))
        self.assertEqual(self.store.spool_bytes(),0)

    def test_analysis_tick_processes_every_camera_recording_in_bounded_batches(self):
        for i in range(7):self.store.media('camera',90+i,91+i,b'\xff\xd8'+bytes([65+i])+b'\xff\xd9','mjpeg',temporary=True)
        self.s.analyzer.analyze.return_value=[]
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(self.s.analyzer.analyze.call_count,2)
        self.assertEqual(self.store.execute('SELECT id FROM media WHERE analyzed=0'),[])
        self.assertEqual(self.store.spool_bytes(),0)

    def test_new_spool_precedes_legacy_without_splitting_contiguous_audio(self):
        for end in (99,100,10,11):
            mid=self.store.media('microphone',end-1,end,Batch(end-1,end,[],b'\0'*32000).wav(),'wav',temporary=end>90)
            if end<90:self.store.execute("UPDATE media SET storage_class='legacy' WHERE id=?",(mid,))
        durations=[]
        def transcribe(data,**kwargs):
            with wave.open(io.BytesIO(data),'rb') as audio:durations.append(audio.getnframes()/audio.getframerate())
            return 'Synthetic recovery fixture'
        self.s.analyzer.transcribe.side_effect=transcribe
        self.s.analyzer.analyze.return_value=[]
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(durations,[2.,2.])

    def test_legacy_snapshot_yields_to_fresh_capture_between_model_calls(self):
        for i in range(10):self.store.media('camera',10+i,11+i,b'\xff\xd8L\xff\xd9','mjpeg')
        def incoming(*args,**kwargs):
            self.now+=60
            self.store.media('camera',self.now-1,self.now,b'\xff\xd8F\xff\xd9','mjpeg',temporary=True)
            return []
        self.s.analyzer.analyze.side_effect=incoming
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(self.s.analyzer.analyze.call_count,1)
        self.assertEqual(self.s.pending.get_nowait(),('archive',self.a.generation))
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE storage_class='spool' AND analyzed=0")),1)
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE storage_class='requested' AND analyzed=0")),5)

    def test_advice_is_not_skipped_when_backlog_yields_to_fresh_capture(self):
        for i in range(10):
            self.store.media('camera', 10+i, 11+i, b'\xff\xd8L\xff\xd9', 'mjpeg')
        def incoming(*args, **kwargs):
            self.now += 60
            self.store.media('camera', self.now-1, self.now, b'\xff\xd8F\xff\xd9', 'mjpeg', temporary=True)
            return []
        self.s.analyzer.analyze.side_effect = incoming
        def candidate(rows, now):
            return {'key': 'synthetic-yield', 'topic': 'work', 'created_at': now, 'text': 'synthetic advice'}
        with patch('butler_ambient.service.advice_candidate', side_effect=candidate), \
             patch('butler_ambient.delivery.quiet', return_value=False), \
             patch('butler_ambient.service.quiet', return_value=False), \
             patch('butler_ambient.daily_report.advice_report', return_value=('synthetic advice','work')):
            self.s.analysis_tick(self.a.generation, archive=True)
            self.wait_advice()
        self.assertEqual([card['title'] for card in self.s.display.snapshot()['cards']], ['Инициативный совет'])
        self.s.delivery.transport.send.assert_not_called()
        self.assertEqual(self.s.pending.get_nowait(), ('archive', self.a.generation))

    def test_current_minute_precedes_old_spool_as_well_as_legacy(self):
        for i in range(7):self.store.media('camera',10+i,11+i,b'\xff\xd8O\xff\xd9','mjpeg',temporary=True)
        fresh=self.store.media('camera',99,100,b'\xff\xd8F\xff\xd9','mjpeg',temporary=True)
        seen=[]
        def analyze(*args,**kwargs):
            seen.extend(kwargs['images']);return []
        self.s.analyzer.analyze.side_effect=analyze
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(seen[0],b'\xff\xd8F\xff\xd9')
        self.assertEqual(self.store.execute('SELECT analyzed FROM media WHERE id=?',(fresh,))[0]['analyzed'],1)
        self.assertEqual(len(seen),8)

    def test_old_spool_yields_after_minute_without_waiting_for_legacy_class(self):
        for i in range(10):self.store.media('camera',10+i,11+i,b'\xff\xd8O\xff\xd9','mjpeg',temporary=True)
        def incoming(*args,**kwargs):
            self.now+=60
            self.store.media('camera',self.now-1,self.now,b'\xff\xd8F\xff\xd9','mjpeg',temporary=True)
            return []
        self.s.analyzer.analyze.side_effect=incoming
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(self.s.analyzer.analyze.call_count,1)
        self.assertEqual(self.s.pending.get_nowait(),('archive',self.a.generation))
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE analyzed=0")),6)

    def test_recent_tail_cannot_fall_behind_old_spool_when_it_crosses_minute_boundary(self):
        self.now=1000.
        for start,tag in ((10,b'O'),(925,b'T'),(999,b'F')):
            self.store.media('camera',start,start+1,b'\xff\xd8'+tag+b'\xff\xd9','mjpeg',temporary=True)
        seen=[]
        def analyze(*args,**kwargs):
            seen.extend(frame[2:3] for frame in kwargs['images']);return []
        self.s.analyzer.analyze.side_effect=analyze
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(seen,[b'F',b'T',b'O'])
        self.assertEqual(self.store.execute('SELECT id FROM media WHERE analyzed=0'),[])

    def test_slow_current_snapshot_finishes_other_sources_before_yielding_old_backlog(self):
        self.now=1000.
        self.store.media('camera',999,1000,b'\xff\xd8C\xff\xd9','mjpeg',temporary=True)
        mic=self.store.media('microphone',999,1000,Batch(999,1000,[],b'\0'*32000).wav(),'wav',temporary=True)
        seen=[]
        def analyze(source,**kwargs):
            seen.append(source)
            if len(seen)==1:
                self.now+=61
                self.store.media('camera',1060,1061,b'\xff\xd8F\xff\xd9','mjpeg',temporary=True)
            return []
        self.s.analyzer.analyze.side_effect=analyze
        self.s.analyzer.transcribe.return_value='Controlled microphone fixture'
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertEqual(seen,['camera','microphone'])
        self.assertEqual(self.store.execute('SELECT analyzed FROM media WHERE id=?',(mic,))[0]['analyzed'],1)

    def test_failed_fresh_source_does_not_spin_or_starve_legacy_other_sources(self):
        self.store.media('camera',99,100,b'\xff\xd8C\xff\xd9','mjpeg',temporary=True)
        self.store.media('microphone',10,11,Batch(10,11,[],b'\0'*32000).wav(),'wav')
        self.s.analyzer.transcribe.return_value='Synthetic fixture'
        def analyze(source,**kwargs):
            if source=='camera':raise RuntimeError('injected camera failure')
            return []
        self.s.analyzer.analyze.side_effect=analyze
        self.s.analysis_tick(self.a.generation,archive=True)
        self.assertTrue(self.s.pending.empty())
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE source='microphone' AND analyzed=1")),1)
        self.assertEqual(self.s.analysis_error,'local-analysis-unavailable')

    def test_partial_capture_cannot_hide_a_full_buffer_and_gaps_are_coalesced(self):
        self.store.SPOOL_LIMIT=32044
        self.a.capture(self.batch(frames=[b'x'*40000]))
        self.assertEqual(self.a.storage_error,'analysis-buffer-full')
        self.assertEqual(self.a.sources['camera'],'unavailable')
        self.assertEqual(self.a.sources['microphone'],'active')
        self.a.capture(self.batch(frames=[b'x'*40000]))
        gaps=self.store.execute("SELECT start,end FROM events WHERE source='camera' AND reason='analysis-buffer-full'")
        self.assertEqual(gaps,[{'start':100.,'end':102.}])
        self.store.SPOOL_LIMIT=200000
        self.a.capture(self.batch(frames=[b'x']))
        self.assertIsNone(self.a.storage_error)

    def test_camera_batch_learns_the_actual_budget_without_inventing_examined_records(self):
        ids=[self.store.media('camera',80+i,81+i,b'\xff\xd8C\xff\xd9','mjpeg',temporary=True) for i in range(20)]
        self.s.analyzer.analyze.return_value=[]
        self.s.analyzer.analysis_inference={'perception':{'images':5,'prompt_tokens':950,'context_tokens':8192}}
        self.assertTrue(self.s.analyze_group(ids,self.a.generation,archive=True))
        self.assertEqual(self.s.camera_batch_size,16)
        self.assertEqual(len(self.store.execute('SELECT id FROM media WHERE analyzed=1')),5)
        self.s.analyzer.analysis_inference={'perception':{'images':15,'prompt_tokens':2000,'context_tokens':8192}}
        self.assertTrue(self.s.analyze_group(ids[5:],self.a.generation,archive=True))
        self.assertEqual(len(self.s.analyzer.analyze.call_args.kwargs['images']),15)
        self.assertEqual(self.store.spool_bytes(),0)

    def test_camera_budget_pressure_reduces_batch_and_never_deletes_unverified_inputs(self):
        ids=[self.store.media('camera',90+i,91+i,b'\xff\xd8C\xff\xd9','mjpeg',temporary=True) for i in range(5)]
        self.s.analyzer.analyze.return_value=[]
        self.s.analyzer.analysis_inference={'perception':{'images':5,'prompt_tokens':7500,'context_tokens':8192}}
        self.assertFalse(self.s.analyze_group(ids,self.a.generation,archive=True))
        self.assertEqual(self.s.camera_batch_size,3)
        self.assertEqual(len(self.store.execute('SELECT id FROM media WHERE analyzed=0 AND path IS NOT NULL')),5)
        self.assertEqual(self.s.analysis_error,'camera-context-budget')

    def test_dialogue_priority_cancel_defers_analysis_without_failed_results_or_cleanup(self):
        mid=self.store.media('camera',99,100,b'\xff\xd8C\xff\xd9','mjpeg',temporary=True)
        self.s.analyzer.analysis_epoch=0
        def cancelled(*args,**kwargs):
            self.s.analyzer.analysis_epoch=1
            raise RuntimeError('cancelled for dialogue')
        self.s.analyzer.analyze.side_effect=cancelled
        self.assertFalse(self.s.analyze_group([mid],self.a.generation,archive=True))
        self.assertEqual(self.s.analysis_diagnostic['phase'],'deferred')
        self.assertIsNone(self.s.analysis_error)
        self.assertEqual(self.store.execute('SELECT id FROM observations'),[])
        self.assertEqual(self.store.execute("SELECT id FROM events WHERE reason='analysis-failed'"),[])
        self.assertIsNotNone(self.store.execute('SELECT path FROM media WHERE id=?',(mid,))[0]['path'])

    def test_duplicate_strict_permission_does_not_duplicate_passive_media_and_retention_remains24h(self):
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.s.verifier.verify.side_effect=lambda b,r:Evidence(b.end,True,True,frozenset({'owner'}),True)
        for _ in range(4):self.s.accept_batch(self.batch())
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE source='microphone'")),4)
        camera=self.store.execute("SELECT * FROM media WHERE source='camera'")
        self.assertEqual(len(camera),1)
        self.assertEqual(Path(camera[0]['path']).read_bytes(),b'controlled jpeg'*4)
        self.store.expire(self.now+86401)
        self.assertTrue(all(r['path'] is None for r in self.store.execute('SELECT path FROM media')))
        self.assertEqual(self.a.snapshot()['raw_retention_hours'],24)

    def test_failed_camera_analysis_does_not_starve_other_sources_or_spin_retries(self):
        self.s.accept_batch(self.batch());self.now+=300
        self.s.pending.get_nowait()  # discard the initially scheduled tick
        self.s.analyzer.transcribe.return_value='Synthetic audio'
        def analyze(source,**kwargs):
            if source=='camera':raise RuntimeError('synthetic camera inference failure')
            return []
        self.s.analyzer.analyze.side_effect=analyze
        self.s.analysis_tick(self.a.generation,archive=True)
        rows=self.store.execute('SELECT source,analyzed FROM media')
        self.assertEqual({r['source']:r['analyzed'] for r in rows},{'camera':0,'microphone':1})
        self.assertTrue(self.s.pending.empty())
        self.assertEqual(self.store.execute("SELECT source,reason FROM events WHERE reason='analysis-failed'"),
            [{'source':'camera','reason':'analysis-failed'}])

    def test_passive_video_uses_existing_compact_width_without_changing_identity_input(self):
        import cv2,numpy as np
        self.s.config['recording_width']=640
        image=np.random.default_rng(1).integers(0,256,size=(720,1280,3),dtype=np.uint8)
        ok,jpeg=cv2.imencode('.jpg',image);self.assertTrue(ok)
        batch=self.batch(frames=[jpeg.tobytes()])
        self.s.accept_batch(batch)
        row=self.store.execute("SELECT path FROM media WHERE source='camera'")[0]
        saved=Path(row['path']).read_bytes()
        decoded=cv2.imdecode(np.frombuffer(saved,dtype=np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[:2],(360,640))
        self.assertEqual(batch.frames,[jpeg.tobytes()])
        self.assertLess(len(saved),len(jpeg.tobytes())/4)

    def test_same_assembler_preserves_the_first_two_qualifying_owner_seconds(self):
        from butler_ambient.conversation import Conversation
        from butler_ambient.listening import Listening
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.store.set('face_wake_dialogue', True); self.store.set('continuous_archive', False)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.s.verifier.verify.side_effect = lambda b,r: Evidence(b.end,False,True,
            voice_ids=frozenset({'owner'}),speech_detected=True,voice_certain=True)
        batches = [self.batch(frames=[],pcm=bytes([n,0])*16000) for n in (1,2,3)]
        with patch.object(Listening, 'speech', return_value=True), patch.object(Conversation, '_silence_tail', return_value=1):
            for b in batches:
                self.now=b.end; self.s.accept_batch(b)
        task = self.s.listening.jobs.get_nowait()
        self.assertEqual(task[0], b''.join(b.pcm for b in batches))
        self.assertEqual(len(task[3].items), 3)
        self.assertTrue(self.s.dialogue_gate.permits(self.now))
        self.assertFalse(self.s.gate.permits(self.now))
        self.assertTrue(self.s.voice.jobs.empty())

    def test_pause_unknown_voice_and_capture_gap_clear_only_the_unadmitted_input(self):
        from butler_ambient.conversation import Conversation
        from butler_ambient.listening import Listening
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.store.set('face_wake_dialogue', True); self.store.set('continuous_archive', False)
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        owner = lambda b,r: Evidence(b.end,False,True,voice_ids=frozenset({'owner'}),speech_detected=True,voice_certain=True)
        self.s.verifier.verify.side_effect = owner
        with patch.object(Listening, 'speech', return_value=True), patch.object(Conversation, '_silence_tail', return_value=0):
            self.s.accept_batch(self.batch(frames=[])); self.assertTrue(self.s.listening.audio)
            self.s.control('enabled', {'value': False}); self.assertFalse(self.s.listening.audio)
            self.s.control('enabled', {'value': True})
            self.s.accept_batch(self.batch(frames=[])); self.assertTrue(self.s.listening.audio)
            self.s.verifier.verify.side_effect = lambda b,r: Evidence(b.end,False,True,speech_detected=True)
            self.s.accept_batch(self.batch(frames=[])); self.assertFalse(self.s.listening.audio)
            self.s.verifier.verify.side_effect = owner
            self.s.accept_batch(self.batch(frames=[])); self.assertTrue(self.s.listening.audio)
            self.s.accept_batch(self.batch(frames=[],gaps=('microphone',))); self.assertFalse(self.s.listening.audio)
        self.assertTrue(self.s.voice.jobs.empty())


if __name__=='__main__':unittest.main()
