"""Real file/SQLite boundaries for continuous recording and sampled inference."""
import json
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import cv2
import numpy as np

from butler_ambient.camera_archive import read_samples
from butler_ambient.local_ui import snapshot
from butler_ambient.service import Service,RPCServer
from butler_ambient.sensing import Batch
from butler_ambient.store import Store,AnalysisBufferFull


class CameraClipsTests(unittest.TestCase):
    def setUp(self):
        disk=patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=10**10))
        disk.start();self.addCleanup(disk.stop)
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name));self.now=100.
        self.service=Service(self.store,Mock(),Mock(),Mock(),clock=lambda:self.now,transport=Mock())
        self.service.screen_worker.submit=Mock();self.store.set('continuous_archive',True)
        self.service.control('enabled',{'value':True});self.archive=self.service.archive

    def tearDown(self):
        self.service.stop();self.store.close();self.temp.cleanup()

    def frame(self,value):
        ok,data=cv2.imencode('.jpg',np.full((24,32,3),value,dtype=np.uint8))
        self.assertTrue(ok);return data.tobytes()

    def capture(self,value=0):
        self.now+=1
        frames=[self.frame(value+i) for i in range(5)]
        times=tuple(self.now-1+(i+1)/5 for i in range(5))
        batch=Batch(self.now-1,self.now,frames,b'\0'*32000,self.now,(),times)
        self.archive.capture(batch)
        return frames

    def camera(self):return self.store.execute("SELECT * FROM media WHERE source='camera' ORDER BY start")

    def test_ten_seconds_keep_every_frame_in_one_closed_clip_with_bounded_selection(self):
        frames=[]
        for i in range(10):frames.extend(self.capture(i*20))
        rows=self.camera();self.assertEqual(len(rows),1);row=rows[0]
        self.assertTrue(row['complete']);self.assertEqual(row['end']-row['start'],10)
        self.assertEqual(Path(row['path']).read_bytes(),b''.join(frames))
        selected,times=read_samples(row)
        self.assertEqual(len(selected),3);self.assertEqual(selected[0],frames[0])
        self.assertTrue(all(frame in frames for frame in selected))
        self.assertEqual(times,sorted(times));self.assertEqual(times[0],100.2)
        self.assertEqual(row['size_bytes'],sum(map(len,frames)))
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE source='microphone'")),10)

    def test_open_clip_is_fresh_for_preview_but_cannot_be_analyzed_or_preserved(self):
        frames=self.capture();row=self.camera()[0]
        self.assertFalse(row['complete']);self.assertEqual(self.store.storage_status()['collecting'],1)
        self.assertFalse(self.service.analyze_group([row['id']],self.archive.generation,archive=True))
        self.service.analyzer.analyze.assert_not_called()
        with self.assertRaises(ValueError):self.store.save_observation(row['id'],[])
        with self.assertRaises(ValueError):self.store.preserve(row['id'],self.now)
        preview=snapshot(self.service,want_preview=True)['preview']
        self.assertEqual(Path(preview['path']).read_bytes(),frames[-1])
        self.assertEqual(preview['captured_at'],self.now)

    def test_gap_and_pause_finalize_prefixes_and_clear_transient_signature(self):
        self.capture();self.assertIsNotNone(self.archive.camera.clip['reference'])
        self.now+=1;self.archive.capture(Batch(self.now-1,self.now,[],b'\0'*32000,gaps=('camera',)))
        self.assertTrue(self.camera()[0]['complete']);self.assertIsNone(self.archive.camera.clip)
        self.capture();self.service.control('enabled',{'value':False})
        self.assertTrue(all(row['complete'] for row in self.camera()))
        self.assertIsNone(self.archive.camera.clip)

    def test_timestamp_gap_starts_new_clip_without_bridging_missing_video(self):
        self.capture();self.now+=2;self.capture(80)
        rows=self.camera();self.assertEqual(len(rows),2)
        self.assertTrue(rows[0]['complete']);self.assertFalse(rows[1]['complete'])
        self.assertEqual(rows[1]['start']-rows[0]['end'],2.)

    def test_quota_failure_closes_valid_prefix_and_does_not_commit_rejected_frames(self):
        frames=self.capture();row=self.camera()[0]
        self.store.SPOOL_LIMIT=self.store.spool_bytes()
        self.capture(80)
        row=self.camera()[0];self.assertTrue(row['complete'])
        self.assertEqual(Path(row['path']).read_bytes(),b''.join(frames))
        self.assertEqual(row['end'],101.)
        self.assertEqual(self.archive.write_errors['camera'],'analysis-buffer-full')
        self.assertEqual(self.store.execute("SELECT start,end FROM events WHERE source='camera' AND reason='analysis-buffer-full'"),[{'start':101.,'end':102.}])

    def test_sqlite_append_failure_rolls_back_original_bytes_end_and_sample_offsets(self):
        self.capture();before=self.camera()[0];original=Path(before['path']).read_bytes()
        self.store.execute("CREATE TRIGGER deny_append BEFORE UPDATE OF size_bytes ON media BEGIN SELECT RAISE(ABORT,'injected append failure'); END")
        with self.assertRaises(sqlite3.Error):self.store.append_camera(before['id'],self.frame(90),102,[{'offset':0,'at':100.2}])
        self.assertEqual(self.camera()[0],before)
        self.assertEqual(Path(before['path']).read_bytes(),original)
        self.store.execute('DROP TRIGGER deny_append')

    def test_crash_recovery_discards_only_uncommitted_tail_and_never_runs_for_cli_reads(self):
        self.capture();row=self.camera()[0];path=Path(row['path']);original=path.read_bytes()
        with path.open('ab') as stream:stream.write(b'uncommitted crashed append')
        other=Store(self.store.root)
        try:self.assertFalse(other.execute('SELECT complete FROM media WHERE id=?',(row['id'],))[0]['complete'])
        finally:other.close()
        self.store.recover_camera_clips()
        self.assertEqual(path.read_bytes(),original)
        self.assertTrue(self.camera()[0]['complete'])
        self.assertEqual(self.camera()[0]['samples'],row['samples'])
        self.archive.camera.clip=None  # Simulate the old writer having exited.

    def test_second_service_cannot_finalize_first_services_active_clip(self):
        server=RPCServer(self.service)
        try:
            self.capture()
            with self.assertRaises(RuntimeError):RPCServer(self.service)
            self.assertFalse(self.camera()[0]['complete'])
        finally:server.server_close()

    def test_recovery_quarantines_truncated_data_without_analyzed_markers(self):
        self.capture();row=self.camera()[0];Path(row['path']).write_bytes(b'broken')
        self.store.recover_camera_clips()
        row=self.camera()[0];self.assertEqual(row['analyzed'],-1);self.assertTrue(row['complete'])
        self.assertEqual(self.store.cleanup_analyzed(),0)
        self.assertEqual(self.store.execute('SELECT id FROM observations'),[])
        self.assertEqual(self.store.execute("SELECT reason FROM events WHERE source='camera'"),[{'reason':'capture-recovery-failed'}])
        self.archive.camera.clip=None

    def test_failed_close_cannot_prevent_pause_or_retain_visual_signature(self):
        self.capture()
        with patch.object(self.store,'finish_camera',side_effect=sqlite3.OperationalError('injected close error')):
            self.service.control('enabled',{'value':False})
        self.assertFalse(self.archive.enabled)
        self.assertIsNone(self.archive.camera.clip['reference'])
        self.assertTrue(self.archive.camera.clip['closing'])
        self.assertEqual(self.archive.storage_error,None)  # Pause clears live status.
        self.archive.camera.finish();self.assertIsNone(self.archive.camera.clip)

    def test_analysis_covers_whole_selected_clips_with_explicit_timestamps_and_durable_provenance(self):
        for i in range(20):self.capture((i%10)*20)
        ids=[row['id'] for row in self.camera()]
        self.service.analyzer.analyze.return_value=[]
        self.assertTrue(self.service.analyze_group(ids,self.archive.generation,archive=True))
        call=self.service.analyzer.analyze.call_args
        self.assertEqual(len(call.kwargs['images']),3)
        self.assertEqual(len(call.kwargs['frame_times']),3)
        rows=self.camera();self.assertEqual([r['analyzed'] for r in rows],[1,0])
        self.assertIsNone(rows[0]['path']);self.assertIsNotNone(rows[1]['path'])
        self.assertEqual(len(json.loads(rows[0]['samples'])),3)
        self.assertEqual(self.store.execute('SELECT media_id FROM observation_media'),[{'media_id':ids[0]}])

    def test_stale_short_clip_is_finalized_before_analysis(self):
        self.capture();self.now+=5;self.service.analyzer.analyze.return_value=[]
        self.service.analyzer.transcribe.return_value='Controlled audio'
        self.service.analysis_tick(self.archive.generation,archive=True)
        row=self.camera()[0];self.assertTrue(row['complete']);self.assertEqual(row['analyzed'],1)
        self.assertIsNone(row['path'])

    def test_live_preview_remains_volatile_and_fresh_when_spool_is_full(self):
        self.store.SPOOL_LIMIT=64
        self.store.media('microphone',99,100,b'x'*64,'wav',temporary=True)
        self.now+=1;frame=self.frame(80)
        self.service._stage_batch(Batch(100,101,[frame],b'\0'*32000,101))
        value=snapshot(self.service,want_preview=True)
        self.assertEqual(base64.b64decode(value['preview']['jpeg_base64']),frame)
        self.assertNotIn('path',value['preview'])
        self.assertEqual(self.store.spool_bytes(),64)
        self.assertFalse((self.store.root/'raw/ui-preview.jpg').exists())
        self.service.control('enabled',{'value':False})
        self.assertIsNone(self.service.preview_frame)
        self.assertIsNone(snapshot(self.service,want_preview=True)['preview'])

    def test_stale_or_strict_paused_frames_cannot_enter_live_preview(self):
        self.now+=1;frame=self.frame(80)
        self.service._stage_batch(Batch(100,101,[frame],b'\0'*32000,98))
        self.assertIsNone(self.service.preview_frame)
        self.store.set('continuous_archive',False)
        self.service._stage_batch(Batch(100,101,[frame],b'\0'*32000,101))
        self.assertIsNone(self.service.preview_frame)
        self.assertIsNone(snapshot(self.service,want_preview=True)['preview'])
