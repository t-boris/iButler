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
from unittest.mock import Mock, patch

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
        self.assertEqual(ui['display']['cards'],[])
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
